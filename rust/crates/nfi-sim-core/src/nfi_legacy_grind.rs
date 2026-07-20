//! Exact spot/backtest state machine for NFI X7's legacy grind continuation.
//!
//! X7 reconstructs eight open grind clusters from filled orders on every
//! candle. This module preserves that reversed walk, callback branch order,
//! strict age comparisons, and stake conversion. Tag 120 starts here, while
//! tag 121 enters only after its regular-mode evaluator reports a de-risk.

use super::nfi_adjustment::evaluate_grind_entry_program;
use super::{
    adjustment_minimum_pair_stake, fee_close, fee_open, nfi_long_grind_supports_trade,
    AdjustmentSignal, Candle, FilledOrder, NfiLongGrindRoute, NfiX7TradeManager, OpenTrade,
    PairSeries, PortfolioConfig, TradeSide,
};

const TEN_MINUTES_MS: i64 = 10 * 60 * 1_000;
const SIX_HOURS_MS: i64 = 6 * 60 * 60 * 1_000;
const TWENTY_FOUR_HOURS_MS: i64 = 24 * 60 * 60 * 1_000;
const LEGACY_CLUSTER_COUNT: usize = 8;

#[derive(Debug, Default)]
struct LegacyCluster {
    count: usize,
    total_amount: f64,
    total_cost: f64,
    entry_ids: Vec<u64>,
    latest_entry_price: Option<f64>,
    open_rate: f64,
    profit_stake: f64,
    profit_rate: f64,
}

impl LegacyCluster {
    fn add_entry(&mut self, order: &FilledOrder) {
        self.count += 1;
        self.total_amount += order.amount;
        self.total_cost += order.amount * order.price;
        self.entry_ids.push(order.id);
        self.latest_entry_price.get_or_insert(order.price);
    }

    fn finish(&mut self, rate: f64, close_fee: f64) {
        if self.count == 0 {
            return;
        }
        self.open_rate = self.total_cost / self.total_amount;
        let current_stake = self.total_amount * rate * (1.0 - close_fee);
        self.profit_stake = current_stake - self.total_cost;
        self.profit_rate = (rate - self.open_rate) / self.open_rate;
    }

    fn latest_distance(&self, rate: f64) -> f64 {
        self.latest_entry_price
            .map_or(0.0, |price| (rate - price) / price)
    }
}

#[derive(Debug, Clone, Copy)]
struct OrderSnapshot {
    amount: f64,
    price: f64,
}

impl From<&FilledOrder> for OrderSnapshot {
    fn from(order: &FilledOrder) -> Self {
        Self {
            amount: order.amount,
            price: order.price,
        }
    }
}

#[derive(Debug)]
struct LegacyState {
    clusters: [LegacyCluster; LEGACY_CLUSTER_COUNT],
    is_derisk_1: bool,
    derisk_1_exit: Option<OrderSnapshot>,
    derisk_1_reentry: Option<OrderSnapshot>,
    first_entry: OrderSnapshot,
    latest_entry_price: f64,
    latest_entry_timestamp_ms: i64,
    latest_exit_price: Option<f64>,
    latest_order_price: f64,
    latest_order_timestamp_ms: i64,
}

struct LegacyContext<'a> {
    route: &'a NfiLongGrindRoute,
    trade: &'a OpenTrade,
    candle: &'a Candle,
    config: &'a PortfolioConfig,
    available_balance: f64,
    minimum_stake: f64,
    slice_amount: f64,
    current_stake_amount: f64,
    is_derisk: bool,
    is_long_grind_entry: bool,
    entry_age_allows: bool,
    maximum_stake_divisor: f64,
    mode: LegacyMode,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LegacyMode {
    Grind,
    RegularContinuation,
}

/// Evaluate the legacy part of `long_grind_adjust_trade_position()`.
///
/// The outer `Option` is the exactness boundary used by the simulator. `None`
/// rejects malformed or unreviewed state; the inner `None` is NFI's ordinary
/// callback no-op.
#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is unsupported state; inner None is callback no-op.
pub(super) fn evaluate_nfi_legacy_grind_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if config.is_futures
        || trade.side != TradeSide::Long
        || !nfi_long_grind_supports_trade(route, trade)
    {
        return None;
    }
    let grind_route = route.adjustment_scope == "spot-grind-backtest-v1" && route.grind_mode;
    let regular_continuation =
        route.adjustment_scope == "spot-regular-backtest-v1" && !route.grind_mode;
    if !grind_route && !regular_continuation {
        return None;
    }

    let minimum_stake = legacy_adjustment_minimum_stake(pair, candle, trade, config)?;
    let state = rebuild_legacy_state(trade, candle.open, fee_close(config))?;
    let stake_multipliers = &route.constants.stake_multipliers_spot;
    let first_multiplier = *stake_multipliers.first()?;
    if first_multiplier <= 0.0 || trade.amount <= 0.0 || trade.leverage <= 0.0 {
        return None;
    }

    // Freqtrade backtests only place filled adjustment orders in the trade
    // history. Their safe_remaining is zero, so NFI's live partial-fill retry
    // branch is structurally unreachable and no state is omitted here.
    if route.grind_mode {
        if let Some(signal) =
            evaluate_first_entry_recovery(route, trade, candle, config, minimum_stake, &state)?
        {
            return Some(Some(signal));
        }
    }

    let slice_amount = state.first_entry.amount * state.first_entry.price / first_multiplier;
    let slice_profit = price_distance(candle.open, state.latest_order_price)?;
    let slice_profit_entry = price_distance(candle.open, state.latest_entry_price)?;
    let slice_profit_exit = state
        .latest_exit_price
        .and_then(|price| price_distance(candle.open, price))
        .unwrap_or(0.0);
    let num_open_grinds = state
        .clusters
        .iter()
        .map(|cluster| cluster.count)
        .sum::<usize>();
    let is_long_grind_entry = evaluate_grind_entry_program(
        manager,
        &route.decision_program,
        trade,
        pair,
        candle_index,
        candle,
        num_open_grinds,
        slice_profit,
        slice_profit_entry,
        slice_profit_exit,
    )?;
    let latest_entry_is_old =
        candle.timestamp_ms - TEN_MINUTES_MS > state.latest_entry_timestamp_ms;
    let latest_order_is_forced_old =
        candle.timestamp_ms - TWENTY_FOUR_HOURS_MS > state.latest_order_timestamp_ms;
    let latest_order_is_old = candle.timestamp_ms - SIX_HOURS_MS > state.latest_order_timestamp_ms;
    let entry_age_allows = latest_entry_is_old
        && (latest_order_is_forced_old || slice_profit < -0.06)
        && (num_open_grinds == 0 || latest_order_is_old || slice_profit < -0.06);
    let context = LegacyContext {
        route,
        trade,
        candle,
        config,
        available_balance,
        minimum_stake,
        slice_amount,
        current_stake_amount: trade.amount * candle.open,
        is_derisk: trade.amount < state.first_entry.amount * 0.95,
        is_long_grind_entry,
        entry_age_allows,
        maximum_stake_divisor: if route.grind_mode {
            first_multiplier
        } else {
            1.0
        },
        mode: if route.grind_mode {
            LegacyMode::Grind
        } else {
            LegacyMode::RegularContinuation
        },
    };

    // The two post-de-risk clusters execute before the six ordinary grind
    // clusters in the source. A signal from an earlier cluster must prevent
    // every later branch from observing this candle.
    for index in [6_usize, 7] {
        if let Some(signal) = evaluate_cluster(&context, &state, index, true)? {
            return Some(Some(signal));
        }
    }
    for index in 0..6 {
        if let Some(signal) = evaluate_cluster(&context, &state, index, false)? {
            return Some(Some(signal));
        }
    }
    if let Some(signal) = evaluate_derisk_one_reentry(&context, &state, pair, candle_index)? {
        return Some(Some(signal));
    }
    Some(None)
}

fn rebuild_legacy_state(trade: &OpenTrade, rate: f64, close_fee: f64) -> Option<LegacyState> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_order = trade.orders.last()?;
    let latest_exit = trade.orders.iter().rev().find(|order| !order.is_entry);
    let mut clusters: [LegacyCluster; LEGACY_CLUSTER_COUNT] =
        std::array::from_fn(|_| LegacyCluster::default());
    let mut closed = [false; LEGACY_CLUSTER_COUNT];
    let mut is_derisk_1 = false;
    let mut derisk_1_exit = None;
    let mut derisk_1_reentry = None;

    // NFI walks newest-to-oldest. Exit tags close a cluster before older
    // entries are visited, and appended order IDs are emitted in this same
    // newest-first order.
    for order in trade.orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && order.id != first_entry.id {
            if tag == "d1" && !is_derisk_1 {
                derisk_1_reentry.get_or_insert_with(|| order.into());
            } else if let Some(index) = direct_entry_cluster(tag) {
                if !closed[index] {
                    clusters[index].add_entry(order);
                }
            } else if !closed[0] && !grind_one_entry_excluded(tag) {
                clusters[0].add_entry(order);
            }
            continue;
        }
        if order.is_entry {
            continue;
        }

        let head = tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = direct_exit_cluster(head) {
            closed[index] = true;
        } else if head == "d1" {
            if !is_derisk_1 {
                is_derisk_1 = true;
                derisk_1_exit = Some(order.into());
            }
        } else if closes_all_grinds(head) {
            closed.fill(true);
        } else if !grind_one_exit_excluded(head) {
            closed[0] = true;
        }
    }
    for cluster in &mut clusters {
        cluster.finish(rate, close_fee);
    }
    Some(LegacyState {
        clusters,
        is_derisk_1,
        derisk_1_exit,
        derisk_1_reentry,
        first_entry: first_entry.into(),
        latest_entry_price: latest_entry.price,
        latest_entry_timestamp_ms: latest_entry.filled_timestamp_ms,
        latest_exit_price: latest_exit.map(|order| order.price),
        latest_order_price: latest_order.price,
        latest_order_timestamp_ms: latest_order.filled_timestamp_ms,
    })
}

fn direct_entry_cluster(tag: &str) -> Option<usize> {
    match tag {
        "gd2" => Some(1),
        "gd3" => Some(2),
        "gd4" => Some(3),
        "gd5" => Some(4),
        "gd6" => Some(5),
        "dl1" => Some(6),
        "dl2" => Some(7),
        _ => None,
    }
}

fn direct_exit_cluster(tag: &str) -> Option<usize> {
    match tag {
        "gd2" | "dd2" => Some(1),
        "gd3" | "dd3" => Some(2),
        "gd4" | "dd4" => Some(3),
        "gd5" | "dd5" => Some(4),
        "gd6" | "dd6" => Some(5),
        "dl1" | "ddl1" => Some(6),
        "dl2" | "ddl2" => Some(7),
        _ => None,
    }
}

fn grind_one_entry_excluded(tag: &str) -> bool {
    matches!(
        tag,
        "r" | "d1"
            | "dl1"
            | "dl2"
            | "g1"
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
            | "gd2"
            | "gd3"
            | "gd4"
            | "gd5"
            | "gd6"
            | "gm0"
            | "gmd0"
            | "gdr"
    )
}

fn grind_one_exit_excluded(tag: &str) -> bool {
    matches!(
        tag,
        "dl1"
            | "ddl1"
            | "dl2"
            | "ddl2"
            | "g1"
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
            | "gd2"
            | "gd3"
            | "gd4"
            | "gd5"
            | "gd6"
            | "dd2"
            | "dd3"
            | "dd4"
            | "dd5"
            | "dd6"
            | "gm0"
            | "gmd0"
            | "gdr"
    )
}

fn closes_all_grinds(tag: &str) -> bool {
    matches!(
        tag,
        "p" | "r" | "d" | "dd0" | "partial_exit" | "force_exit" | ""
    )
}

#[allow(clippy::option_option)] // Preserve evaluator validity separately from callback no-op.
fn evaluate_first_entry_recovery(
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
    minimum_stake: f64,
    state: &LegacyState,
) -> Option<Option<AdjustmentSignal>> {
    let already_filled = trade
        .orders
        .iter()
        .filter(|order| !order.is_entry)
        .any(|order| {
            order
                .tag
                .as_deref()
                .and_then(|tag| tag.split_whitespace().next())
                .is_some_and(|tag| matches!(tag, "gm0" | "gmd0"))
        });
    if already_filled {
        return Some(None);
    }
    let original_stake_basis = state.first_entry.amount * (trade.stake_amount / trade.amount);
    if original_stake_basis - minimum_stake * 1.5 <= minimum_stake {
        return Some(None);
    }

    let distance = price_distance(candle.open, state.first_entry.price)?;
    let threshold = route.first_entry_profit_threshold_spot + fee_open(config) + fee_close(config);
    let tag = if distance > threshold {
        Some("gm0")
    } else if route.derisk_use_grind_stops && distance < route.first_entry_stop_threshold_spot {
        Some("gmd0")
    } else {
        None
    };
    let Some(tag) = tag else {
        return Some(None);
    };

    let requested_exit = state.first_entry.amount * candle.open / trade.leverage;
    let Some(stake_amount) =
        legacy_partial_exit_stake(trade, candle.open, minimum_stake, requested_exit)
    else {
        return Some(None);
    };
    Some(Some(AdjustmentSignal {
        stake_amount: -stake_amount,
        tag: order_id_tag(tag, &state.clusters[0].entry_ids),
    }))
}

#[allow(clippy::option_option)] // Preserve evaluator validity separately from callback no-op.
fn evaluate_cluster(
    context: &LegacyContext<'_>,
    state: &LegacyState,
    index: usize,
    post_derisk: bool,
) -> Option<Option<AdjustmentSignal>> {
    let definition = context.route.constants.clusters.get(index)?;
    let cluster = state.clusters.get(index)?;
    let stakes = &definition.stakes_spot;
    let thresholds = &definition.thresholds_spot;
    let scaled_stakes = scale_stakes_for_minimum(
        stakes,
        context.slice_amount,
        context.minimum_stake,
        1.0,
        context.trade.leverage,
    )?;
    let first_entry_condition = if post_derisk {
        context.is_derisk || context.mode == LegacyMode::RegularContinuation
    } else {
        context.is_derisk || context.route.grind_mode
    };
    let distance_allows = if cluster.count == 0 {
        first_entry_condition
    } else if cluster.count < scaled_stakes.len() {
        cluster.latest_distance(context.candle.open) < *thresholds.get(cluster.count)?
    } else {
        false
    };
    let route_allows = if post_derisk {
        state.is_derisk_1 && state.derisk_1_reentry.is_none()
    } else {
        true
    };
    let first_cost = state.first_entry.amount * state.first_entry.price;
    let below_maximum = context.current_stake_amount
        < first_cost * context.route.constants.max_stake_multiplier / context.maximum_stake_divisor;
    if route_allows
        && cluster.count < scaled_stakes.len()
        && distance_allows
        && context.entry_age_allows
        && context.is_long_grind_entry
        && below_maximum
    {
        let requested =
            (context.slice_amount * scaled_stakes[cluster.count]).max(context.minimum_stake * 1.5);
        if requested > context.available_balance {
            return Some(None);
        }
        return Some(Some(AdjustmentSignal {
            stake_amount: requested,
            tag: definition.entry_tag.clone(),
        }));
    }

    if cluster.count > 0
        && cluster.profit_rate
            > definition.profit_threshold_spot
                + fee_open(context.config)
                + fee_close(context.config)
    {
        let requested = cluster.total_amount * context.candle.open / context.trade.leverage;
        if let Some(stake_amount) = legacy_partial_exit_stake(
            context.trade,
            context.candle.open,
            context.minimum_stake,
            requested,
        ) {
            return Some(Some(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&definition.entry_tag, &cluster.entry_ids),
            }));
        }
    }

    let stop_condition = if post_derisk {
        context.is_derisk || context.mode == LegacyMode::RegularContinuation
    } else {
        context.is_derisk || context.route.grind_mode
    };
    if context.route.derisk_use_grind_stops
        && cluster.count > 0
        && cluster.profit_stake < context.slice_amount * definition.stop_threshold_spot
        && stop_condition
    {
        let requested = cluster.total_amount * context.candle.open / context.trade.leverage;
        if let Some(stake_amount) = legacy_partial_exit_stake(
            context.trade,
            context.candle.open,
            context.minimum_stake,
            requested,
        ) {
            return Some(Some(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&definition.stop_tag, &cluster.entry_ids),
            }));
        }
    }
    Some(None)
}

#[allow(clippy::option_option)] // Preserve evaluator validity separately from callback no-op.
fn evaluate_derisk_one_reentry(
    context: &LegacyContext<'_>,
    state: &LegacyState,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<Option<AdjustmentSignal>> {
    let threshold = context.route.constants.derisk_1_reentry_spot;
    if state.is_derisk_1 && state.derisk_1_reentry.is_none() {
        let exit = state.derisk_1_exit?;
        if price_distance(context.candle.open, exit.price)? < threshold
            && context.entry_age_allows
            && super::feature_bool_at(pair, candle_index, "global_protections_long_pump")?
            && super::feature_bool_at(pair, candle_index, "global_protections_long_dump")?
            && context.is_long_grind_entry
        {
            let requested = (exit.amount * exit.price).max(context.minimum_stake * 1.5);
            if requested > context.available_balance {
                return Some(None);
            }
            return Some(Some(AdjustmentSignal {
                stake_amount: requested,
                tag: "d1".to_owned(),
            }));
        }
    }

    let Some(reentry) = state.derisk_1_reentry else {
        return Some(None);
    };
    if price_distance(context.candle.open, reentry.price)? >= threshold / context.trade.leverage {
        return Some(None);
    }
    let requested = reentry.amount * context.candle.open / context.trade.leverage;
    let Some(stake_amount) = legacy_partial_exit_stake(
        context.trade,
        context.candle.open,
        context.minimum_stake,
        requested,
    ) else {
        return Some(None);
    };
    Some(Some(AdjustmentSignal {
        stake_amount: -stake_amount,
        tag: "d1".to_owned(),
    }))
}

fn legacy_adjustment_minimum_stake(
    pair: &PairSeries,
    candle: &Candle,
    _trade: &OpenTrade,
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
    stake_leverage: f64,
    trade_leverage: f64,
) -> Option<Vec<f64>> {
    let first = *stakes.first()?;
    if slice_amount <= 0.0 || first <= 0.0 || stake_leverage <= 0.0 {
        return None;
    }
    if slice_amount * first / stake_leverage >= minimum_stake {
        return Some(stakes.to_vec());
    }
    let multiplier = minimum_stake / slice_amount / first * trade_leverage;
    Some(stakes.iter().map(|stake| stake * multiplier).collect())
}

fn legacy_partial_exit_stake(
    trade: &OpenTrade,
    rate: f64,
    minimum_stake: f64,
    requested_exit: f64,
) -> Option<f64> {
    let remaining = trade.amount * rate / trade.leverage - requested_exit;
    let exit_amount = if remaining < minimum_stake * 1.55 {
        trade.amount * rate / trade.leverage - minimum_stake * 1.55
    } else {
        requested_exit
    };
    let ft_stake = exit_amount * trade.leverage * (trade.stake_amount / trade.amount) / rate;
    (exit_amount > minimum_stake && ft_stake > minimum_stake).then_some(ft_stake)
}

fn order_id_tag(prefix: &str, ids: &[u64]) -> String {
    ids.iter().fold(prefix.to_owned(), |mut tag, id| {
        tag.push(' ');
        tag.push_str(&id.to_string());
        tag
    })
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}
