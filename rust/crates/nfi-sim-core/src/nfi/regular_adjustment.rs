//! Exact backtest regular-mode adjustment used by NFI X7 tag 121.
//!
//! X7 sends tag 121 through `long_adjust_trade_position_no_derisk()` before
//! the legacy grind callback. The source rebuilds one rebuy bucket and its
//! grind clusters from filled orders on every candle. This module preserves
//! that newest-to-oldest order walk and the callback's early-return order.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::{fee_close, fee_open};
use crate::callbacks::insert_projected_feature_window;
use crate::domain::{
    AdjustmentSignal, Candle, CompiledOrderSequence, CompiledOrderSide,
    CompiledRegularAdjustmentProgram, CompiledRegularOrderScan, CompiledRegularTransition,
    FilledOrder, NfiLongGrindRoute, NfiRegularAdjustmentConstants, NfiRegularAdjustmentPolicy,
    NfiRegularGrind, NfiX7TradeManager, PairSeries, PortfolioConfig,
};
use crate::execution::adjustment_minimum_pair_stake;
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{evaluate_scalar_program_bundle, number_value, scalar_truthy};

use super::dispatch::nfi_long_grind_supports_trade;
use super::state::nfi_profit_snapshot;

/// The regular helper either returns from the outer callback or deliberately
/// transfers a de-risked trade to the legacy continuation below it.
pub(crate) enum RegularAdjustmentOutcome {
    Return(Option<AdjustmentSignal>),
    ContinueLegacy,
}

#[derive(Debug)]
struct RegularGrindContract<'a> {
    entry_tag: &'a str,
    stop_tag: &'a str,
    futures_fallback_loss_threshold: Option<f64>,
}

#[derive(Debug)]
enum RegularOrderContract<'a> {
    Compiled(&'a CompiledRegularOrderScan),
    ReviewedLegacy,
}

#[derive(Debug)]
struct RegularRuntimeContract<'a> {
    rebuy_tag: &'a str,
    grinds: Vec<RegularGrindContract<'a>>,
    derisk_tag: &'a str,
    derisk_level_one_tag: &'a str,
    order_scan: RegularOrderContract<'a>,
    continuation_amount_ratio: f64,
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
    grinds: Vec<RegularCluster>,
    is_derisk: bool,
    is_derisk_1: bool,
    first_entry_cost: f64,
    latest_entry_price: f64,
    latest_entry_timestamp_ms: i64,
    latest_order_price: f64,
    latest_order_timestamp_ms: i64,
}

fn compiled_runtime_contract<'a>(
    program: &'a CompiledRegularAdjustmentProgram,
    constants: &'a NfiRegularAdjustmentConstants,
) -> Option<RegularRuntimeContract<'a>> {
    let scan = &program.order_scan;
    (scan.sequence == CompiledOrderSequence::Reverse
        && scan.entry_order_side == CompiledOrderSide::Buy
        && scan.exit_order_side == CompiledOrderSide::Sell
        && scan.exclude_first_entry)
        .then_some(())?;
    let mut rebuy_tag = None;
    let mut grinds = Vec::with_capacity(constants.grinds.len());
    let mut derisk_tag = None;
    let mut derisk_level_one_tag = None;
    for transition in &program.source_order {
        match transition {
            CompiledRegularTransition::Rebuy { tag, .. } => {
                (rebuy_tag.is_none()).then_some(())?;
                rebuy_tag = Some(tag.as_str());
            }
            CompiledRegularTransition::Grind {
                level,
                entry_tag,
                stop_tag,
                futures_fallback_loss_threshold,
                ..
            } => {
                (*level == grinds.len() + 1).then_some(())?;
                grinds.push(RegularGrindContract {
                    entry_tag,
                    stop_tag,
                    futures_fallback_loss_threshold: *futures_fallback_loss_threshold,
                });
            }
            CompiledRegularTransition::Derisk { tag, level_one, .. } => {
                let slot = if *level_one {
                    &mut derisk_level_one_tag
                } else {
                    &mut derisk_tag
                };
                (slot.is_none()).then_some(())?;
                *slot = Some(tag.as_str());
            }
        }
    }
    (grinds.len() == constants.grinds.len()).then_some(())?;
    Some(RegularRuntimeContract {
        rebuy_tag: rebuy_tag?,
        grinds,
        derisk_tag: derisk_tag?,
        derisk_level_one_tag: derisk_level_one_tag?,
        order_scan: RegularOrderContract::Compiled(&program.order_scan),
        continuation_amount_ratio: program.continuation.amount_ratio,
    })
}

fn reviewed_legacy_contract<'a>(
    route: &'a NfiLongGrindRoute,
    constants: &'a NfiRegularAdjustmentConstants,
) -> RegularRuntimeContract<'a> {
    let grinds = constants
        .grinds
        .iter()
        .enumerate()
        .map(|(index, grind)| RegularGrindContract {
            entry_tag: &grind.entry_tag,
            stop_tag: &grind.stop_tag,
            futures_fallback_loss_threshold: (index == 0)
                .then_some(route.futures_fallback_loss_threshold)
                .flatten(),
        })
        .collect();
    RegularRuntimeContract {
        rebuy_tag: "r",
        grinds,
        derisk_tag: "d",
        derisk_level_one_tag: "d1",
        order_scan: RegularOrderContract::ReviewedLegacy,
        continuation_amount_ratio: 0.95,
    }
}

fn outcomes_match(
    left: Option<&RegularAdjustmentOutcome>,
    right: Option<&RegularAdjustmentOutcome>,
) -> bool {
    match (left, right) {
        (
            Some(RegularAdjustmentOutcome::ContinueLegacy),
            Some(RegularAdjustmentOutcome::ContinueLegacy),
        )
        | (
            Some(RegularAdjustmentOutcome::Return(None)),
            Some(RegularAdjustmentOutcome::Return(None)),
        ) => true,
        (
            Some(RegularAdjustmentOutcome::Return(Some(left))),
            Some(RegularAdjustmentOutcome::Return(Some(right))),
        ) => left.tag == right.tag && left.stake_amount.to_bits() == right.stake_amount.to_bits(),
        _ => false,
    }
}

/// Evaluate the source-bound tag-121 regular adjustment prelude.
///
/// `None` is reserved for an invalid or broader-than-certified input. A valid
/// callback no-op is represented by `Return(None)`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn evaluate_nfi_regular_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<RegularAdjustmentOutcome> {
    if trade.side != TradeSide::Long
        || route.grind_mode
        || route.adjustment_scope != "regular-backtest-v2"
        || !nfi_long_grind_supports_trade(route, trade)
    {
        return None;
    }
    let constants = route.regular_constants.as_ref()?;
    let program = route.regular_decision_program.as_deref()?;
    let legacy_contract = reviewed_legacy_contract(route, constants);
    let legacy = evaluate_regular_adjustment_with_contract(
        manager,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
        constants,
        program,
        &legacy_contract,
    );
    let Some(compiled) = route.regular_program.as_ref() else {
        return legacy;
    };
    let primary_contract = compiled_runtime_contract(compiled, constants)?;
    let primary = evaluate_regular_adjustment_with_contract(
        manager,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
        constants,
        program,
        &primary_contract,
    );
    outcomes_match(primary.as_ref(), legacy.as_ref()).then_some(primary?)
}

#[allow(clippy::too_many_arguments)]
fn evaluate_regular_adjustment_with_contract(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
    constants: &NfiRegularAdjustmentConstants,
    program: &str,
    contract: &RegularRuntimeContract<'_>,
) -> Option<RegularAdjustmentOutcome> {
    let minimum_stake = regular_adjustment_minimum_stake(pair, candle, config)?;
    let state = rebuild_regular_state(trade, candle.open, constants, contract)?;

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
        config.is_futures,
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
        config,
        available_balance,
        minimum_stake,
        snapshot.initial_stake_ratio,
        slice_profit,
        slice_profit_entry,
        entry_program_allows,
        &state,
        contract.rebuy_tag,
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
            contract.grinds.get(index)?,
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
        config,
        candle.open,
        minimum_stake,
        snapshot.stake,
        &state,
        contract.derisk_tag,
        contract.derisk_level_one_tag,
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
    config: &PortfolioConfig,
    available_balance: f64,
    minimum_stake: f64,
    initial_stake_ratio: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    entry_program_allows: bool,
    state: &RegularState,
    tag: &str,
) -> Option<BranchOutcome> {
    let cluster = &state.rebuy;
    let (stakes, thresholds) = if config.is_futures {
        (
            &constants.rebuy_stakes_futures,
            &constants.rebuy_thresholds_futures,
        )
    } else {
        (
            &constants.rebuy_stakes_spot,
            &constants.rebuy_thresholds_spot,
        )
    };
    if cluster.count >= stakes.len() {
        return Some(BranchOutcome::Continue);
    }
    let threshold = *thresholds.get(cluster.count)?;
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
        stakes,
        state.first_entry_cost,
        minimum_stake,
        trade.leverage,
    )?;
    let stake_leverage = if config.is_futures {
        trade.leverage
    } else {
        1.0
    };
    let requested = (state.first_entry_cost * scaled[cluster.count] / stake_leverage)
        .min(available_balance)
        .max(minimum_stake * policy.minimum_entry_multiplier);
    if requested > available_balance {
        return Some(BranchOutcome::ReturnNone);
    }
    Some(BranchOutcome::Signal(AdjustmentSignal {
        stake_amount: requested,
        tag: tag.to_owned(),
    }))
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)] // Preserve the callback's source-ordered branch priority.
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
    contract: &RegularGrindContract<'_>,
) -> Option<BranchOutcome> {
    let cluster = state.grinds.get(index)?;
    let (stakes, thresholds, profit_threshold, stop_threshold) = if config.is_futures {
        (
            &definition.stakes_futures,
            &definition.thresholds_futures,
            definition.profit_threshold_futures,
            definition.stop_threshold_futures,
        )
    } else {
        (
            &definition.stakes_spot,
            &definition.thresholds_spot,
            definition.profit_threshold_spot,
            definition.stop_threshold_spot,
        )
    };
    if cluster.count < stakes.len() {
        let threshold = *thresholds.get(cluster.count)?;
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
                stakes,
                state.first_entry_cost,
                minimum_stake,
                trade.leverage,
            )?;
            let stake_leverage = if config.is_futures {
                trade.leverage
            } else {
                1.0
            };
            let requested = (state.first_entry_cost * scaled[cluster.count] / stake_leverage)
                .max(minimum_stake * policy.minimum_entry_multiplier);
            if requested > available_balance {
                return Some(BranchOutcome::ReturnNone);
            }
            return Some(BranchOutcome::Signal(AdjustmentSignal {
                stake_amount: requested,
                tag: contract.entry_tag.to_owned(),
            }));
        }
    }

    if contract
        .futures_fallback_loss_threshold
        .is_some_and(|threshold| {
            config.is_futures
                && cluster.count < stakes.len()
                && slice_profit < threshold / trade.leverage
        })
    {
        let scaled = scale_stakes_for_minimum(
            stakes,
            state.first_entry_cost,
            minimum_stake,
            trade.leverage,
        )?;
        let requested = (state.first_entry_cost * scaled[cluster.count] / trade.leverage)
            .max(minimum_stake * policy.minimum_entry_multiplier);
        if requested > available_balance {
            return Some(BranchOutcome::ReturnNone);
        }
        return Some(BranchOutcome::Signal(AdjustmentSignal {
            stake_amount: requested,
            tag: contract.entry_tag.to_owned(),
        }));
    }

    if cluster.count > 0
        && cluster.profit_rate > profit_threshold + fee_open(config) + fee_close(config)
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
                tag: order_id_tag(contract.entry_tag, &cluster.entry_ids),
            }));
        }
    }

    if use_grind_stops && cluster.count > 0 && cluster.profit_rate < stop_threshold {
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
                tag: order_id_tag(contract.stop_tag, &cluster.entry_ids),
            }));
        }
    }
    Some(BranchOutcome::Continue)
}

fn rebuild_regular_state(
    trade: &OpenTrade,
    rate: f64,
    constants: &NfiRegularAdjustmentConstants,
    contract: &RegularRuntimeContract<'_>,
) -> Option<RegularState> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_order = trade.orders.last()?;
    let mut rebuy = RegularCluster::default();
    let mut grinds = (0..constants.grinds.len())
        .map(|_| RegularCluster::default())
        .collect::<Vec<_>>();
    let mut rebuy_closed = false;
    let mut grind_closed = vec![false; constants.grinds.len()];
    let mut is_derisk = false;
    let mut is_derisk_1 = false;

    for order in trade.orders.iter().rev() {
        let full_tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && order.id != first_entry.id {
            if let Some(index) = regular_grind_entry_index(full_tag, &contract.grinds) {
                if !grind_closed.get(index).copied()? {
                    grinds.get_mut(index)?.add_entry(order);
                }
            } else if !rebuy_closed && !contract.rebuy_entry_excluded(full_tag) {
                rebuy.add_entry(order);
            }
            continue;
        }
        if order.is_entry {
            continue;
        }

        let head = full_tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = regular_grind_exit_index(head, &contract.grinds) {
            *grind_closed.get_mut(index)? = true;
        } else if contract.derisk_exit(head) {
            is_derisk = true;
            is_derisk_1 |= head == contract.derisk_level_one_tag;
            grind_closed.fill(true);
            rebuy_closed = true;
        } else if !contract.rebuy_exit_excluded(head) {
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
                    if amount < first_entry.amount * contract.continuation_amount_ratio {
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

#[allow(clippy::too_many_arguments)] // The source branch needs trade, mode, state, and IR tags.
fn evaluate_derisk(
    constants: &NfiRegularAdjustmentConstants,
    trade: &OpenTrade,
    config: &PortfolioConfig,
    rate: f64,
    minimum_stake: f64,
    profit_stake: f64,
    state: &RegularState,
    tag: &str,
    level_one_tag: &str,
) -> Option<AdjustmentSignal> {
    if !constants.derisk_enable {
        return None;
    }
    let minimum_remaining_multiplier = constants.policy.minimum_remaining_multiplier;
    let (derisk_threshold, derisk_level_1_threshold) = if config.is_futures {
        (
            constants.derisk_threshold_futures,
            constants.derisk_level_1_threshold_futures,
        )
    } else {
        (
            constants.derisk_threshold_spot,
            constants.derisk_level_1_threshold_spot,
        )
    };
    let threshold_basis = state.first_entry_cost / trade.leverage;
    if profit_stake < threshold_basis * derisk_threshold {
        if let Some(signal) = derisk_signal(
            trade,
            rate,
            minimum_stake,
            minimum_remaining_multiplier,
            tag,
        ) {
            return Some(signal);
        }
    }
    if !state.is_derisk_1 && profit_stake < threshold_basis * derisk_level_1_threshold {
        return derisk_signal(
            trade,
            rate,
            minimum_stake,
            minimum_remaining_multiplier,
            level_one_tag,
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

impl RegularRuntimeContract<'_> {
    fn rebuy_entry_excluded(&self, tag: &str) -> bool {
        match &self.order_scan {
            RegularOrderContract::Compiled(scan) => scan
                .rebuy_entry_excluded_tags
                .iter()
                .any(|excluded| excluded == tag),
            RegularOrderContract::ReviewedLegacy => regular_rebuy_entry_excluded(tag),
        }
    }

    fn rebuy_exit_excluded(&self, tag: &str) -> bool {
        match &self.order_scan {
            RegularOrderContract::Compiled(scan) => scan
                .rebuy_exit_excluded_tags
                .iter()
                .any(|excluded| excluded == tag),
            RegularOrderContract::ReviewedLegacy => regular_rebuy_exit_excluded(tag),
        }
    }

    fn derisk_exit(&self, tag: &str) -> bool {
        match &self.order_scan {
            RegularOrderContract::Compiled(scan) => {
                scan.derisk_exit_tags.iter().any(|derisk| derisk == tag)
            }
            RegularOrderContract::ReviewedLegacy => regular_derisk_exit(tag),
        }
    }
}

fn regular_grind_entry_index(tag: &str, grinds: &[RegularGrindContract<'_>]) -> Option<usize> {
    grinds.iter().position(|grind| grind.entry_tag == tag)
}

fn regular_grind_exit_index(tag: &str, grinds: &[RegularGrindContract<'_>]) -> Option<usize> {
    grinds
        .iter()
        .position(|grind| grind.entry_tag == tag || grind.stop_tag == tag)
}

fn regular_derisk_exit(tag: &str) -> bool {
    matches!(tag, "d" | "d1" | "dd0")
        || structured_level_tag(tag, "dd").is_some()
        || structured_level_tag(tag, "ddl").is_some()
}

fn regular_rebuy_entry_excluded(tag: &str) -> bool {
    matches!(tag, "gm0" | "gmd0")
        || ["g", "sg", "dl", "gd"]
            .iter()
            .any(|prefix| structured_level_tag(tag, prefix).is_some())
}

fn regular_rebuy_exit_excluded(tag: &str) -> bool {
    tag == "p" || regular_rebuy_entry_excluded(tag)
}

fn structured_level_tag(tag: &str, prefix: &str) -> Option<usize> {
    let suffix = tag.strip_prefix(prefix)?;
    (!suffix.is_empty() && suffix.bytes().all(|byte| byte.is_ascii_digit()))
        .then(|| suffix.parse::<usize>().ok())
        .flatten()
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}
