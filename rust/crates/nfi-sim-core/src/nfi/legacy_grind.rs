//! Exact backtest state machine for NFI X7's legacy grind continuation.
//!
//! X7 reconstructs its open grind clusters from filled orders on every
//! candle. This module preserves that reversed walk, callback branch order,
//! strict age comparisons, and stake conversion. Tag 120 starts here, while
//! tag 121 enters only after its regular-mode evaluator reports a de-risk.

use super::adjustment::evaluate_grind_entry_program;
use super::dispatch::nfi_long_grind_supports_trade;
use crate::calculations::{fee_close, fee_open};
use crate::domain::{
    AdjustmentSignal, Candle, CompiledLegacyGrindProgram, CompiledLegacyGrindTransition,
    FilledOrder, NfiLegacyGrindCluster, NfiLongGrindRoute, NfiX7TradeManager, PairSeries,
    PortfolioConfig,
};
use crate::execution::adjustment_minimum_pair_stake;
use crate::portfolio::{OpenTrade, TradeSide};

const TEN_MINUTES_MS: i64 = 10 * 60 * 1_000;
const SIX_HOURS_MS: i64 = 6 * 60 * 60 * 1_000;
const TWENTY_FOUR_HOURS_MS: i64 = 24 * 60 * 60 * 1_000;
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
    clusters: Vec<LegacyCluster>,
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
    slice_profit: f64,
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

fn legacy_mode_for_route(
    route: &NfiLongGrindRoute,
    config: &PortfolioConfig,
) -> Option<LegacyMode> {
    match (route.adjustment_scope.as_str(), route.grind_mode) {
        ("grind-backtest-v2", true) => Some(LegacyMode::Grind),
        // Schema v1 artifacts remain readable, but their claim was
        // intentionally spot-only and must never be widened during replay.
        ("spot-grind-backtest-v1", true) if !config.is_futures => Some(LegacyMode::Grind),
        ("regular-backtest-v2", false) => Some(LegacyMode::RegularContinuation),
        _ => None,
    }
}

type LegacyClusterMode<'a> = (&'a [f64], &'a [f64], f64, f64, f64);

fn legacy_cluster_mode<'a>(
    definition: &'a NfiLegacyGrindCluster,
    config: &PortfolioConfig,
    trade_leverage: f64,
) -> LegacyClusterMode<'a> {
    if config.is_futures {
        (
            &definition.stakes_futures,
            &definition.thresholds_futures,
            definition.profit_threshold_futures,
            definition.stop_threshold_futures,
            trade_leverage,
        )
    } else {
        (
            &definition.stakes_spot,
            &definition.thresholds_spot,
            definition.profit_threshold_spot,
            definition.stop_threshold_spot,
            1.0,
        )
    }
}

/// Source-order result for one legacy grind cluster.
///
/// The strategy's callback distinguishes an unmatched cluster from a matched
/// entry that exceeds Freqtrade's current `max_stake`. The former continues to
/// the next cluster; the latter executes `return None` and must stop the whole
/// callback. Keeping those outcomes explicit prevents a smaller, later grind
/// from bypassing the wallet guard.
enum LegacyClusterOutcome {
    Continue,
    ReturnNone,
    Signal(AdjustmentSignal),
}

enum CompiledGrindOutcome {
    /// No compiled transition fired; the residual callback may still act.
    NoTransition,
    /// An earlier, intentionally uncompiled branch has precedence.
    ResidualPrecedes,
    /// The compiled prefix owns this callback result, including `return None`.
    Reached(Option<AdjustmentSignal>),
}

/// Evaluate the legacy part of `long_grind_adjust_trade_position()`.
///
/// The outer `Option` is the exactness boundary used by the simulator. `None`
/// rejects malformed or unreviewed state; the inner `None` is NFI's ordinary
/// callback no-op.
#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is unsupported state; inner None is callback no-op.
pub(crate) fn evaluate_nfi_legacy_grind_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    let legacy = evaluate_nfi_legacy_grind_shadow(
        manager,
        route,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )?;
    let Some(program) = route.program.as_ref() else {
        return Some(legacy);
    };
    match evaluate_compiled_grind(
        manager,
        route,
        program,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )? {
        CompiledGrindOutcome::ResidualPrecedes => Some(legacy),
        CompiledGrindOutcome::Reached(compiled) => {
            compiled_adjustments_match(compiled.as_ref(), legacy.as_ref()).then_some(legacy)
        }
        CompiledGrindOutcome::NoTransition => {
            let legacy_is_compiled = legacy.as_ref().is_some_and(|signal| {
                let head = signal.tag.split_whitespace().next().unwrap_or("");
                program
                    .source_order
                    .iter()
                    .any(|transition| compiled_transition_owns_tag(transition, head))
            });
            (!legacy_is_compiled).then_some(legacy)
        }
    }
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)]
fn evaluate_nfi_legacy_grind_shadow(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    if trade.side != TradeSide::Long || !nfi_long_grind_supports_trade(route, trade) {
        return None;
    }
    let mode = legacy_mode_for_route(route, config)?;

    let minimum_stake = legacy_adjustment_minimum_stake(pair, candle, trade, config)?;
    let state = rebuild_legacy_state(trade, candle.open, fee_close(config), route)?;
    let stake_multipliers = if config.is_futures {
        &route.constants.stake_multipliers_futures
    } else {
        &route.constants.stake_multipliers_spot
    };
    let first_multiplier = *stake_multipliers.first()?;
    if first_multiplier <= 0.0 || trade.amount <= 0.0 || trade.leverage <= 0.0 {
        return None;
    }

    // Freqtrade backtests only place filled adjustment orders in the trade
    // history. Their safe_remaining is zero, so NFI's live partial-fill retry
    // branch is structurally unreachable and no state is omitted here.
    if mode == LegacyMode::Grind {
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
        slice_profit,
        current_stake_amount: trade.amount * candle.open,
        is_derisk: trade.amount < state.first_entry.amount * 0.95,
        is_long_grind_entry,
        entry_age_allows,
        maximum_stake_divisor: if mode == LegacyMode::Grind {
            first_multiplier
        } else {
            1.0
        },
        mode,
    };

    // Post-de-risk clusters execute before ordinary grind clusters. The
    // extracted entry tags define membership, so adding levels does not
    // require changing an array size or numeric match arm.
    for post_derisk in [true, false] {
        for (index, definition) in route.constants.clusters.iter().enumerate() {
            if is_post_derisk_cluster(definition) != post_derisk {
                continue;
            }
            match evaluate_cluster(&context, &state, index, post_derisk)? {
                LegacyClusterOutcome::Continue => {}
                LegacyClusterOutcome::ReturnNone => return Some(None),
                LegacyClusterOutcome::Signal(signal) => return Some(Some(signal)),
            }
        }
    }
    if let Some(signal) = evaluate_derisk_one_reentry(&context, &state, pair, candle_index)? {
        return Some(Some(signal));
    }
    Some(None)
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
fn evaluate_compiled_grind(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    program: &CompiledLegacyGrindProgram,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<CompiledGrindOutcome> {
    if trade.side != TradeSide::Long
        || !route.grind_mode
        || !nfi_long_grind_supports_trade(route, trade)
    {
        return None;
    }
    let minimum_stake = legacy_adjustment_minimum_stake(pair, candle, trade, config)?;
    let state = rebuild_compiled_grind_state(trade, candle.open, fee_close(config), program)?;
    let stake_multipliers = if config.is_futures {
        &route.constants.stake_multipliers_futures
    } else {
        &route.constants.stake_multipliers_spot
    };
    let first_multiplier = *stake_multipliers.first()?;
    if first_multiplier <= 0.0 || trade.amount <= 0.0 || trade.leverage <= 0.0 {
        return None;
    }

    let first_transition = program.source_order.first()?;
    let (profit_tag, stop_action, append_entry_ids_from, profit_threshold) = match first_transition
    {
        CompiledLegacyGrindTransition::FirstEntryProfit {
            tag,
            append_entry_ids_from,
            profit_threshold,
            ..
        } => (tag, None, append_entry_ids_from, *profit_threshold),
        CompiledLegacyGrindTransition::FirstEntry {
            profit_tag,
            stop_tag,
            append_entry_ids_from,
            profit_threshold,
            stop_threshold,
            ..
        } => (
            profit_tag,
            Some((stop_tag, *stop_threshold)),
            append_entry_ids_from,
            *profit_threshold,
        ),
        CompiledLegacyGrindTransition::Cluster { .. } => return None,
    };
    let complete_program = program.schema_version == "grind-transition-program-v2";
    if complete_program != stop_action.is_some() {
        return None;
    };
    let first_entry_closed = trade
        .orders
        .iter()
        .filter(|order| !order.is_entry)
        .any(|order| {
            order
                .tag
                .as_deref()
                .and_then(|value| value.split_whitespace().next())
                .is_some_and(|value| {
                    program
                        .order_scan
                        .first_entry_closed_tags
                        .iter()
                        .any(|tag| tag == value)
                })
        });
    let original_stake_basis = state.first_entry.amount * (trade.stake_amount / trade.amount);
    let first_entry_has_room = original_stake_basis
        - minimum_stake * program.policy.minimum_entry_multiplier
        > minimum_stake;
    if !first_entry_closed && first_entry_has_room {
        let distance = price_distance(candle.open, state.first_entry.price)?;
        let requested_exit = state.first_entry.amount * candle.open / trade.leverage;
        let action_tag = if distance > profit_threshold + fee_open(config) + fee_close(config) {
            Some(profit_tag)
        } else if route.derisk_use_grind_stops
            && stop_action.is_some_and(|(_, threshold)| distance < threshold)
        {
            stop_action.map(|(tag, _)| tag)
        } else {
            None
        };
        if let Some(action_tag) = action_tag {
            if let Some(stake_amount) = compiled_partial_exit_stake(
                trade,
                candle.open,
                minimum_stake,
                requested_exit,
                program.policy.minimum_remaining_multiplier,
            ) {
                let cluster = compiled_cluster_by_tag(program, &state, append_entry_ids_from)?;
                return Some(CompiledGrindOutcome::Reached(Some(AdjustmentSignal {
                    stake_amount: -stake_amount,
                    tag: order_id_tag(action_tag, &cluster.entry_ids),
                })));
            }
        } else if !complete_program
            && route.derisk_use_grind_stops
            && distance < route.first_entry_stop_threshold_spot
            && compiled_partial_exit_stake(
                trade,
                candle.open,
                minimum_stake,
                requested_exit,
                program.policy.minimum_remaining_multiplier,
            )
            .is_some()
        {
            return Some(CompiledGrindOutcome::ResidualPrecedes);
        }
    }

    // Historical v1 programs left post-de-risk actions to the handwritten
    // implementation. V2 carries every cluster in source order below.
    if !complete_program
        && (state.is_derisk_1 && state.derisk_1_reentry.is_none()
            || program
                .order_scan
                .known_clusters
                .iter()
                .zip(&state.clusters)
                .any(|(definition, cluster)| definition.post_derisk && cluster.count > 0))
    {
        return Some(CompiledGrindOutcome::ResidualPrecedes);
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
    let entry_age_allows = candle.timestamp_ms - program.policy.entry_retry_ms
        > state.latest_entry_timestamp_ms
        && (candle.timestamp_ms - program.policy.force_order_age_ms
            > state.latest_order_timestamp_ms
            || slice_profit < program.policy.forced_entry_loss_gate)
        && (num_open_grinds == 0
            || candle.timestamp_ms - program.policy.order_age_ms > state.latest_order_timestamp_ms
            || slice_profit < program.policy.forced_entry_loss_gate);
    let is_derisk = trade.amount < state.first_entry.amount * program.policy.derisk_amount_ratio;
    let current_stake_amount = trade.amount * candle.open;
    let first_cost = state.first_entry.amount * state.first_entry.price;
    let below_maximum =
        current_stake_amount < first_cost * route.constants.max_stake_multiplier / first_multiplier;

    let cluster_transitions = program.source_order.iter().skip(1);
    for (source_index, transition) in cluster_transitions.enumerate() {
        let CompiledLegacyGrindTransition::Cluster {
            entry_tag,
            stop_tag,
            post_derisk,
            futures_fallback_loss_threshold,
            ..
        } = transition
        else {
            return None;
        };
        let compiled_index = program
            .order_scan
            .known_clusters
            .iter()
            .position(|cluster| cluster.entry_tag == *entry_tag && cluster.stop_tag == *stop_tag)?;
        let definition = route
            .constants
            .clusters
            .iter()
            .find(|cluster| cluster.entry_tag == *entry_tag && cluster.stop_tag == *stop_tag)?;
        let cluster = state.clusters.get(compiled_index)?;
        if *post_derisk
            != program
                .order_scan
                .known_clusters
                .get(compiled_index)?
                .post_derisk
        {
            return None;
        }
        let (stakes, thresholds, profit_threshold, stop_threshold, stake_leverage) =
            legacy_cluster_mode(definition, config, trade.leverage);
        let scaled_stakes = scale_stakes_for_minimum(
            stakes,
            slice_amount,
            minimum_stake,
            stake_leverage,
            trade.leverage,
        )?;
        let first_entry_condition = if *post_derisk {
            is_derisk
        } else {
            is_derisk || route.grind_mode
        };
        let distance_allows = if cluster.count == 0 {
            first_entry_condition
        } else if cluster.count < scaled_stakes.len() {
            cluster.latest_distance(candle.open) < *thresholds.get(cluster.count)?
        } else {
            false
        };
        let route_allows = if *post_derisk {
            state.is_derisk_1 && state.derisk_1_reentry.is_none()
        } else {
            true
        };
        if route_allows
            && cluster.count < scaled_stakes.len()
            && distance_allows
            && entry_age_allows
            && is_long_grind_entry
            && below_maximum
        {
            let requested = (slice_amount * scaled_stakes[cluster.count] / stake_leverage)
                .max(minimum_stake * program.policy.minimum_entry_multiplier);
            return Some(CompiledGrindOutcome::Reached(
                (requested <= available_balance).then(|| AdjustmentSignal {
                    stake_amount: requested,
                    tag: entry_tag.clone(),
                }),
            ));
        }

        if futures_fallback_loss_threshold.is_some_and(|threshold| {
            config.is_futures
                && first_entry_condition
                && cluster.count < scaled_stakes.len()
                && slice_profit < threshold / trade.leverage
        }) {
            let requested = (slice_amount * scaled_stakes[cluster.count] / stake_leverage)
                .max(minimum_stake * program.policy.minimum_entry_multiplier);
            return Some(CompiledGrindOutcome::Reached(
                (requested <= available_balance).then(|| AdjustmentSignal {
                    stake_amount: requested,
                    tag: entry_tag.clone(),
                }),
            ));
        }
        // Historical v1 programs left this source-ordered branch to the
        // handwritten shadow. The index applies only to that sealed schema.
        if !complete_program
            && source_index == 0
            && config.is_futures
            && (is_derisk || route.grind_mode)
            && cluster.count < scaled_stakes.len()
            && route
                .futures_fallback_loss_threshold
                .is_some_and(|threshold| slice_profit < threshold / trade.leverage)
        {
            return Some(CompiledGrindOutcome::ResidualPrecedes);
        }
        if cluster.count > 0
            && cluster.profit_rate > profit_threshold + fee_open(config) + fee_close(config)
        {
            let requested = cluster.total_amount * candle.open / trade.leverage;
            if let Some(stake_amount) = compiled_partial_exit_stake(
                trade,
                candle.open,
                minimum_stake,
                requested,
                program.policy.minimum_remaining_multiplier,
            ) {
                return Some(CompiledGrindOutcome::Reached(Some(AdjustmentSignal {
                    stake_amount: -stake_amount,
                    tag: order_id_tag(entry_tag, &cluster.entry_ids),
                })));
            }
        }
        if route.derisk_use_grind_stops
            && cluster.count > 0
            && cluster.profit_stake < slice_amount * stop_threshold
            && (is_derisk || route.grind_mode)
        {
            if let Some(stake_amount) = compiled_partial_exit_stake(
                trade,
                candle.open,
                minimum_stake,
                cluster.total_amount * candle.open / trade.leverage,
                program.policy.minimum_remaining_multiplier,
            ) {
                if complete_program {
                    return Some(CompiledGrindOutcome::Reached(Some(AdjustmentSignal {
                        stake_amount: -stake_amount,
                        tag: order_id_tag(stop_tag, &cluster.entry_ids),
                    })));
                }
                return Some(CompiledGrindOutcome::ResidualPrecedes);
            }
        }
    }
    Some(CompiledGrindOutcome::NoTransition)
}

fn rebuild_compiled_grind_state(
    trade: &OpenTrade,
    rate: f64,
    close_fee: f64,
    program: &CompiledLegacyGrindProgram,
) -> Option<LegacyState> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_order = trade.orders.last()?;
    let latest_exit = trade.orders.iter().rev().find(|order| !order.is_entry);
    let first_ordinary = program
        .order_scan
        .known_clusters
        .iter()
        .position(|cluster| !cluster.post_derisk)?;
    let mut clusters = (0..program.order_scan.known_clusters.len())
        .map(|_| LegacyCluster::default())
        .collect::<Vec<_>>();
    let mut closed = vec![false; clusters.len()];
    let mut is_derisk_1 = false;
    let mut derisk_1_exit = None;
    let mut derisk_1_reentry = None;

    for order in trade.orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && (!program.order_scan.exclude_first_entry || order.id != first_entry.id)
        {
            if tag == program.order_scan.derisk_entry_tag && !is_derisk_1 {
                derisk_1_reentry.get_or_insert_with(|| order.into());
            } else if let Some(index) = program
                .order_scan
                .known_clusters
                .iter()
                .position(|cluster| cluster.entry_tag == tag)
            {
                if !closed.get(index).copied()? {
                    clusters.get_mut(index)?.add_entry(order);
                }
            } else if !closed.get(first_ordinary).copied()?
                && !program
                    .order_scan
                    .level_one_entry_excluded_tags
                    .iter()
                    .any(|excluded| excluded == tag)
            {
                clusters.get_mut(first_ordinary)?.add_entry(order);
            }
            continue;
        }
        if order.is_entry {
            continue;
        }
        let head = tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = program
            .order_scan
            .known_clusters
            .iter()
            .position(|cluster| cluster.entry_tag == head || cluster.stop_tag == head)
        {
            *closed.get_mut(index)? = true;
        } else if head == program.order_scan.derisk_entry_tag {
            if !is_derisk_1 {
                is_derisk_1 = true;
                derisk_1_exit = Some(order.into());
            }
        } else if program
            .order_scan
            .close_all_exit_tags
            .iter()
            .any(|closed_tag| closed_tag == head)
        {
            closed.fill(true);
        } else if !program
            .order_scan
            .level_one_exit_excluded_tags
            .iter()
            .any(|excluded| excluded == head)
        {
            *closed.get_mut(first_ordinary)? = true;
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

fn compiled_cluster_by_tag<'a>(
    program: &CompiledLegacyGrindProgram,
    state: &'a LegacyState,
    tag: &str,
) -> Option<&'a LegacyCluster> {
    let index = program
        .order_scan
        .known_clusters
        .iter()
        .position(|cluster| cluster.entry_tag == tag)?;
    state.clusters.get(index)
}

fn compiled_partial_exit_stake(
    trade: &OpenTrade,
    rate: f64,
    minimum_stake: f64,
    requested_exit: f64,
    minimum_remaining_multiplier: f64,
) -> Option<f64> {
    let minimum_remaining = minimum_stake * minimum_remaining_multiplier;
    let remaining = trade.amount * rate / trade.leverage - requested_exit;
    let exit_amount = if remaining < minimum_remaining {
        trade.amount * rate / trade.leverage - minimum_remaining
    } else {
        requested_exit
    };
    let ft_stake = exit_amount * trade.leverage * (trade.stake_amount / trade.amount) / rate;
    (exit_amount > minimum_stake && ft_stake > minimum_stake).then_some(ft_stake)
}

fn compiled_adjustments_match(
    compiled: Option<&AdjustmentSignal>,
    legacy: Option<&AdjustmentSignal>,
) -> bool {
    match (compiled, legacy) {
        (None, None) => true,
        (Some(compiled), Some(legacy)) => {
            compiled.stake_amount.to_bits() == legacy.stake_amount.to_bits()
                && compiled.tag == legacy.tag
        }
        _ => false,
    }
}

fn compiled_transition_owns_tag(transition: &CompiledLegacyGrindTransition, tag: &str) -> bool {
    match transition {
        CompiledLegacyGrindTransition::FirstEntryProfit {
            tag: profit_tag, ..
        } => profit_tag == tag,
        CompiledLegacyGrindTransition::FirstEntry {
            profit_tag,
            stop_tag,
            ..
        } => profit_tag == tag || stop_tag == tag,
        CompiledLegacyGrindTransition::Cluster {
            entry_tag,
            stop_tag,
            ..
        } => entry_tag == tag || stop_tag == tag,
    }
}

fn rebuild_legacy_state(
    trade: &OpenTrade,
    rate: f64,
    close_fee: f64,
    route: &NfiLongGrindRoute,
) -> Option<LegacyState> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let latest_entry = trade.orders.iter().rev().find(|order| order.is_entry)?;
    let latest_order = trade.orders.last()?;
    let latest_exit = trade.orders.iter().rev().find(|order| !order.is_entry);
    let mut clusters = (0..route.constants.clusters.len())
        .map(|_| LegacyCluster::default())
        .collect::<Vec<_>>();
    let mut closed = vec![false; route.constants.clusters.len()];
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
            } else if let Some(index) = direct_entry_cluster(tag, &route.constants.clusters) {
                if !closed.get(index).copied()? {
                    clusters.get_mut(index)?.add_entry(order);
                }
            } else if !closed.first().copied()?
                && !grind_one_entry_excluded(tag, &route.constants.clusters)
            {
                clusters.first_mut()?.add_entry(order);
            }
            continue;
        }
        if order.is_entry {
            continue;
        }

        let head = tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = direct_exit_cluster(head, &route.constants.clusters) {
            *closed.get_mut(index)? = true;
        } else if head == "d1" {
            if !is_derisk_1 {
                is_derisk_1 = true;
                derisk_1_exit = Some(order.into());
            }
        } else if closes_all_grinds(head) {
            closed.fill(true);
        } else if !grind_one_exit_excluded(head, &route.constants.clusters) {
            *closed.first_mut()? = true;
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

fn direct_entry_cluster(tag: &str, clusters: &[NfiLegacyGrindCluster]) -> Option<usize> {
    clusters.iter().position(|cluster| cluster.entry_tag == tag)
}

fn direct_exit_cluster(tag: &str, clusters: &[NfiLegacyGrindCluster]) -> Option<usize> {
    clusters
        .iter()
        .position(|cluster| cluster.entry_tag == tag || cluster.stop_tag == tag)
}

fn grind_one_entry_excluded(tag: &str, clusters: &[NfiLegacyGrindCluster]) -> bool {
    matches!(tag, "r" | "d1" | "gm0" | "gmd0" | "gdr")
        || clusters
            .iter()
            .any(|cluster| cluster.entry_tag == tag || cluster.stop_tag == tag)
        || ["g", "sg"]
            .iter()
            .any(|prefix| structured_level_tag(tag, prefix).is_some())
}

fn grind_one_exit_excluded(tag: &str, clusters: &[NfiLegacyGrindCluster]) -> bool {
    matches!(tag, "gm0" | "gmd0" | "gdr")
        || clusters
            .iter()
            .any(|cluster| cluster.entry_tag == tag || cluster.stop_tag == tag)
        || ["g", "sg"]
            .iter()
            .any(|prefix| structured_level_tag(tag, prefix).is_some())
}

fn is_post_derisk_cluster(cluster: &NfiLegacyGrindCluster) -> bool {
    structured_level_tag(&cluster.entry_tag, "dl").is_some()
}

fn structured_level_tag(tag: &str, prefix: &str) -> Option<usize> {
    let suffix = tag.strip_prefix(prefix)?;
    (!suffix.is_empty() && suffix.bytes().all(|byte| byte.is_ascii_digit()))
        .then(|| suffix.parse::<usize>().ok())
        .flatten()
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

fn evaluate_cluster(
    context: &LegacyContext<'_>,
    state: &LegacyState,
    index: usize,
    post_derisk: bool,
) -> Option<LegacyClusterOutcome> {
    let definition = context.route.constants.clusters.get(index)?;
    let cluster = state.clusters.get(index)?;
    let (stakes, thresholds, profit_threshold, stop_threshold, stake_leverage) =
        legacy_cluster_mode(definition, context.config, context.trade.leverage);
    let scaled_stakes = scale_stakes_for_minimum(
        stakes,
        context.slice_amount,
        context.minimum_stake,
        stake_leverage,
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
        let requested = (context.slice_amount * scaled_stakes[cluster.count] / stake_leverage)
            .max(context.minimum_stake * 1.5);
        if requested > context.available_balance {
            return Some(LegacyClusterOutcome::ReturnNone);
        }
        return Some(LegacyClusterOutcome::Signal(AdjustmentSignal {
            stake_amount: requested,
            tag: definition.entry_tag.clone(),
        }));
    }

    // X7 places this futures-only gd1 branch immediately after the ordinary
    // grind-1 entry and before its exit/stop checks. It is intentionally a
    // drawdown fallback: indicator, distance, age, and maximum-position gates
    // are bypassed, while the wallet maximum still returns None from the
    // callback. The threshold is extracted from the strategy source into IR.
    if let Some(outcome) = evaluate_futures_drawdown_fallback(
        context,
        definition,
        cluster,
        &scaled_stakes,
        stake_leverage,
        index,
    ) {
        return Some(outcome);
    }

    if cluster.count > 0
        && cluster.profit_rate
            > profit_threshold + fee_open(context.config) + fee_close(context.config)
    {
        let requested = cluster.total_amount * context.candle.open / context.trade.leverage;
        if let Some(stake_amount) = legacy_partial_exit_stake(
            context.trade,
            context.candle.open,
            context.minimum_stake,
            requested,
        ) {
            return Some(LegacyClusterOutcome::Signal(AdjustmentSignal {
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
        && cluster.profit_stake < context.slice_amount * stop_threshold
        && stop_condition
    {
        let requested = cluster.total_amount * context.candle.open / context.trade.leverage;
        if let Some(stake_amount) = legacy_partial_exit_stake(
            context.trade,
            context.candle.open,
            context.minimum_stake,
            requested,
        ) {
            return Some(LegacyClusterOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&definition.stop_tag, &cluster.entry_ids),
            }));
        }
    }
    Some(LegacyClusterOutcome::Continue)
}

fn evaluate_futures_drawdown_fallback(
    context: &LegacyContext<'_>,
    definition: &NfiLegacyGrindCluster,
    cluster: &LegacyCluster,
    scaled_stakes: &[f64],
    stake_leverage: f64,
    index: usize,
) -> Option<LegacyClusterOutcome> {
    let threshold = context.route.futures_fallback_loss_threshold?;
    if index != 0
        || !context.config.is_futures
        || !(context.is_derisk || context.route.grind_mode)
        || cluster.count >= scaled_stakes.len()
        || context.slice_profit >= threshold / context.trade.leverage
    {
        return None;
    }
    let requested = (context.slice_amount * scaled_stakes[cluster.count] / stake_leverage)
        .max(context.minimum_stake * 1.5);
    if requested > context.available_balance {
        return Some(LegacyClusterOutcome::ReturnNone);
    }
    Some(LegacyClusterOutcome::Signal(AdjustmentSignal {
        stake_amount: requested,
        tag: definition.entry_tag.clone(),
    }))
}

#[allow(clippy::option_option)] // Preserve evaluator validity separately from callback no-op.
fn evaluate_derisk_one_reentry(
    context: &LegacyContext<'_>,
    state: &LegacyState,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<Option<AdjustmentSignal>> {
    let threshold = if context.config.is_futures {
        context.route.constants.derisk_1_reentry_futures
    } else {
        context.route.constants.derisk_1_reentry_spot
    };
    if state.is_derisk_1 && state.derisk_1_reentry.is_none() {
        let exit = state.derisk_1_exit?;
        if price_distance(context.candle.open, exit.price)? < threshold
            && context.entry_age_allows
            && crate::callbacks::feature_bool_at(
                pair,
                candle_index,
                "global_protections_long_pump",
            )?
            && crate::callbacks::feature_bool_at(
                pair,
                candle_index,
                "global_protections_long_dump",
            )?
            && context.is_long_grind_entry
        {
            let stake_leverage = if context.config.is_futures {
                context.trade.leverage
            } else {
                1.0
            };
            let requested =
                (exit.amount * exit.price / stake_leverage).max(context.minimum_stake * 1.5);
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
