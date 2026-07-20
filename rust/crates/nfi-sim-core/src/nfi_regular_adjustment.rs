//! Exact spot/backtest regular-mode adjustment used by NFI X7 tag 121.
//!
//! X7 sends tag 121 through `long_adjust_trade_position_no_derisk()` before
//! the legacy grind callback. The source rebuilds one rebuy bucket and six
//! grind clusters from filled orders on every candle. This module preserves
//! that newest-to-oldest order walk and the callback's early-return order.

use super::{
    adjustment_minimum_pair_stake, evaluate_scalar_program_bundle, fee_close, fee_open,
    insert_projected_feature_window, nfi_long_grind_supports_trade, nfi_profit_snapshot,
    number_value, scalar_truthy, AdjustmentSignal, BTreeMap, Candle, FilledOrder,
    NfiLongGrindRoute, NfiRegularAdjustmentConstants, NfiRegularAdjustmentPolicy, NfiRegularGrind,
    NfiX7TradeManager, OpenTrade, PairSeries, PortfolioConfig, TradeSide, Value,
};

const REGULAR_GRIND_COUNT: usize = 6;

/// The regular helper either returns from the outer callback or deliberately
/// transfers a de-risked trade to the legacy continuation below it.
pub(super) enum RegularAdjustmentOutcome {
    Return(Option<AdjustmentSignal>),
    ContinueLegacy,
}

#[derive(Debug, Default)]
struct RegularCluster {
    count: usize,
    total_amount: f64,
    total_cost: f64,
    entry_ids: Vec<u64>,
    latest_entry_price: Option<f64>,
    open_rate: f64,
    profit_rate: f64,
}

impl RegularCluster {
    fn add_entry(&mut self, order: &FilledOrder) {
        self.count += 1;
        self.total_amount += order.amount;
        self.total_cost += order.amount * order.price;
        self.entry_ids.push(order.id);
        self.latest_entry_price.get_or_insert(order.price);
    }

    fn finish(&mut self, rate: f64) {
        if self.count == 0 {
            return;
        }
        self.open_rate = self.total_cost / self.total_amount;
        self.profit_rate = (rate - self.open_rate) / self.open_rate;
    }

    fn latest_distance(&self, rate: f64) -> f64 {
        self.latest_entry_price
            .map_or(0.0, |price| (rate - price) / price)
    }
}

#[derive(Debug)]
struct RegularState {
    rebuy: RegularCluster,
    grinds: [RegularCluster; REGULAR_GRIND_COUNT],
    is_derisk: bool,
    is_derisk_1: bool,
    first_entry_cost: f64,
    latest_entry_price: f64,
    latest_entry_timestamp_ms: i64,
    latest_order_price: f64,
    latest_order_timestamp_ms: i64,
}

/// Evaluate the source-bound tag-121 regular adjustment prelude.
///
/// `None` is reserved for an invalid or broader-than-certified input. A valid
/// callback no-op is represented by `Return(None)`.
#[allow(clippy::too_many_arguments)]
pub(super) fn evaluate_nfi_regular_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<RegularAdjustmentOutcome> {
    if config.is_futures
        || trade.side != TradeSide::Long
        || route.grind_mode
        || route.adjustment_scope != "spot-regular-backtest-v1"
        || !nfi_long_grind_supports_trade(route, trade)
    {
        return None;
    }
    let constants = route.regular_constants.as_ref()?;
    let program = route.regular_decision_program.as_deref()?;
    let minimum_stake = regular_adjustment_minimum_stake(pair, candle, config)?;
    let state = rebuild_regular_state(trade, candle.open)?;

    // The helper returns this flag to `long_grind_adjust_trade_position()`.
    // Only this outcome continues into the legacy post-de-risk clusters.
    if state.is_derisk {
        return Some(RegularAdjustmentOutcome::ContinueLegacy);
    }

    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        false,
    )?;
    let slice_profit = price_distance(candle.open, state.latest_order_price)?;
    let slice_profit_entry = price_distance(candle.open, state.latest_entry_price)?;
    let num_open_grinds = state
        .grinds
        .iter()
        .map(|cluster| cluster.count)
        .sum::<usize>();
    let entry_program_allows =
        evaluate_regular_entry_program(manager, program, pair, candle_index, slice_profit)?;

    match evaluate_rebuy(
        constants,
        trade,
        candle,
        available_balance,
        minimum_stake,
        snapshot.initial_stake_ratio,
        slice_profit,
        slice_profit_entry,
        entry_program_allows,
        &state,
    )? {
        BranchOutcome::Continue => {}
        BranchOutcome::ReturnNone => {
            return Some(RegularAdjustmentOutcome::Return(None));
        }
        BranchOutcome::Signal(signal) => {
            return Some(RegularAdjustmentOutcome::Return(Some(signal)));
        }
    }

    for (index, definition) in constants.grinds.iter().enumerate() {
        match evaluate_grind(
            definition,
            trade,
            candle,
            config,
            available_balance,
            minimum_stake,
            snapshot.initial_stake_ratio,
            slice_profit,
            num_open_grinds,
            entry_program_allows,
            &state,
            index,
            constants.use_grind_stops,
            &constants.policy,
        )? {
            BranchOutcome::Continue => {}
            BranchOutcome::ReturnNone => {
                return Some(RegularAdjustmentOutcome::Return(None));
            }
            BranchOutcome::Signal(signal) => {
                return Some(RegularAdjustmentOutcome::Return(Some(signal)));
            }
        }
    }

    if let Some(signal) = evaluate_derisk(
        constants,
        trade,
        candle.open,
        minimum_stake,
        snapshot.stake,
        &state,
    ) {
        return Some(RegularAdjustmentOutcome::Return(Some(signal)));
    }
    Some(RegularAdjustmentOutcome::Return(None))
}

enum BranchOutcome {
    Continue,
    ReturnNone,
    Signal(AdjustmentSignal),
}

#[allow(clippy::too_many_arguments)]
fn evaluate_rebuy(
    constants: &NfiRegularAdjustmentConstants,
    trade: &OpenTrade,
    candle: &Candle,
    available_balance: f64,
    minimum_stake: f64,
    initial_stake_ratio: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    entry_program_allows: bool,
    state: &RegularState,
) -> Option<BranchOutcome> {
    let cluster = &state.rebuy;
    if cluster.count >= constants.rebuy_stakes_spot.len() {
        return Some(BranchOutcome::Continue);
    }
    let threshold = *constants.rebuy_thresholds_spot.get(cluster.count)?;
    let distance = if cluster.count > 0 {
        cluster.latest_distance(candle.open)
    } else {
        initial_stake_ratio
    };
    let policy = &constants.policy;
    let age_allows = candle.timestamp_ms - policy.entry_retry_ms > state.latest_entry_timestamp_ms
        && (candle.timestamp_ms - policy.rebuy_order_age_ms > state.latest_order_timestamp_ms
            || slice_profit < policy.forced_age_profit_gate);
    if slice_profit_entry >= threshold
        || distance >= threshold
        || !age_allows
        || !entry_program_allows
    {
        return Some(BranchOutcome::Continue);
    }

    // NFI caps rebuy to max_stake before applying the exchange minimum. If the
    // minimum then exceeds max_stake the callback explicitly returns None.
    let scaled = scale_stakes_for_minimum(
        &constants.rebuy_stakes_spot,
        state.first_entry_cost,
        minimum_stake,
        trade.leverage,
    )?;
    let requested = (state.first_entry_cost * scaled[cluster.count])
        .min(available_balance)
        .max(minimum_stake * policy.minimum_entry_multiplier);
    if requested > available_balance {
        return Some(BranchOutcome::ReturnNone);
    }
    Some(BranchOutcome::Signal(AdjustmentSignal {
        stake_amount: requested,
        tag: "r".to_owned(),
    }))
}

#[allow(clippy::too_many_arguments)]
fn evaluate_grind(
    definition: &NfiRegularGrind,
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
    minimum_stake: f64,
    initial_stake_ratio: f64,
    slice_profit: f64,
    num_open_grinds: usize,
    entry_program_allows: bool,
    state: &RegularState,
    index: usize,
    use_grind_stops: bool,
    policy: &NfiRegularAdjustmentPolicy,
) -> Option<BranchOutcome> {
    let cluster = state.grinds.get(index)?;
    if cluster.count < definition.stakes_spot.len() {
        let threshold = *definition.thresholds_spot.get(cluster.count)?;
        let distance = if cluster.count > 0 {
            cluster.latest_distance(candle.open)
        } else {
            initial_stake_ratio
        };
        let age_allows = candle.timestamp_ms - policy.entry_retry_ms
            > state.latest_entry_timestamp_ms
            && (candle.timestamp_ms - policy.grind_force_order_age_ms
                > state.latest_order_timestamp_ms
                || slice_profit < policy.grind_entry_profit_gate)
            && (num_open_grinds == 0
                || candle.timestamp_ms - policy.grind_order_age_ms
                    > state.latest_order_timestamp_ms
                || slice_profit < policy.forced_age_profit_gate)
            && (num_open_grinds == 0 || slice_profit < policy.additional_grind_profit_gate);
        if distance < threshold && age_allows && entry_program_allows {
            let scaled = scale_stakes_for_minimum(
                &definition.stakes_spot,
                state.first_entry_cost,
                minimum_stake,
                trade.leverage,
            )?;
            let requested = (state.first_entry_cost * scaled[cluster.count])
                .max(minimum_stake * policy.minimum_entry_multiplier);
            if requested > available_balance {
                return Some(BranchOutcome::ReturnNone);
            }
            return Some(BranchOutcome::Signal(AdjustmentSignal {
                stake_amount: requested,
                tag: definition.entry_tag.clone(),
            }));
        }
    }

    if cluster.count > 0
        && cluster.profit_rate
            > definition.profit_threshold_spot + fee_open(config) + fee_close(config)
    {
        let requested = cluster.total_amount * candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(
            trade,
            candle.open,
            minimum_stake,
            policy.minimum_remaining_multiplier,
            requested,
        ) {
            return Some(BranchOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&definition.entry_tag, &cluster.entry_ids),
            }));
        }
    }

    if use_grind_stops && cluster.count > 0 && cluster.profit_rate < definition.stop_threshold_spot
    {
        let requested = cluster.total_amount * candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(
            trade,
            candle.open,
            minimum_stake,
            policy.minimum_remaining_multiplier,
            requested,
        ) {
            return Some(BranchOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&definition.stop_tag, &cluster.entry_ids),
            }));
        }
    }
    Some(BranchOutcome::Continue)
}

fn rebuild_regular_state(trade: &OpenTrade, rate: f64) -> Option<RegularState> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_order = trade.orders.last()?;
    let mut rebuy = RegularCluster::default();
    let mut grinds: [RegularCluster; REGULAR_GRIND_COUNT] =
        std::array::from_fn(|_| RegularCluster::default());
    let mut rebuy_closed = false;
    let mut grind_closed = [false; REGULAR_GRIND_COUNT];
    let mut is_derisk = false;
    let mut is_derisk_1 = false;

    for order in trade.orders.iter().rev() {
        let full_tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && order.id != first_entry.id {
            if let Some(index) = regular_grind_entry_index(full_tag) {
                if !grind_closed[index] {
                    grinds[index].add_entry(order);
                }
            } else if !rebuy_closed && !regular_rebuy_entry_excluded(full_tag) {
                rebuy.add_entry(order);
            }
            continue;
        }
        if order.is_entry {
            continue;
        }

        let head = full_tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = regular_grind_exit_index(head) {
            grind_closed[index] = true;
        } else if regular_derisk_exit(head) {
            is_derisk = true;
            is_derisk_1 |= head == "d1";
            grind_closed.fill(true);
            rebuy_closed = true;
        } else if !regular_rebuy_exit_excluded(head) {
            rebuy_closed = true;
        }

        // NFI also recognizes an untagged or differently tagged de-risk by
        // replaying amount up to this exit.
        if !is_derisk {
            let mut amount = 0.0;
            for replay in &trade.orders {
                if replay.is_entry {
                    amount += replay.amount;
                } else {
                    amount -= replay.amount;
                }
                if replay.id == order.id {
                    if amount < first_entry.amount * 0.95 {
                        is_derisk = true;
                    }
                    break;
                }
            }
        }
        if rebuy_closed && grind_closed.iter().all(|closed| *closed) {
            break;
        }
    }

    rebuy.finish(rate);
    for cluster in &mut grinds {
        cluster.finish(rate);
    }
    Some(RegularState {
        rebuy,
        grinds,
        is_derisk,
        is_derisk_1,
        first_entry_cost: first_entry.amount * first_entry.price,
        latest_entry_price: latest_entry.price,
        latest_entry_timestamp_ms: latest_entry.filled_timestamp_ms,
        latest_order_price: latest_order.price,
        latest_order_timestamp_ms: latest_order.filled_timestamp_ms,
    })
}

fn evaluate_regular_entry_program(
    manager: &NfiX7TradeManager,
    program_name: &str,
    pair: &PairSeries,
    candle_index: usize,
    slice_profit: f64,
) -> Option<bool> {
    let mut variables = BTreeMap::from([
        ("slice_profit".to_owned(), number_value(slice_profit)?),
        ("is_derisk".to_owned(), Value::Bool(false)),
    ]);
    insert_projected_feature_window(
        &mut variables,
        pair,
        candle_index,
        manager.feature_projection(program_name)?,
    )?;
    let value = evaluate_scalar_program_bundle(&manager.programs, program_name, &variables)?;
    Some(scalar_truthy(&value))
}

fn regular_adjustment_minimum_stake(
    pair: &PairSeries,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<f64> {
    let has_limit = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    has_limit
        .then(|| adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent))
}

fn scale_stakes_for_minimum(
    stakes: &[f64],
    slice_amount: f64,
    minimum_stake: f64,
    trade_leverage: f64,
) -> Option<Vec<f64>> {
    let first = *stakes.first()?;
    if slice_amount <= 0.0 || first <= 0.0 || trade_leverage <= 0.0 {
        return None;
    }
    if slice_amount * first / trade_leverage >= minimum_stake {
        return Some(stakes.to_vec());
    }
    let multiplier = minimum_stake / slice_amount / first * trade_leverage;
    Some(stakes.iter().map(|stake| stake * multiplier).collect())
}

fn partial_exit_stake(
    trade: &OpenTrade,
    rate: f64,
    minimum_stake: f64,
    minimum_remaining_multiplier: f64,
    requested_exit: f64,
) -> Option<f64> {
    let remaining = trade.amount * rate / trade.leverage - requested_exit;
    let exit_amount = if remaining < minimum_stake * minimum_remaining_multiplier {
        trade.amount * rate / trade.leverage - minimum_stake * minimum_remaining_multiplier
    } else {
        requested_exit
    };
    let ft_stake = exit_amount * trade.leverage * (trade.stake_amount / trade.amount) / rate;
    (exit_amount > minimum_stake && ft_stake > minimum_stake).then_some(ft_stake)
}

fn derisk_signal(
    trade: &OpenTrade,
    rate: f64,
    minimum_stake: f64,
    minimum_remaining_multiplier: f64,
    tag: &str,
) -> Option<AdjustmentSignal> {
    let requested =
        trade.amount * rate / trade.leverage - minimum_stake * minimum_remaining_multiplier;
    let stake_amount = requested * trade.leverage * (trade.stake_amount / trade.amount) / rate;
    (requested > minimum_stake && stake_amount > minimum_stake).then(|| AdjustmentSignal {
        stake_amount: -stake_amount,
        tag: tag.to_owned(),
    })
}

fn evaluate_derisk(
    constants: &NfiRegularAdjustmentConstants,
    trade: &OpenTrade,
    rate: f64,
    minimum_stake: f64,
    profit_stake: f64,
    state: &RegularState,
) -> Option<AdjustmentSignal> {
    if !constants.derisk_enable {
        return None;
    }
    let minimum_remaining_multiplier = constants.policy.minimum_remaining_multiplier;
    if profit_stake < state.first_entry_cost * constants.derisk_threshold_spot {
        if let Some(signal) = derisk_signal(
            trade,
            rate,
            minimum_stake,
            minimum_remaining_multiplier,
            "d",
        ) {
            return Some(signal);
        }
    }
    if !state.is_derisk_1
        && profit_stake < state.first_entry_cost * constants.derisk_level_1_threshold_spot
    {
        return derisk_signal(
            trade,
            rate,
            minimum_stake,
            minimum_remaining_multiplier,
            "d1",
        );
    }
    None
}

fn order_id_tag(prefix: &str, ids: &[u64]) -> String {
    ids.iter().fold(prefix.to_owned(), |mut tag, id| {
        tag.push(' ');
        tag.push_str(&id.to_string());
        tag
    })
}

fn regular_grind_entry_index(tag: &str) -> Option<usize> {
    match tag {
        "g1" => Some(0),
        "g2" => Some(1),
        "g3" => Some(2),
        "g4" => Some(3),
        "g5" => Some(4),
        "g6" => Some(5),
        _ => None,
    }
}

fn regular_grind_exit_index(tag: &str) -> Option<usize> {
    match tag {
        "g1" | "sg1" => Some(0),
        "g2" | "sg2" => Some(1),
        "g3" | "sg3" => Some(2),
        "g4" | "sg4" => Some(3),
        "g5" | "sg5" => Some(4),
        "g6" | "sg6" => Some(5),
        _ => None,
    }
}

fn regular_derisk_exit(tag: &str) -> bool {
    matches!(
        tag,
        "d" | "d1" | "dd0" | "ddl1" | "ddl2" | "dd1" | "dd2" | "dd3" | "dd4" | "dd5" | "dd6"
    )
}

fn regular_rebuy_entry_excluded(tag: &str) -> bool {
    matches!(
        tag,
        "g1" | "g2"
            | "g3"
            | "g4"
            | "g5"
            | "g6"
            | "sg1"
            | "sg2"
            | "sg3"
            | "sg4"
            | "sg5"
            | "sg6"
            | "dl1"
            | "dl2"
            | "gd1"
            | "gd2"
            | "gd3"
            | "gd4"
            | "gd5"
            | "gd6"
            | "gm0"
            | "gmd0"
    )
}

fn regular_rebuy_exit_excluded(tag: &str) -> bool {
    matches!(
        tag,
        "p" | "g1"
            | "g2"
            | "g3"
            | "g4"
            | "g5"
            | "g6"
            | "sg1"
            | "sg2"
            | "sg3"
            | "sg4"
            | "sg5"
            | "sg6"
            | "dl1"
            | "dl2"
            | "gd1"
            | "gd2"
            | "gd3"
            | "gd4"
            | "gd5"
            | "gd6"
            | "gm0"
            | "gmd0"
    )
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}
