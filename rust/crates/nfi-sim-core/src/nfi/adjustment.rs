//! Exact NFI X7 system-v3.2 position adjustment for managed long routes.
//!
//! The strategy intentionally reconstructs each open grind cluster from filled
//! orders on every candle. The engine derives the same immutable projection,
//! then caches it until an adjustment appends an order. Price-dependent profit
//! values are still recomputed every candle, so the cache changes no callback
//! input or source-order decision.

use std::collections::BTreeMap;
use std::sync::Arc;

use serde_json::Value;

use crate::calculations::{fee_close, fee_open};
use crate::callbacks::{
    feature_number_at, insert_projected_feature_window, scalar_program_feature_projection,
    scalar_trade_value,
};
use crate::domain::{
    AdjustmentSignal, Candle, CompiledOrderSide, CompiledSystemAdjustmentAction,
    CompiledSystemAdjustmentExecutionMode, CompiledSystemAdjustmentInputKind,
    CompiledSystemAdjustmentProgram, CompiledSystemAdjustmentSide, CompiledSystemGrindTags,
    CompiledSystemStakeScale, NfiX7AdjustmentComparison, NfiX7AdjustmentCondition,
    NfiX7AdjustmentExpression, NfiX7AdjustmentOperand, NfiX7AdjustmentPredicate, NfiX7GrindLevel,
    NfiX7PositionAdjustment, NfiX7TradeManager, OrderSide, PairSeries, PortfolioConfig,
};
use crate::execution::adjustment_minimum_pair_stake;
use crate::order_aggregates::FilledOrderSelector;
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{
    evaluate_scalar_decision_program, evaluate_scalar_program_bundle, number_value, scalar_truthy,
};

use super::state::{nfi_profit_snapshot, NfiProfitSnapshot, PositionAdjustmentRequest};

#[derive(Debug, Clone, Default)]
struct GrindCluster {
    count: usize,
    total_amount: f64,
    total_cost: f64,
    entry_ids: Vec<u64>,
    latest_entry_price: Option<f64>,
    exit_price: Option<f64>,
}

impl GrindCluster {
    fn profit_stake(&self, rate: f64, close_fee: f64, side: TradeSide) -> f64 {
        let current_stake = self.total_amount * rate * (1.0 - close_fee);
        match side {
            TradeSide::Long => current_stake - self.total_cost,
            TradeSide::Short => self.total_cost - current_stake,
        }
    }

    fn profit_rate(&self, rate: f64) -> f64 {
        if self.count == 0 {
            return 0.0;
        }
        let open_rate = self.total_cost / self.total_amount;
        (rate - open_rate) / open_rate
    }

    fn directional_distance(&self, rate: f64, side: TradeSide) -> f64 {
        self.latest_entry_price
            .map_or(0.0, |price| directional_rate((rate - price) / price, side))
    }
}

#[derive(Debug, Clone)]
pub(crate) struct AdjustmentState {
    order_count: usize,
    /// A compiled state may only be reused by the exact structural program.
    /// `None` identifies the independently reconstructed legacy shadow.
    program_fingerprint: Option<String>,
    clusters: Vec<GrindCluster>,
    derisk_found: Vec<bool>,
    first_entry_amount: f64,
    first_entry_cost: f64,
    latest_entry_price: f64,
    latest_entry_timestamp_ms: i64,
    latest_exit_price: Option<f64>,
    latest_order_price: f64,
    latest_order_timestamp_ms: i64,
}

struct AdjustmentContext<'a> {
    adjustment: &'a NfiX7PositionAdjustment,
    pair: &'a PairSeries,
    candle_index: usize,
    candle: &'a Candle,
    config: &'a PortfolioConfig,
    available_balance: f64,
    minimum_stake: f64,
    snapshot: NfiProfitSnapshot,
    slice_amount: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    current_stake_amount: f64,
    rebuy_mode: bool,
    is_grind_entry: bool,
    extra_entry_checks: bool,
}

/// Source-order result for one grind level.
///
/// NFI has two different no-order paths which must not be collapsed:
/// `Continue` means this grind level did not match and evaluation may proceed
/// to the next level. `ReturnNone` models an explicit strategy `return None`
/// (notably when a matched entry is larger than Freqtrade's `max_stake`) and
/// must stop the callback immediately.
enum GrindLevelOutcome {
    Continue,
    ReturnNone,
    Signal(AdjustmentSignal),
}

/// Evaluate the source-bound adjustment callback.
///
/// The outer `Option` is the evaluator validity boundary. The inner `Option`
/// is the callback's ordinary `None` result.
#[allow(clippy::option_option)] // Outer None is invalid IR; inner None is callback no-op.
pub(crate) fn evaluate_nfi_position_adjustment(
    manager: &NfiX7TradeManager,
    adjustment: &NfiX7PositionAdjustment,
    expected_side: TradeSide,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
    initial_stake_multiplier: f64,
    rebuy_mode: bool,
) -> Option<Option<AdjustmentSignal>> {
    let Some(program) = adjustment.program.as_ref() else {
        return evaluate_nfi_position_adjustment_legacy(
            manager,
            adjustment,
            expected_side,
            trade,
            request,
            initial_stake_multiplier,
            rebuy_mode,
        );
    };
    if program.execution_mode == CompiledSystemAdjustmentExecutionMode::Primary {
        return evaluate_compiled_system_adjustment(
            manager,
            adjustment,
            program,
            expected_side,
            trade,
            request,
            initial_stake_multiplier,
            rebuy_mode,
        );
    }
    let mut primary_trade = trade.clone();
    let primary = evaluate_compiled_system_adjustment(
        manager,
        adjustment,
        program,
        expected_side,
        &mut primary_trade,
        request,
        initial_stake_multiplier,
        rebuy_mode,
    )?;
    let mut shadow_trade = trade.clone();
    let shadow = evaluate_nfi_position_adjustment_legacy(
        manager,
        adjustment,
        expected_side,
        &mut shadow_trade,
        request,
        initial_stake_multiplier,
        rebuy_mode,
    )?;
    if !same_adjustment(primary.as_ref(), shadow.as_ref())
        || primary_trade.custom_data != shadow_trade.custom_data
    {
        return None;
    }
    trade.custom_data = primary_trade.custom_data;
    trade.nfi_adjustment_state = primary_trade.nfi_adjustment_state;
    Some(primary)
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None is invalid IR; inner None is callback no-op.
fn evaluate_nfi_position_adjustment_legacy(
    manager: &NfiX7TradeManager,
    adjustment: &NfiX7PositionAdjustment,
    expected_side: TradeSide,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
    initial_stake_multiplier: f64,
    rebuy_mode: bool,
) -> Option<Option<AdjustmentSignal>> {
    if !adjustment.enabled {
        return Some(None);
    }
    if trade.side != expected_side {
        return None;
    }
    // Rebuy tags deliberately live outside the regular adjustment tag set.
    // The route dispatcher validates the source's rebuy predicate before it
    // sets `rebuy_mode`, and X7 then transfers that same trade into the shared
    // grind callback after its first level-3 de-risk. Reapplying the regular
    // tag predicate here would reject every valid rebuy-to-grind transfer.
    // Non-rebuy calls still require the ordinary adjustment tag contract.
    if !rebuy_mode && !nfi_adjustment_supports_trade(adjustment, trade) {
        return None;
    }
    if trade.custom_data.get("system_version")?.as_str()? != adjustment.system_version {
        return None;
    }

    let state = legacy_adjustment_state(trade, adjustment)?;
    let result = evaluate_nfi_position_adjustment_with_state(
        manager,
        adjustment,
        trade,
        &state,
        request,
        initial_stake_multiplier,
        rebuy_mode,
    );
    trade.nfi_adjustment_state = Some(state);
    result
}

fn same_adjustment(left: Option<&AdjustmentSignal>, right: Option<&AdjustmentSignal>) -> bool {
    match (left, right) {
        (None, None) => true,
        (Some(left), Some(right)) => {
            left.stake_amount.to_bits() == right.stake_amount.to_bits() && left.tag == right.tag
        }
        _ => false,
    }
}

enum CompiledActionOutcome {
    Continue,
    ReturnNone,
    Signal(AdjustmentSignal),
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)] // Setup mirrors the complete callback-visible context.
#[allow(clippy::option_option)] // Preserve compiled-program validity separately from no-op.
fn evaluate_compiled_system_adjustment(
    manager: &NfiX7TradeManager,
    adjustment: &NfiX7PositionAdjustment,
    program: &CompiledSystemAdjustmentProgram,
    expected_side: TradeSide,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
    initial_stake_multiplier: f64,
    rebuy_mode: bool,
) -> Option<Option<AdjustmentSignal>> {
    let program_side = match expected_side {
        TradeSide::Long => CompiledSystemAdjustmentSide::Long,
        TradeSide::Short => CompiledSystemAdjustmentSide::Short,
    };
    if !matches!(
        program.execution_mode,
        CompiledSystemAdjustmentExecutionMode::Primary
            | CompiledSystemAdjustmentExecutionMode::PrimaryWithLegacyShadow
    ) || program.side != program_side
        || trade.side != expected_side
    {
        return None;
    }
    if !adjustment.enabled {
        return Some(None);
    }
    if !rebuy_mode && !nfi_adjustment_supports_trade(adjustment, trade) {
        return None;
    }
    if trade.custom_data.get("system_version")?.as_str()? != adjustment.system_version {
        return None;
    }
    if !initial_stake_multiplier.is_finite() || initial_stake_multiplier <= 0.0 {
        return None;
    }
    let state = compiled_adjustment_state(trade, program)?;
    let exchange_minimum_stake =
        adjustment_minimum_stake(request.pair, request.candle, trade, request.config)?;
    let minimum_stake =
        grind_callback_minimum_stake(exchange_minimum_stake, trade.leverage, rebuy_mode);
    let available_balance =
        grind_callback_maximum_stake(request.available_balance, trade.leverage, rebuy_mode);
    let snapshot = nfi_profit_snapshot(
        trade,
        request.candle.open,
        fee_open(request.config),
        fee_close(request.config),
        request.config.is_futures,
    )?;
    let slice_amount = state.first_entry_cost / initial_stake_multiplier;
    let slice_profit = price_distance(request.candle.open, state.latest_order_price)?;
    let slice_profit_entry = price_distance(request.candle.open, state.latest_entry_price)?;
    let slice_profit_exit = state
        .latest_exit_price
        .and_then(|price| price_distance(request.candle.open, price))
        .unwrap_or(0.0);
    let open_grind_count = state
        .clusters
        .iter()
        .map(|cluster| cluster.count)
        .sum::<usize>();
    let grind_entry_signal = evaluate_grind_entry_program(
        manager,
        &adjustment.decision_program,
        trade,
        request.pair,
        request.candle_index,
        request.candle,
        open_grind_count,
        slice_profit,
        slice_profit_entry,
        slice_profit_exit,
    )?;
    let policy = adjustment.constants.policy.as_ref()?;
    if program.retry_policy.entry_retry_ms != policy.entry_retry_ms
        || program.retry_policy.stale_order_ms != policy.stale_order_ms
        || program.retry_policy.entry_retry_ms <= 0
        || program.retry_policy.stale_order_ms <= 0
    {
        return None;
    }
    let retry_cutoff = request
        .candle
        .timestamp_ms
        .checked_sub(program.retry_policy.entry_retry_ms)?;
    let stale_cutoff = request
        .candle
        .timestamp_ms
        .checked_sub(program.retry_policy.stale_order_ms)?;
    let extra_profit = adjustment_condition_matches(
        &policy.extra_entry_profit_condition,
        request.pair,
        request.candle_index,
        slice_profit,
        slice_profit_entry,
        open_grind_count,
    )?;
    let extra_derisk = any_derisk_level(&state, &policy.extra_entry_derisk_levels)?;
    let extra_entry_checks = retry_cutoff > state.latest_entry_timestamp_ms
        && (stale_cutoff > state.latest_order_timestamp_ms || extra_profit || extra_derisk);
    let previous_maxima = read_and_update_compiled_cluster_maxima(
        trade,
        &state.clusters,
        &program.order_scan.grind_levels,
        request.candle.open,
        fee_close(request.config),
        trade.side,
    )?;
    let context = AdjustmentContext {
        adjustment,
        pair: request.pair,
        candle_index: request.candle_index,
        candle: request.candle,
        config: request.config,
        available_balance,
        minimum_stake,
        snapshot,
        slice_amount,
        slice_profit,
        slice_profit_entry,
        current_stake_amount: trade.amount * request.candle.open,
        rebuy_mode,
        is_grind_entry: grind_entry_signal,
        extra_entry_checks,
    };
    for action in &program.source_order {
        match evaluate_compiled_system_action(
            action,
            program,
            &context,
            trade,
            &state,
            &previous_maxima,
            open_grind_count,
        )? {
            CompiledActionOutcome::Continue => {}
            CompiledActionOutcome::ReturnNone => {
                trade.nfi_adjustment_state = Some(state);
                return Some(None);
            }
            CompiledActionOutcome::Signal(signal) => {
                trade.nfi_adjustment_state = Some(state);
                return Some(Some(signal));
            }
        }
    }
    trade.nfi_adjustment_state = Some(state);
    Some(None)
}

#[allow(clippy::too_many_arguments)]
fn evaluate_compiled_system_action(
    action: &CompiledSystemAdjustmentAction,
    program: &CompiledSystemAdjustmentProgram,
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
    previous_maxima: &[(f64, f64)],
    open_grind_count: usize,
) -> Option<CompiledActionOutcome> {
    let mut variables = BTreeMap::new();
    for binding in &action.bindings {
        let value = compiled_binding_value(
            binding.kind,
            binding.level,
            action,
            program,
            context,
            trade,
            state,
            previous_maxima,
            open_grind_count,
        )?;
        if variables.insert(binding.name.clone(), value).is_some() {
            return None;
        }
    }
    let projection = scalar_program_feature_projection(&action.decision_program);
    insert_projected_feature_window(
        &mut variables,
        context.pair,
        context.candle_index,
        &projection,
    )?;
    let value = evaluate_scalar_decision_program(&action.decision_program, &variables)?;
    if value.as_str() == Some("continue") {
        return Some(CompiledActionOutcome::Continue);
    }
    if value.as_str() == Some("return-none") {
        return Some(CompiledActionOutcome::ReturnNone);
    }
    let values = value.as_array()?;
    if values.len() != 2 {
        return None;
    }
    let stake_amount = values.first()?.as_f64()?;
    let tag = values.get(1)?.as_str()?;
    if !stake_amount.is_finite() || stake_amount == 0.0 || tag != action.tag {
        return None;
    }
    let mut tag = tag.to_owned();
    if action.append_entry_ids {
        let index = compiled_grind_index(program, action.level)?;
        for id in &state.clusters.get(index)?.entry_ids {
            tag.push(' ');
            tag.push_str(&id.to_string());
        }
    }
    Some(CompiledActionOutcome::Signal(AdjustmentSignal {
        stake_amount,
        tag,
    }))
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)] // Typed bindings keep source names out of the runtime.
fn compiled_binding_value(
    kind: CompiledSystemAdjustmentInputKind,
    level: Option<usize>,
    action: &CompiledSystemAdjustmentAction,
    program: &CompiledSystemAdjustmentProgram,
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
    previous_maxima: &[(f64, f64)],
    open_grind_count: usize,
) -> Option<Value> {
    let cluster_value = |level: Option<usize>| -> Option<(usize, &GrindCluster, &NfiX7GrindLevel)> {
        let level = level?;
        let index = compiled_grind_index(program, level)?;
        let cluster = state.clusters.get(index)?;
        let constants = context
            .adjustment
            .constants
            .grinds
            .iter()
            .find(|record| record.level == level)?;
        Some((index, cluster, constants))
    };
    let number = |value| number_value(value);
    match kind {
        CompiledSystemAdjustmentInputKind::CurrentRate
        | CompiledSystemAdjustmentInputKind::ExitRate => number(context.candle.open),
        CompiledSystemAdjustmentInputKind::CurrentStakeAmount => {
            number(context.current_stake_amount)
        }
        CompiledSystemAdjustmentInputKind::FeeCloseRate => number(fee_close(context.config)),
        CompiledSystemAdjustmentInputKind::FeeOpenRate => number(fee_open(context.config)),
        CompiledSystemAdjustmentInputKind::FirstEntryAmount => number(state.first_entry_amount),
        CompiledSystemAdjustmentInputKind::IsFuturesMode => {
            Some(Value::Bool(context.config.is_futures))
        }
        CompiledSystemAdjustmentInputKind::ExtraEntryChecks => {
            Some(Value::Bool(context.extra_entry_checks))
        }
        CompiledSystemAdjustmentInputKind::GrindEntrySignal => {
            Some(Value::Bool(context.is_grind_entry))
        }
        CompiledSystemAdjustmentInputKind::BelowMaximumStake => Some(Value::Bool(
            context.current_stake_amount
                < context.slice_amount * context.adjustment.constants.max_stake_multiplier,
        )),
        CompiledSystemAdjustmentInputKind::IsRebuyMode => Some(Value::Bool(context.rebuy_mode)),
        CompiledSystemAdjustmentInputKind::IsSystemV3
        | CompiledSystemAdjustmentInputKind::IsSystemV31 => Some(Value::Bool(false)),
        CompiledSystemAdjustmentInputKind::IsSystemV32 => Some(Value::Bool(true)),
        CompiledSystemAdjustmentInputKind::LastCandle
        | CompiledSystemAdjustmentInputKind::PreviousCandle => Some(Value::Null),
        CompiledSystemAdjustmentInputKind::MaximumStake => number(context.available_balance),
        CompiledSystemAdjustmentInputKind::MinimumStake => number(context.minimum_stake),
        CompiledSystemAdjustmentInputKind::OpenGrindCount => {
            Some(Value::Number(u64::try_from(open_grind_count).ok()?.into()))
        }
        CompiledSystemAdjustmentInputKind::ProfitRatio => number(context.snapshot.ratio),
        CompiledSystemAdjustmentInputKind::ProfitStake => number(context.snapshot.stake),
        CompiledSystemAdjustmentInputKind::SliceAmount => number(context.slice_amount),
        CompiledSystemAdjustmentInputKind::SliceProfit => number(context.slice_profit),
        CompiledSystemAdjustmentInputKind::SliceProfitEntry => number(context.slice_profit_entry),
        CompiledSystemAdjustmentInputKind::ActionTag => Some(Value::String(action.tag.clone())),
        CompiledSystemAdjustmentInputKind::Trade => scalar_trade_value(trade),
        CompiledSystemAdjustmentInputKind::TradeAmount => number(trade.amount),
        CompiledSystemAdjustmentInputKind::TradeLeverage => number(trade.leverage),
        CompiledSystemAdjustmentInputKind::TradeStakeAmount => number(trade.stake_amount),
        CompiledSystemAdjustmentInputKind::DeriskFound => {
            let index = compiled_derisk_index(program, level?)?;
            Some(Value::Bool(state.derisk_found.get(index).copied()?))
        }
        CompiledSystemAdjustmentInputKind::ClusterCount => {
            let (_, cluster, _) = cluster_value(level)?;
            Some(Value::Number(u64::try_from(cluster.count).ok()?.into()))
        }
        CompiledSystemAdjustmentInputKind::ClusterMaximumCount => {
            let stakes = compiled_scaled_stakes(program, context, trade, level?)?;
            Some(Value::Number(u64::try_from(stakes.len()).ok()?.into()))
        }
        CompiledSystemAdjustmentInputKind::ClusterDistance => {
            let (_, cluster, _) = cluster_value(level)?;
            number(cluster.directional_distance(context.candle.open, trade.side))
        }
        CompiledSystemAdjustmentInputKind::ClusterThresholds => {
            let (_, _, constants) = cluster_value(level)?;
            let values = if context.config.is_futures {
                &constants.thresholds_futures
            } else {
                &constants.thresholds_spot
            };
            number_array(values)
        }
        CompiledSystemAdjustmentInputKind::ClusterStakes => {
            let values = compiled_scaled_stakes(program, context, trade, level?)?;
            number_array(&values)
        }
        CompiledSystemAdjustmentInputKind::ClusterTotalAmount => {
            let (_, cluster, _) = cluster_value(level)?;
            number(cluster.total_amount)
        }
        CompiledSystemAdjustmentInputKind::ClusterOpenRate => {
            let (_, cluster, _) = cluster_value(level)?;
            let value = if cluster.count == 0 {
                0.0
            } else {
                cluster.total_cost / cluster.total_amount
            };
            number(value)
        }
        CompiledSystemAdjustmentInputKind::ClusterProfitRate => {
            let (_, cluster, _) = cluster_value(level)?;
            number(cluster.profit_rate(context.candle.open))
        }
        CompiledSystemAdjustmentInputKind::ClusterProfitStake => {
            let (_, cluster, _) = cluster_value(level)?;
            number(cluster.profit_stake(context.candle.open, fee_close(context.config), trade.side))
        }
        CompiledSystemAdjustmentInputKind::ClusterProfitThreshold => {
            let (_, _, constants) = cluster_value(level)?;
            number(if context.config.is_futures {
                constants.profit_threshold_futures
            } else {
                constants.profit_threshold_spot
            })
        }
        CompiledSystemAdjustmentInputKind::ClusterDeriskThreshold => {
            let (_, _, constants) = cluster_value(level)?;
            number(if context.config.is_futures {
                constants.derisk_futures
            } else {
                constants.derisk_spot
            })
        }
        CompiledSystemAdjustmentInputKind::ClusterMaximumProfitStake => {
            let (index, _, _) = cluster_value(level)?;
            number(previous_maxima.get(index)?.0)
        }
        CompiledSystemAdjustmentInputKind::ClusterMaximumProfitRate => {
            let (index, _, _) = cluster_value(level)?;
            number(previous_maxima.get(index)?.1)
        }
    }
}

fn number_array(values: &[f64]) -> Option<Value> {
    values
        .iter()
        .map(|value| number_value(*value))
        .collect::<Option<Vec<_>>>()
        .map(Value::Array)
}

fn compiled_scaled_stakes(
    program: &CompiledSystemAdjustmentProgram,
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    level: usize,
) -> Option<Vec<f64>> {
    let index = compiled_grind_index(program, level)?;
    let tags = program.order_scan.grind_levels.get(index)?;
    let constants = context
        .adjustment
        .constants
        .grinds
        .iter()
        .find(|record| record.level == level)?;
    let stakes = if context.config.is_futures {
        &constants.stakes_futures
    } else {
        &constants.stakes_spot
    };
    let stake_leverage = match tags.minimum_scale_leverage {
        CompiledSystemStakeScale::TradeLeverage => trade.leverage,
        CompiledSystemStakeScale::MarketModeLeverage => {
            if context.config.is_futures {
                trade.leverage
            } else {
                1.0
            }
        }
    };
    scale_stakes_for_minimum(
        stakes,
        context.slice_amount,
        context.minimum_stake,
        stake_leverage,
        trade.leverage,
    )
}

fn compiled_grind_index(program: &CompiledSystemAdjustmentProgram, level: usize) -> Option<usize> {
    program
        .order_scan
        .grind_levels
        .iter()
        .position(|record| record.level == level)
}

fn compiled_derisk_index(program: &CompiledSystemAdjustmentProgram, level: usize) -> Option<usize> {
    program
        .order_scan
        .derisk_tags
        .iter()
        .position(|record| record.level == level)
}

#[allow(clippy::option_option)] // Preserve the evaluator-validity boundary.
fn evaluate_nfi_position_adjustment_with_state(
    manager: &NfiX7TradeManager,
    adjustment: &NfiX7PositionAdjustment,
    trade: &mut OpenTrade,
    state: &AdjustmentState,
    request: &PositionAdjustmentRequest<'_>,
    initial_stake_multiplier: f64,
    rebuy_mode: bool,
) -> Option<Option<AdjustmentSignal>> {
    let pair = request.pair;
    let candle = request.candle;
    let config = request.config;
    let exchange_minimum_stake = adjustment_minimum_stake(pair, candle, trade, config)?;
    // The rebuy wrapper divides Freqtrade's callback minimum by leverage
    // before it delegates to the shared grind callback. Ordinary grind routes
    // enter that callback directly and retain the unleveraged exchange value.
    // Keeping this wrapper boundary prevents an exact cluster exit from being
    // mistaken for a reserve-violating near-full exit.
    let minimum_stake =
        grind_callback_minimum_stake(exchange_minimum_stake, trade.leverage, rebuy_mode);
    // The rebuy wrapper applies the same leverage conversion to Freqtrade's
    // callback maximum before transferring into this shared grind callback.
    // This boundary matters when a large grind level is affordable from the
    // raw wallet but not from the wrapper-adjusted maximum.
    let available_balance =
        grind_callback_maximum_stake(request.available_balance, trade.leverage, rebuy_mode);
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    if !initial_stake_multiplier.is_finite() || initial_stake_multiplier <= 0.0 {
        return None;
    }
    // X7 opens rebuy-mode trades at a fraction of the normal slot size. Once
    // a level-3 de-risk transfers the trade to this shared callback, the
    // source restores the normal slice before sizing every grind branch.
    let slice_amount = state.first_entry_cost / initial_stake_multiplier;
    let slice_profit = price_distance(candle.open, state.latest_order_price)?;
    let slice_profit_entry = price_distance(candle.open, state.latest_entry_price)?;
    let slice_profit_exit = state
        .latest_exit_price
        .and_then(|price| price_distance(candle.open, price))
        .unwrap_or(0.0);
    let is_grind_entry = evaluate_grind_entry_program(
        manager,
        &adjustment.decision_program,
        trade,
        pair,
        request.candle_index,
        candle,
        state
            .clusters
            .iter()
            .map(|cluster| cluster.count)
            .sum::<usize>(),
        slice_profit,
        slice_profit_entry,
        slice_profit_exit,
    )?;
    let policy = adjustment.constants.policy.as_ref()?;
    if policy.entry_retry_ms <= 0
        || policy.stale_order_ms <= 0
        || policy.extra_entry_derisk_levels.is_empty()
    {
        return None;
    }
    let retry_cutoff = candle.timestamp_ms.checked_sub(policy.entry_retry_ms)?;
    let stale_cutoff = candle.timestamp_ms.checked_sub(policy.stale_order_ms)?;
    let num_open_grinds = state
        .clusters
        .iter()
        .map(|cluster| cluster.count)
        .sum::<usize>();
    let extra_profit = adjustment_condition_matches(
        &policy.extra_entry_profit_condition,
        pair,
        request.candle_index,
        slice_profit,
        slice_profit_entry,
        num_open_grinds,
    )?;
    let extra_derisk = any_derisk_level(state, &policy.extra_entry_derisk_levels)?;
    let extra_entry_checks = retry_cutoff > state.latest_entry_timestamp_ms
        && (stale_cutoff > state.latest_order_timestamp_ms || extra_profit || extra_derisk);

    // X7 reads the previous maxima for this invocation, then persists any new
    // maxima before evaluating exits. `long_grind_exit_v3` currently has its
    // trailing branch disabled, but preserving the write order protects the
    // order_filled reset contract and future proof fixtures.
    let previous_maxima =
        read_and_update_cluster_maxima(trade, &state.clusters, candle.open, fee_close(config));
    let context = AdjustmentContext {
        adjustment,
        pair,
        candle_index: request.candle_index,
        candle,
        config,
        available_balance,
        minimum_stake,
        snapshot,
        slice_amount,
        slice_profit,
        slice_profit_entry,
        current_stake_amount: trade.amount * candle.open,
        rebuy_mode,
        is_grind_entry,
        extra_entry_checks,
    };

    if let Some(adjustment) = evaluate_derisk_levels(&context, trade, state)? {
        return Some(Some(adjustment));
    }
    for index in 0..state.clusters.len() {
        match evaluate_grind_level(&context, trade, state, &previous_maxima, index)? {
            GrindLevelOutcome::Continue => {}
            GrindLevelOutcome::ReturnNone => return Some(None),
            GrindLevelOutcome::Signal(adjustment) => return Some(Some(adjustment)),
        }
    }
    Some(None)
}

fn nfi_adjustment_supports_trade(adjustment: &NfiX7PositionAdjustment, trade: &OpenTrade) -> bool {
    let entry_tag = trade.entry_tag.as_deref().unwrap_or("");
    entry_tag.split_whitespace().any(|word| {
        adjustment
            .entry_tags
            .iter()
            .any(|supported| supported == word)
    })
}

fn adjustment_minimum_stake(
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

fn legacy_adjustment_state(
    trade: &mut OpenTrade,
    adjustment: &NfiX7PositionAdjustment,
) -> Option<Arc<AdjustmentState>> {
    trade
        .nfi_adjustment_state
        .take()
        .filter(|state| {
            state.order_count == trade.orders.len() && state.program_fingerprint.is_none()
        })
        .or_else(|| rebuild_adjustment_state(trade, adjustment).map(Arc::new))
}

fn compiled_adjustment_state(
    trade: &mut OpenTrade,
    program: &CompiledSystemAdjustmentProgram,
) -> Option<Arc<AdjustmentState>> {
    trade
        .nfi_adjustment_state
        .take()
        .filter(|state| {
            state.order_count == trade.orders.len()
                && state.program_fingerprint.as_deref() == Some(program.fingerprint.as_str())
        })
        .or_else(|| rebuild_compiled_adjustment_state(trade, program).map(Arc::new))
}

fn rebuild_adjustment_state(
    trade: &OpenTrade,
    adjustment: &NfiX7PositionAdjustment,
) -> Option<AdjustmentState> {
    let first = trade.orders.first()?;
    let aggregates = trade.filled_order_aggregates();
    if aggregates.order_count() != trade.orders.len() {
        return None;
    }
    let latest = aggregates
        .select(FilledOrderSelector::All)
        .latest
        .as_ref()?;
    let latest_entry = aggregates
        .select(FilledOrderSelector::Entries)
        .latest
        .as_ref()?;
    let latest_exit = aggregates
        .select(FilledOrderSelector::Exits)
        .latest
        .as_ref();
    let mut clusters = vec![GrindCluster::default(); adjustment.constants.grinds.len()];
    let mut cluster_closed = vec![false; clusters.len()];
    let mut derisk_found = vec![false; adjustment.constants.derisk_levels.len()];

    // Reversed traversal is observable: exit tags list still-open entry IDs
    // newest first, matching NFI's `reversed(filled_orders)` loop.
    for order in trade.orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry && order.sequence != 0 {
            if let Some(index) = grind_entry_index(tag) {
                if !cluster_closed.get(index).copied()? {
                    let cluster = clusters.get_mut(index)?;
                    cluster.count += 1;
                    cluster.total_amount += order.amount;
                    cluster.total_cost += order.amount * order.price;
                    cluster.entry_ids.push(order.id);
                    cluster.latest_entry_price.get_or_insert(order.price);
                }
            }
            continue;
        }
        if order.is_entry {
            continue;
        }
        let head = tag.split_whitespace().next().unwrap_or("");
        if let Some(index) = derisk_level_index(head) {
            *derisk_found.get_mut(index)? = true;
        } else if let Some(index) = grind_exit_index(head) {
            if !cluster_closed.get(index).copied()? {
                *cluster_closed.get_mut(index)? = true;
                clusters.get_mut(index)?.exit_price = Some(order.price);
            }
        } else if head == "derisk_global" {
            for (closed, cluster) in cluster_closed.iter_mut().zip(&mut clusters) {
                if !*closed {
                    *closed = true;
                    cluster.exit_price = Some(order.price);
                }
            }
        }
    }
    Some(AdjustmentState {
        order_count: trade.orders.len(),
        program_fingerprint: None,
        clusters,
        derisk_found,
        first_entry_amount: first.amount,
        first_entry_cost: first.amount * first.price,
        latest_entry_price: latest_entry.price,
        latest_entry_timestamp_ms: latest_entry.timestamp_ms,
        latest_exit_price: latest_exit.map(|order| order.price),
        latest_order_price: latest.price,
        latest_order_timestamp_ms: latest.timestamp_ms,
    })
}

fn rebuild_compiled_adjustment_state(
    trade: &OpenTrade,
    program: &CompiledSystemAdjustmentProgram,
) -> Option<AdjustmentState> {
    let first = trade.orders.first()?;
    if !compiled_order_side_matches(program.order_scan.entry_order_side, first.side) {
        return None;
    }
    let aggregates = trade.filled_order_aggregates();
    if aggregates.order_count() != trade.orders.len() {
        return None;
    }
    let latest = aggregates
        .select(FilledOrderSelector::All)
        .latest
        .as_ref()?;
    let latest_entry = aggregates
        .select(FilledOrderSelector::Entries)
        .latest
        .as_ref()?;
    let latest_exit = aggregates
        .select(FilledOrderSelector::Exits)
        .latest
        .as_ref();
    let mut clusters = vec![GrindCluster::default(); program.order_scan.grind_levels.len()];
    let mut cluster_closed = vec![false; clusters.len()];
    let mut derisk_found = vec![false; program.order_scan.derisk_tags.len()];
    for order in trade.orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        let head = tag.split_whitespace().next().unwrap_or("");
        if compiled_order_side_matches(program.order_scan.entry_order_side, order.side) {
            if program.order_scan.exclude_first_entry && order.id == first.id {
                continue;
            }
            if let Some(index) = program
                .order_scan
                .grind_levels
                .iter()
                .position(|record| record.entry_tag == tag)
            {
                if !cluster_closed.get(index).copied()? {
                    let cluster = clusters.get_mut(index)?;
                    cluster.count = cluster.count.checked_add(1)?;
                    cluster.total_amount += order.amount;
                    cluster.total_cost += order.amount * order.price;
                    cluster.entry_ids.push(order.id);
                    cluster.latest_entry_price.get_or_insert(order.price);
                }
            }
            continue;
        }
        if !compiled_order_side_matches(program.order_scan.exit_order_side, order.side) {
            return None;
        }
        if let Some(index) = program
            .order_scan
            .derisk_tags
            .iter()
            .position(|record| record.tag == head)
        {
            *derisk_found.get_mut(index)? = true;
            continue;
        }
        if head == program.order_scan.global_exit_tag {
            for (closed, cluster) in cluster_closed.iter_mut().zip(&mut clusters) {
                if !*closed {
                    *closed = true;
                    cluster.exit_price = Some(order.price);
                }
            }
            continue;
        }
        if let Some(index) = program
            .order_scan
            .grind_levels
            .iter()
            .position(|record| record.exit_tag == head || record.derisk_tag == head)
        {
            if !cluster_closed.get(index).copied()? {
                *cluster_closed.get_mut(index)? = true;
                clusters.get_mut(index)?.exit_price = Some(order.price);
            }
        }
    }
    Some(AdjustmentState {
        order_count: trade.orders.len(),
        program_fingerprint: Some(program.fingerprint.clone()),
        clusters,
        derisk_found,
        first_entry_amount: first.amount,
        first_entry_cost: first.amount * first.price,
        latest_entry_price: latest_entry.price,
        latest_entry_timestamp_ms: latest_entry.timestamp_ms,
        latest_exit_price: latest_exit.map(|order| order.price),
        latest_order_price: latest.price,
        latest_order_timestamp_ms: latest.timestamp_ms,
    })
}

fn read_and_update_compiled_cluster_maxima(
    trade: &mut OpenTrade,
    clusters: &[GrindCluster],
    levels: &[CompiledSystemGrindTags],
    rate: f64,
    close_fee: f64,
    side: TradeSide,
) -> Option<Vec<(f64, f64)>> {
    if clusters.len() != levels.len() {
        return None;
    }
    clusters
        .iter()
        .zip(levels)
        .map(|(cluster, level)| {
            let previous_stake = custom_number(trade, &level.maximum_profit_stake_key);
            let previous_rate = custom_number(trade, &level.maximum_profit_rate_key);
            let profit_stake = cluster.profit_stake(rate, close_fee, side);
            let profit_rate = cluster.profit_rate(rate);
            if profit_stake > previous_stake {
                trade.custom_data.insert(
                    level.maximum_profit_stake_key.clone(),
                    number_value(profit_stake)?,
                );
            }
            let rate_improved = match side {
                TradeSide::Long => profit_rate > previous_rate,
                TradeSide::Short => profit_rate < previous_rate,
            };
            if rate_improved {
                trade.custom_data.insert(
                    level.maximum_profit_rate_key.clone(),
                    number_value(profit_rate)?,
                );
            }
            Some((previous_stake, previous_rate))
        })
        .collect()
}

const fn compiled_order_side_matches(expected: CompiledOrderSide, actual: OrderSide) -> bool {
    matches!(
        (expected, actual),
        (CompiledOrderSide::Buy, OrderSide::Buy) | (CompiledOrderSide::Sell, OrderSide::Sell)
    )
}

fn grind_entry_index(tag: &str) -> Option<usize> {
    structured_level_index(tag, "grind_", &["entry"])
}

fn grind_exit_index(tag: &str) -> Option<usize> {
    structured_level_index(tag, "grind_", &["exit", "derisk"])
}

fn derisk_level_index(tag: &str) -> Option<usize> {
    tag.strip_prefix("derisk_level_")?
        .parse::<usize>()
        .ok()?
        .checked_sub(1)
}

fn structured_level_index(tag: &str, prefix: &str, actions: &[&str]) -> Option<usize> {
    let suffix = tag.strip_prefix(prefix)?;
    let (level, action) = suffix.split_once('_')?;
    if !actions.contains(&action) {
        return None;
    }
    level.parse::<usize>().ok()?.checked_sub(1)
}

fn price_distance(rate: f64, reference: f64) -> Option<f64> {
    (reference > 0.0).then_some((rate - reference) / reference)
}

fn directional_rate(rate: f64, side: TradeSide) -> f64 {
    match side {
        TradeSide::Long => rate,
        TradeSide::Short => -rate,
    }
}

/// Execute X7's source-compiled `long_grind_entry_v3` predicate.
///
/// Both the system-v3.2 and legacy tag-120 callbacks call this same Python
/// method. Sharing the scalar-program boundary here ensures they receive the
/// same dataframe projection and variable encoding.
#[allow(clippy::too_many_arguments)]
pub(crate) fn evaluate_grind_entry_program(
    manager: &NfiX7TradeManager,
    program_name: &str,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    num_open_grinds: usize,
    slice_profit: f64,
    slice_profit_entry: f64,
    slice_profit_exit: f64,
) -> Option<bool> {
    let mut variables = BTreeMap::from([
        (
            "num_open_grinds_and_buybacks".to_owned(),
            Value::Number(u64::try_from(num_open_grinds).ok()?.into()),
        ),
        ("slice_profit".to_owned(), number_value(slice_profit)?),
        (
            "slice_profit_entry".to_owned(),
            number_value(slice_profit_entry)?,
        ),
        (
            "slice_profit_exit".to_owned(),
            number_value(slice_profit_exit)?,
        ),
        // The current X7 source names this direction flag `is_derisk` even
        // though callers pass `True` for the long route.
        ("is_derisk".to_owned(), Value::Bool(true)),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
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

fn read_and_update_cluster_maxima(
    trade: &mut OpenTrade,
    clusters: &[GrindCluster],
    rate: f64,
    close_fee: f64,
) -> Vec<(f64, f64)> {
    clusters
        .iter()
        .enumerate()
        .map(|(index, cluster)| {
            let level = index + 1;
            let stake_key = format!("grind_{level}_cluster_max_profit_stake");
            let rate_key = format!("grind_{level}_cluster_max_profit_rate");
            let previous_stake = custom_number(trade, &stake_key);
            let previous_rate = custom_number(trade, &rate_key);
            let profit_stake = cluster.profit_stake(rate, close_fee, trade.side);
            let profit_rate = cluster.profit_rate(rate);
            if profit_stake > previous_stake {
                trade
                    .custom_data
                    .insert(stake_key, number_value(profit_stake).unwrap_or(Value::Null));
            }
            let rate_improved = match trade.side {
                TradeSide::Long => profit_rate > previous_rate,
                // Upstream stores the raw price distance for shorts, so a more
                // profitable cluster is a more negative value.
                TradeSide::Short => profit_rate < previous_rate,
            };
            if rate_improved {
                trade
                    .custom_data
                    .insert(rate_key, number_value(profit_rate).unwrap_or(Value::Null));
            }
            (previous_stake, previous_rate)
        })
        .collect()
}

fn custom_number(trade: &OpenTrade, key: &str) -> f64 {
    trade
        .custom_data
        .get(key)
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
}

#[allow(clippy::option_option)] // Preserve the evaluator-validity boundary.
fn evaluate_derisk_levels(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
) -> Option<Option<AdjustmentSignal>> {
    let constants = &context.adjustment.constants;
    // All three shared de-risk branches contain `not is_rebuy_mode` in X7.
    // A rebuy trade transfers here only after its dedicated level-3 de-risk,
    // so running level 1 or 2 afterward would create an impossible order.
    if !constants.derisk_enable || context.rebuy_mode {
        return Some(None);
    }
    for level in &constants.derisk_levels {
        let index = level.level.checked_sub(1)?;
        let threshold = if context.config.is_futures {
            level.threshold_futures
        } else {
            level.threshold_spot
        };
        if !level.enabled
            || state.derisk_found.get(index).copied()?
            || context.snapshot.stake >= context.slice_amount * threshold / trade.leverage
        {
            continue;
        }
        let stake_fraction = if context.config.is_futures {
            level.stake_futures
        } else {
            level.stake_spot
        };
        let sell_amount =
            state.first_entry_amount * stake_fraction * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, sell_amount) {
            return Some(Some(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: format!("derisk_level_{}", level.level),
            }));
        }
    }
    Some(None)
}

fn evaluate_grind_level(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
    previous_maxima: &[(f64, f64)],
    index: usize,
) -> Option<GrindLevelOutcome> {
    let constants = context.adjustment.constants.grinds.get(index)?;
    let cluster = state.clusters.get(index)?;
    let stakes = if context.config.is_futures {
        &constants.stakes_futures
    } else {
        &constants.stakes_spot
    };
    let thresholds = if context.config.is_futures {
        &constants.thresholds_futures
    } else {
        &constants.thresholds_spot
    };
    let scaled_stakes = scale_stakes_for_minimum(
        stakes,
        context.slice_amount,
        context.minimum_stake,
        if index == 0 || context.config.is_futures {
            trade.leverage
        } else {
            1.0
        },
        trade.leverage,
    )?;
    let entry_signal = grind_entry_signal(context, trade, state, index)?;
    let below_maximum = context.current_stake_amount
        < context.slice_amount * context.adjustment.constants.max_stake_multiplier;
    let distance_allows_entry = if cluster.count == 0 {
        true
    } else if cluster.count < scaled_stakes.len() {
        cluster.directional_distance(context.candle.open, trade.side)
            < *thresholds.get(cluster.count)?
    } else {
        false
    };
    if constants.enabled
        && entry_signal
        && context.extra_entry_checks
        && cluster.count < scaled_stakes.len()
        && distance_allows_entry
        && below_maximum
    {
        let requested = context.slice_amount * scaled_stakes[cluster.count] / trade.leverage;
        let requested = requested.max(context.minimum_stake * 1.5);
        // NFI returns None when the requested order exceeds the current wallet
        // maximum; Freqtrade does not clamp this callback result.
        if requested > context.available_balance {
            return Some(GrindLevelOutcome::ReturnNone);
        }
        return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
            stake_amount: requested,
            tag: format!("grind_{}_entry", index + 1),
        }));
    }

    if cluster.count > 0
        && grind_exit_signal(
            context,
            trade,
            cluster,
            constants,
            *previous_maxima.get(index)?,
        )?
    {
        let raw_exit = cluster.total_amount * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, raw_exit) {
            return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&format!("grind_{}_exit", index + 1), &cluster.entry_ids),
            }));
        }
    }

    let derisk_threshold = if context.config.is_futures {
        constants.derisk_futures
    } else {
        constants.derisk_spot
    };
    if constants.use_derisk
        && cluster.count > 0
        && directional_rate(cluster.profit_rate(context.candle.open), trade.side) < derisk_threshold
    {
        let raw_exit = cluster.total_amount * context.candle.open / trade.leverage;
        if let Some(stake_amount) = partial_exit_stake(context, trade, raw_exit) {
            return Some(GrindLevelOutcome::Signal(AdjustmentSignal {
                stake_amount: -stake_amount,
                tag: order_id_tag(&format!("grind_{}_derisk", index + 1), &cluster.entry_ids),
            }));
        }
    }
    Some(GrindLevelOutcome::Continue)
}

fn scale_stakes_for_minimum(
    stakes: &[f64],
    slice_amount: f64,
    minimum_stake: f64,
    stake_leverage: f64,
    trade_leverage: f64,
) -> Option<Vec<f64>> {
    let first = *stakes.first()?;
    if slice_amount * first / stake_leverage >= minimum_stake {
        return Some(stakes.to_vec());
    }
    let multiplier = minimum_stake / slice_amount / first * trade_leverage;
    Some(stakes.iter().map(|stake| stake * multiplier).collect())
}

fn grind_entry_signal(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    state: &AdjustmentState,
    index: usize,
) -> Option<bool> {
    let policy = context.adjustment.constants.policy.as_ref()?;
    let level = index.checked_add(1)?;
    let matching = policy
        .grind_entry_fallbacks
        .iter()
        .filter(|record| record.level == level)
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return None;
    }
    let num_open_grinds = state
        .clusters
        .iter()
        .map(|cluster| cluster.count)
        .sum::<usize>();
    let fallback =
        matching[0]
            .predicates
            .iter()
            .try_fold(false, |matched, predicate| -> Option<bool> {
                if matched {
                    return Some(true);
                }
                adjustment_predicate_matches(
                    predicate,
                    state,
                    context.config,
                    trade,
                    context.pair,
                    context.candle_index,
                    context.candle.open,
                    context.slice_profit,
                    context.slice_profit_entry,
                    num_open_grinds,
                )
            })?;
    Some(context.is_grind_entry || fallback)
}

#[allow(clippy::too_many_arguments)]
fn adjustment_predicate_matches(
    predicate: &NfiX7AdjustmentPredicate,
    state: &AdjustmentState,
    config: &PortfolioConfig,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    current_rate: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<bool> {
    if let Some(expression) = &predicate.expression {
        return adjustment_expression_matches(
            expression,
            state,
            config,
            trade,
            pair,
            candle_index,
            current_rate,
            slice_profit,
            slice_profit_entry,
            num_open_grinds,
        );
    }
    if !predicate.any_derisk_levels.is_empty()
        && !any_derisk_level(state, &predicate.any_derisk_levels)?
    {
        return Some(false);
    }
    predicate
        .conditions
        .iter()
        .try_fold(true, |matched, condition| {
            if matched {
                adjustment_condition_matches(
                    condition,
                    pair,
                    candle_index,
                    slice_profit,
                    slice_profit_entry,
                    num_open_grinds,
                )
            } else {
                Some(false)
            }
        })
}

#[allow(clippy::too_many_arguments)]
fn adjustment_expression_matches(
    expression: &NfiX7AdjustmentExpression,
    state: &AdjustmentState,
    config: &PortfolioConfig,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    current_rate: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<bool> {
    match expression {
        NfiX7AdjustmentExpression::All { values } => {
            for value in values {
                if !adjustment_expression_matches(
                    value,
                    state,
                    config,
                    trade,
                    pair,
                    candle_index,
                    current_rate,
                    slice_profit,
                    slice_profit_entry,
                    num_open_grinds,
                )? {
                    return Some(false);
                }
            }
            Some(true)
        }
        NfiX7AdjustmentExpression::Any { values } => {
            for value in values {
                if adjustment_expression_matches(
                    value,
                    state,
                    config,
                    trade,
                    pair,
                    candle_index,
                    current_rate,
                    slice_profit,
                    slice_profit_entry,
                    num_open_grinds,
                )? {
                    return Some(true);
                }
            }
            Some(false)
        }
        NfiX7AdjustmentExpression::Not { value } => adjustment_expression_matches(
            value,
            state,
            config,
            trade,
            pair,
            candle_index,
            current_rate,
            slice_profit,
            slice_profit_entry,
            num_open_grinds,
        )
        .map(|value| !value),
        NfiX7AdjustmentExpression::Flag { name } => match name.as_str() {
            "is_futures_mode" => Some(config.is_futures),
            "trade_is_short" => Some(trade.side == TradeSide::Short),
            _ => None,
        },
        NfiX7AdjustmentExpression::DeriskFound { level } => {
            any_derisk_level(state, std::slice::from_ref(level))
        }
        NfiX7AdjustmentExpression::Present { operand } => Some(
            adjustment_expression_operand_value(
                operand,
                trade,
                pair,
                candle_index,
                current_rate,
                slice_profit,
                slice_profit_entry,
                num_open_grinds,
            )
            .is_some(),
        ),
        NfiX7AdjustmentExpression::Comparison {
            left,
            operator,
            right,
        } => adjustment_comparison_matches(
            left,
            *operator,
            right,
            trade,
            pair,
            candle_index,
            current_rate,
            slice_profit,
            slice_profit_entry,
            num_open_grinds,
        ),
    }
}

fn any_derisk_level(state: &AdjustmentState, levels: &[usize]) -> Option<bool> {
    levels.iter().try_fold(false, |found, level| {
        let index = level.checked_sub(1)?;
        Some(found || state.derisk_found.get(index).copied()?)
    })
}

fn adjustment_condition_matches(
    condition: &NfiX7AdjustmentCondition,
    pair: &PairSeries,
    candle_index: usize,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<bool> {
    let left = adjustment_legacy_operand_value(
        &condition.left,
        pair,
        candle_index,
        slice_profit,
        slice_profit_entry,
        num_open_grinds,
    )?;
    let right = adjustment_legacy_operand_value(
        &condition.right,
        pair,
        candle_index,
        slice_profit,
        slice_profit_entry,
        num_open_grinds,
    )?;
    Some(adjustment_values_match(left, condition.operator, right))
}

#[allow(clippy::too_many_arguments)]
fn adjustment_comparison_matches(
    left: &NfiX7AdjustmentOperand,
    operator: NfiX7AdjustmentComparison,
    right: &NfiX7AdjustmentOperand,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    current_rate: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<bool> {
    let left = adjustment_expression_operand_value(
        left,
        trade,
        pair,
        candle_index,
        current_rate,
        slice_profit,
        slice_profit_entry,
        num_open_grinds,
    )?;
    let right = adjustment_expression_operand_value(
        right,
        trade,
        pair,
        candle_index,
        current_rate,
        slice_profit,
        slice_profit_entry,
        num_open_grinds,
    )?;
    Some(adjustment_values_match(left, operator, right))
}

fn adjustment_values_match(left: f64, operator: NfiX7AdjustmentComparison, right: f64) -> bool {
    match operator {
        NfiX7AdjustmentComparison::Lt => left < right,
        NfiX7AdjustmentComparison::Gt => left > right,
        NfiX7AdjustmentComparison::Eq => {
            matches!(left.partial_cmp(&right), Some(std::cmp::Ordering::Equal))
        }
    }
}

fn adjustment_legacy_operand_value(
    operand: &NfiX7AdjustmentOperand,
    pair: &PairSeries,
    candle_index: usize,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<f64> {
    let value = match operand {
        NfiX7AdjustmentOperand::Literal { value } => *value,
        NfiX7AdjustmentOperand::Variable { name } => match name.as_str() {
            "slice_profit" => slice_profit,
            "slice_profit_entry" => slice_profit_entry,
            "num_open_grinds_and_buybacks" => f64::from(u32::try_from(num_open_grinds).ok()?),
            _ => return None,
        },
        NfiX7AdjustmentOperand::Feature { name, multiplier } => {
            feature_number_at(pair, candle_index, name)? * multiplier
        }
        NfiX7AdjustmentOperand::Trade { .. } => return None,
    };
    value.is_finite().then_some(value)
}

#[allow(clippy::too_many_arguments)]
fn adjustment_expression_operand_value(
    operand: &NfiX7AdjustmentOperand,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    current_rate: f64,
    slice_profit: f64,
    slice_profit_entry: f64,
    num_open_grinds: usize,
) -> Option<f64> {
    let value = match operand {
        NfiX7AdjustmentOperand::Literal { value } => *value,
        NfiX7AdjustmentOperand::Variable { name } => match name.as_str() {
            "current_rate" => current_rate,
            "slice_profit" => slice_profit,
            "slice_profit_entry" => slice_profit_entry,
            "num_open_grinds_and_buybacks" => f64::from(u32::try_from(num_open_grinds).ok()?),
            _ => return None,
        },
        NfiX7AdjustmentOperand::Feature { name, multiplier } => {
            feature_number_at(pair, candle_index, name)? * multiplier
        }
        NfiX7AdjustmentOperand::Trade { name, multiplier } => match name.as_str() {
            "liquidation_price" => trade.liquidation_price? * multiplier,
            _ => return None,
        },
    };
    value.is_finite().then_some(value)
}

fn grind_exit_signal(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    cluster: &GrindCluster,
    constants: &NfiX7GrindLevel,
    _previous_maximum: (f64, f64),
) -> Option<bool> {
    let profit_threshold = if context.config.is_futures {
        constants.profit_threshold_futures
    } else {
        constants.profit_threshold_spot
    };
    if directional_rate(cluster.profit_rate(context.candle.open), trade.side)
        < profit_threshold + fee_open(context.config) + fee_close(context.config)
    {
        return Some(false);
    }
    let field = |name| feature_number_at(context.pair, context.candle_index, name);
    let normal_exit = match trade.side {
        TradeSide::Long => {
            field("RSI_3")? > 99.0
                || field("RSI_14")? > 70.0
                || field("WILLR_14")? > -0.1
                || field("STOCHRSIk_14_14_3_3")? > 95.0
                || field("close")? > field("BBU_20_2.0")? * 1.01
                || (field("RSI_3")? > 90.0 && field("RSI_14")? < 50.0)
                || (field("RSI_3")? > 80.0
                    && field("RSI_3_1h")? < 20.0
                    && field("RSI_3_4h")? < 20.0
                    && field("ROC_9_1d")? > -10.0
                    && field("BTC_RSI_14_4h")? < 35.0)
        }
        TradeSide::Short => {
            field("RSI_3")? < 1.0
                || field("RSI_14")? < 30.0
                || field("WILLR_14")? < -99.9
                || field("STOCHRSIk_14_14_3_3")? < 5.0
                || field("close")? < field("BBL_20_2.0")? * 0.99
                || (field("RSI_3")? < 10.0 && field("RSI_14")? > 50.0)
                || (field("RSI_3")? < 20.0
                    && field("RSI_3_1h")? > 80.0
                    && field("RSI_3_4h")? > 80.0
                    && field("ROC_9_1d")? < 10.0
                    && field("BTC_RSI_14_4h")? > 65.0)
        }
    };
    Some(normal_exit)
}

fn partial_exit_stake(
    context: &AdjustmentContext<'_>,
    trade: &OpenTrade,
    requested_exit: f64,
) -> Option<f64> {
    let remaining = context.current_stake_amount / trade.leverage - requested_exit;
    let exit_amount = if remaining < context.minimum_stake * 1.55 {
        trade.amount * context.candle.open / trade.leverage - context.minimum_stake * 1.55
    } else {
        requested_exit
    };
    let ft_stake =
        exit_amount * trade.leverage * (trade.stake_amount / trade.amount) / context.candle.open;
    (exit_amount > context.minimum_stake && ft_stake > context.minimum_stake).then_some(ft_stake)
}

fn grind_callback_minimum_stake(
    exchange_minimum_stake: f64,
    leverage: f64,
    rebuy_mode: bool,
) -> f64 {
    if rebuy_mode {
        exchange_minimum_stake / leverage
    } else {
        exchange_minimum_stake
    }
}

fn grind_callback_maximum_stake(
    exchange_maximum_stake: f64,
    leverage: f64,
    rebuy_mode: bool,
) -> f64 {
    if rebuy_mode {
        exchange_maximum_stake / leverage
    } else {
        exchange_maximum_stake
    }
}

fn order_id_tag(prefix: &str, ids: &[u64]) -> String {
    ids.iter().fold(prefix.to_owned(), |mut tag, id| {
        tag.push(' ');
        tag.push_str(&id.to_string());
        tag
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, OnceLock};

    use serde_json::json;

    use crate::domain::{
        CompiledSystemAdjustmentProgram, FilledOrder, NfiX7AdjustmentConstants,
        NfiX7AdjustmentExpression, NfiX7AdjustmentPredicate, NfiX7GrindLevel,
        NfiX7PositionAdjustment, OrderSide, PairSeries, PortfolioConfig,
    };
    use crate::portfolio::{OpenTrade, TradeSide};

    use super::{
        adjustment_predicate_matches, compiled_adjustment_state, derisk_level_index,
        evaluate_compiled_system_action, grind_callback_maximum_stake,
        grind_callback_minimum_stake, grind_entry_index, grind_exit_index,
        rebuild_compiled_adjustment_state, AdjustmentContext, AdjustmentState,
        CompiledActionOutcome, GrindCluster, NfiProfitSnapshot,
    };

    fn compiled_action_program(result: &serde_json::Value) -> CompiledSystemAdjustmentProgram {
        compiled_directional_action_program(result, "long", "buy", "sell")
    }

    fn compiled_directional_action_program(
        result: &serde_json::Value,
        side: &str,
        entry_order_side: &str,
        exit_order_side: &str,
    ) -> CompiledSystemAdjustmentProgram {
        serde_json::from_value(json!({
            "schema_version": "system-adjustment-program-v1",
            "execution_mode": "primary-with-legacy-shadow",
            "side": side,
            "source_callback": "source_adjustment",
            "source_order": [{
                "kind": "grind-exit",
                "level": 12,
                "tag": "source_exit",
                "append_entry_ids": true,
                "decision_program": result,
                "bindings": [
                    {"name": "min_stake", "kind": "minimum-stake"},
                    {"name": "tag", "kind": "action-tag"}
                ],
                "input_contract": {},
                "location": {"line": 10, "column": 0, "end_line": 20, "end_column": 1}
            }],
            "order_scan": {
                "sequence": "reverse",
                "entry_order_side": entry_order_side,
                "exit_order_side": exit_order_side,
                "exclude_first_entry": true,
                "global_exit_tag": "source_global_exit",
                "derisk_tags": [],
                "grind_levels": [{
                    "level": 12,
                    "entry_tag": "source_entry",
                    "exit_tag": "source_exit",
                    "derisk_tag": "source_derisk",
                    "maximum_profit_stake_key": "source_max_stake",
                    "maximum_profit_rate_key": "source_max_rate",
                    "minimum_scale_leverage": "trade-leverage"
                }],
                "partial_fill_policy": "filled-orders-have-zero-remaining"
            },
            "input_contract": {},
            "retry_policy": {"entry_retry_ms": 300_000, "stale_order_ms": 21_600_000},
            "location": {"line": 1, "column": 0, "end_line": 30, "end_column": 1},
            "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }))
        .expect("valid compiled system adjustment")
    }

    fn test_adjustment() -> NfiX7PositionAdjustment {
        NfiX7PositionAdjustment {
            enabled: true,
            entry_tags: vec!["source_route".to_owned()],
            system_version: "system_v3_2".to_owned(),
            source_callback: Some("source_adjustment".to_owned()),
            decision_program: "source_entry_program".to_owned(),
            program_order: vec!["source_exit".to_owned()],
            stateful_input_contract: json!({}),
            constants: NfiX7AdjustmentConstants {
                derisk_enable: false,
                max_stake_multiplier: 1.0,
                rebuy_stake_multiplier: None,
                derisk_levels: Vec::new(),
                grinds: vec![NfiX7GrindLevel {
                    level: 12,
                    enabled: true,
                    use_derisk: true,
                    derisk_futures: -0.2,
                    derisk_spot: -0.2,
                    profit_threshold_futures: 0.02,
                    profit_threshold_spot: 0.02,
                    stakes_futures: vec![0.1],
                    stakes_spot: vec![0.1],
                    thresholds_futures: vec![-0.1],
                    thresholds_spot: vec![-0.1],
                }],
                policy: None,
            },
            program: None,
        }
    }

    fn test_trade() -> OpenTrade {
        OpenTrade {
            id: 1,
            pair_index: 0,
            pair: "TEST/USDT".to_owned(),
            side: TradeSide::Long,
            leverage: 1.0,
            amount_step: 0.001,
            price_step: 0.01,
            open_timestamp_ms: 0,
            open_rate: 100.0,
            amount: 1.0,
            stake_amount: 100.0,
            max_stake_amount: 100.0,
            entry_cost_with_fees: 100.1,
            first_entry_cost_with_fees: 100.1,
            adjustment_count: 0,
            entry_tag: Some("source_route".to_owned()),
            entry_tag_cache: OnceLock::new(),
            funding_fees: 0.0,
            funding_fees_total: 0.0,
            funding_sum_high: 0.0,
            funding_sum_low: 0.0,
            funding_rebase_seed: None,
            realized_partial_profit: 0.0,
            liquidation_price: None,
            liquidation_price_is_explicit: false,
            initial_stop_loss: 1.0,
            stop_loss: 1.0,
            minimum_rate: 90.0,
            maximum_rate: 100.0,
            orders: vec![FilledOrder {
                id: 1,
                funding_fee: 0.0,
                sequence: 0,
                side: OrderSide::Buy,
                is_entry: true,
                filled_timestamp_ms: 0,
                amount: 1.0,
                price: 100.0,
                cost: 100.0,
                tag: Some("source_route".to_owned()),
            }],
            filled_order_aggregates: OnceLock::new(),
            custom_data: BTreeMap::new(),
            nfi_adjustment_state: None,
        }
    }

    fn pair_and_config() -> (PairSeries, PortfolioConfig) {
        let pair = serde_json::from_value(json!({
            "pair": "TEST/USDT",
            "minimum_cost": 5.0,
            "candles": [{
                "timestamp_ms": 300_000,
                "open": 90.0,
                "high": 91.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0
            }]
        }))
        .expect("valid pair");
        let config = serde_json::from_value(json!({
            "starting_balance": 1_000.0,
            "max_open_trades": 1,
            "stake_amount": 100.0,
            "fee_rate": 0.001,
            "stoploss_ratio": -0.99,
            "amount_step": 0.001,
            "price_step": 0.01,
            "amount_reserve_percent": 0.0
        }))
        .expect("valid config");
        (pair, config)
    }

    fn liquidation_proximity_predicate() -> NfiX7AdjustmentPredicate {
        let expression: NfiX7AdjustmentExpression = serde_json::from_value(json!({
            "op": "all",
            "values": [
                {"op": "flag", "name": "is_futures_mode"},
                {
                    "op": "present",
                    "operand": {
                        "kind": "trade",
                        "name": "liquidation_price",
                        "multiplier": 1.0
                    }
                },
                {
                    "op": "any",
                    "values": [
                        {
                            "op": "all",
                            "values": [
                                {"op": "flag", "name": "trade_is_short"},
                                {
                                    "op": "comparison",
                                    "left": {"kind": "variable", "name": "current_rate"},
                                    "operator": "gt",
                                    "right": {
                                        "kind": "trade",
                                        "name": "liquidation_price",
                                        "multiplier": 0.9
                                    }
                                }
                            ]
                        },
                        {
                            "op": "all",
                            "values": [
                                {
                                    "op": "not",
                                    "value": {"op": "flag", "name": "trade_is_short"}
                                },
                                {
                                    "op": "comparison",
                                    "left": {"kind": "variable", "name": "current_rate"},
                                    "operator": "lt",
                                    "right": {
                                        "kind": "trade",
                                        "name": "liquidation_price",
                                        "multiplier": 1.1
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }))
        .expect("valid liquidation-proximity expression");
        NfiX7AdjustmentPredicate {
            any_derisk_levels: Vec::new(),
            conditions: Vec::new(),
            expression: Some(expression),
        }
    }

    fn empty_adjustment_state() -> AdjustmentState {
        AdjustmentState {
            order_count: 1,
            program_fingerprint: None,
            clusters: Vec::new(),
            derisk_found: vec![false; 3],
            first_entry_amount: 1.0,
            first_entry_cost: 100.0,
            latest_entry_price: 100.0,
            latest_entry_timestamp_ms: 0,
            latest_exit_price: None,
            latest_order_price: 100.0,
            latest_order_timestamp_ms: 0,
        }
    }

    #[test]
    fn liquidation_proximity_expression_matches_long_futures() {
        let (pair, mut config) = pair_and_config();
        config.is_futures = true;
        let mut trade = test_trade();
        trade.liquidation_price = Some(95.0);
        let predicate = liquidation_proximity_predicate();
        let state = empty_adjustment_state();
        {
            let matches = |current_rate| {
                adjustment_predicate_matches(
                    &predicate,
                    &state,
                    &config,
                    &trade,
                    &pair,
                    0,
                    current_rate,
                    -0.04,
                    -0.04,
                    0,
                )
            };

            assert_eq!(matches(100.0), Some(true));
            assert_eq!(matches(105.0), Some(false));
        }
        trade.liquidation_price = None;
        assert_eq!(
            adjustment_predicate_matches(
                &predicate, &state, &config, &trade, &pair, 0, 100.0, -0.04, -0.04, 0,
            ),
            Some(false)
        );
    }

    #[test]
    fn liquidation_proximity_expression_matches_short_futures() {
        let (pair, mut config) = pair_and_config();
        config.is_futures = true;
        let mut trade = test_trade();
        trade.side = TradeSide::Short;
        trade.liquidation_price = Some(110.0);
        let predicate = liquidation_proximity_predicate();
        let state = empty_adjustment_state();
        {
            let matches = |current_rate| {
                adjustment_predicate_matches(
                    &predicate,
                    &state,
                    &config,
                    &trade,
                    &pair,
                    0,
                    current_rate,
                    0.04,
                    0.04,
                    0,
                )
            };

            assert_eq!(matches(100.0), Some(true));
            assert_eq!(matches(98.0), Some(false));
        }
        config.is_futures = false;
        assert_eq!(
            adjustment_predicate_matches(
                &predicate, &state, &config, &trade, &pair, 0, 100.0, 0.04, 0.04, 0,
            ),
            Some(false)
        );
    }

    #[test]
    fn rebuy_transfer_keeps_the_wrappers_leverage_adjusted_minimum() {
        let exchange_minimum = 11.025_21;

        let transferred = grind_callback_minimum_stake(exchange_minimum, 2.0, true);
        let direct = grind_callback_minimum_stake(exchange_minimum, 2.0, false);

        assert!((transferred - 5.512_605).abs() < f64::EPSILON);
        assert!((direct - exchange_minimum).abs() < f64::EPSILON);
    }

    #[test]
    fn rebuy_transfer_keeps_the_wrappers_leverage_adjusted_maximum() {
        let exchange_maximum = 65_859.0;

        let transferred = grind_callback_maximum_stake(exchange_maximum, 2.0, true);
        let direct = grind_callback_maximum_stake(exchange_maximum, 2.0, false);

        assert!((transferred - 32_929.5).abs() < f64::EPSILON);
        assert!((direct - exchange_maximum).abs() < f64::EPSILON);
    }

    #[test]
    fn structured_grind_tags_have_no_fixed_level_ceiling() {
        assert_eq!(grind_entry_index("grind_7_entry"), Some(6));
        assert_eq!(grind_exit_index("grind_12_exit"), Some(11));
        assert_eq!(grind_exit_index("grind_12_derisk"), Some(11));
        assert_eq!(derisk_level_index("derisk_level_12"), Some(11));
        assert_eq!(grind_entry_index("grind_0_entry"), None);
        assert_eq!(grind_exit_index("grind_12_unknown"), None);
    }

    #[test]
    fn compiled_state_cache_reuses_only_matching_order_and_program_identity() {
        let scalar_program = json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [["literal", null]],
            "statements": [["return", 0]]
        });
        let program = compiled_action_program(&scalar_program);
        let mut trade = test_trade();
        let first = compiled_adjustment_state(&mut trade, &program).expect("initial state");
        trade.nfi_adjustment_state = Some(Arc::clone(&first));

        let reused = compiled_adjustment_state(&mut trade, &program).expect("cached state");
        assert!(Arc::ptr_eq(&first, &reused));

        trade.nfi_adjustment_state = Some(Arc::clone(&reused));
        let mut different_program = program.clone();
        different_program.fingerprint = "b".repeat(64);
        let different = compiled_adjustment_state(&mut trade, &different_program)
            .expect("program-specific rebuild");
        assert!(!Arc::ptr_eq(&reused, &different));

        trade.nfi_adjustment_state = Some(Arc::clone(&different));
        trade.push_filled_order(FilledOrder {
            id: 2,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Buy,
            is_entry: true,
            filled_timestamp_ms: 60_000,
            amount: 0.5,
            price: 90.0,
            cost: 45.0,
            tag: Some("source_entry".to_owned()),
        });
        assert!(trade.nfi_adjustment_state.is_none());
        let rebuilt = compiled_adjustment_state(&mut trade, &different_program)
            .expect("order append rebuild");
        assert!(!Arc::ptr_eq(&different, &rebuilt));
        assert_eq!(rebuilt.order_count, 2);
    }

    #[test]
    fn compiled_action_uses_payload_stake_tag_and_dynamic_entry_ids() {
        let scalar_program = json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["min_stake", "tag"],
            "expressions": [
                ["variable", "min_stake"],
                ["literal", 2.0],
                ["multiply", 0, 1],
                ["variable", "tag"],
                ["tuple", [2, 3]]
            ],
            "statements": [["return", 4]]
        });
        let program = compiled_action_program(&scalar_program);
        let adjustment = test_adjustment();
        let trade = test_trade();
        let (pair, config) = pair_and_config();
        let candle = pair.candles.get(0).expect("one candle").into_owned();
        let state = AdjustmentState {
            order_count: 1,
            program_fingerprint: None,
            clusters: vec![GrindCluster {
                count: 2,
                total_amount: 0.5,
                total_cost: 50.0,
                entry_ids: vec![7, 9],
                latest_entry_price: Some(100.0),
                exit_price: None,
            }],
            derisk_found: Vec::new(),
            first_entry_amount: 1.0,
            first_entry_cost: 100.0,
            latest_entry_price: 100.0,
            latest_entry_timestamp_ms: 0,
            latest_exit_price: None,
            latest_order_price: 100.0,
            latest_order_timestamp_ms: 0,
        };
        let context = AdjustmentContext {
            adjustment: &adjustment,
            pair: &pair,
            candle_index: 0,
            candle: &candle,
            config: &config,
            available_balance: 1_000.0,
            minimum_stake: 5.0,
            snapshot: NfiProfitSnapshot {
                stake: 0.0,
                ratio: 0.0,
                current_stake_ratio: 0.0,
                initial_stake_ratio: 0.0,
            },
            slice_amount: 100.0,
            slice_profit: -0.1,
            slice_profit_entry: -0.1,
            current_stake_amount: 90.0,
            rebuy_mode: false,
            is_grind_entry: false,
            extra_entry_checks: false,
        };
        let outcome = evaluate_compiled_system_action(
            &program.source_order[0],
            &program,
            &context,
            &trade,
            &state,
            &[(0.0, 0.0)],
            2,
        )
        .expect("valid generic action");

        let CompiledActionOutcome::Signal(signal) = outcome else {
            panic!("source action must emit an adjustment");
        };
        assert!((signal.stake_amount - 10.0).abs() < f64::EPSILON);
        assert_eq!(signal.tag, "source_exit 7 9");
    }

    #[test]
    fn compiled_action_preserves_explicit_source_return_none() {
        let scalar_program = json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["min_stake", "tag"],
            "expressions": [["literal", "return-none"]],
            "statements": [["return", 0]]
        });
        let program = compiled_action_program(&scalar_program);
        let adjustment = test_adjustment();
        let trade = test_trade();
        let (pair, config) = pair_and_config();
        let candle = pair.candles.get(0).expect("one candle").into_owned();
        let state = AdjustmentState {
            order_count: 1,
            program_fingerprint: None,
            clusters: vec![GrindCluster::default()],
            derisk_found: Vec::new(),
            first_entry_amount: 1.0,
            first_entry_cost: 100.0,
            latest_entry_price: 100.0,
            latest_entry_timestamp_ms: 0,
            latest_exit_price: None,
            latest_order_price: 100.0,
            latest_order_timestamp_ms: 0,
        };
        let context = AdjustmentContext {
            adjustment: &adjustment,
            pair: &pair,
            candle_index: 0,
            candle: &candle,
            config: &config,
            available_balance: 1_000.0,
            minimum_stake: 5.0,
            snapshot: NfiProfitSnapshot {
                stake: 0.0,
                ratio: 0.0,
                current_stake_ratio: 0.0,
                initial_stake_ratio: 0.0,
            },
            slice_amount: 100.0,
            slice_profit: -0.1,
            slice_profit_entry: -0.1,
            current_stake_amount: 90.0,
            rebuy_mode: false,
            is_grind_entry: false,
            extra_entry_checks: false,
        };

        assert!(matches!(
            evaluate_compiled_system_action(
                &program.source_order[0],
                &program,
                &context,
                &trade,
                &state,
                &[(0.0, 0.0)],
                0,
            ),
            Some(CompiledActionOutcome::ReturnNone)
        ));
    }

    #[test]
    fn compiled_short_order_scan_and_partial_exit_use_directional_program_data() {
        let scalar_program = json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["min_stake", "tag"],
            "expressions": [
                ["variable", "min_stake"],
                ["literal", -2.0],
                ["multiply", 0, 1],
                ["variable", "tag"],
                ["tuple", [2, 3]]
            ],
            "statements": [["return", 4]]
        });
        let program = compiled_directional_action_program(&scalar_program, "short", "sell", "buy");
        let adjustment = test_adjustment();
        let mut trade = test_trade();
        trade.side = TradeSide::Short;
        trade.orders[0].side = OrderSide::Sell;
        trade.orders.push(FilledOrder {
            id: 7,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Sell,
            is_entry: true,
            filled_timestamp_ms: 60_000,
            amount: 0.5,
            price: 105.0,
            cost: 52.5,
            tag: Some("source_entry".to_owned()),
        });
        let state = rebuild_compiled_adjustment_state(&trade, &program)
            .expect("short sell entries reconstruct through program order sides");
        assert_eq!(state.clusters[0].count, 1);
        assert_eq!(state.clusters[0].entry_ids, vec![7]);

        let (pair, config) = pair_and_config();
        let candle = pair.candles.get(0).expect("one candle").into_owned();
        let context = AdjustmentContext {
            adjustment: &adjustment,
            pair: &pair,
            candle_index: 0,
            candle: &candle,
            config: &config,
            available_balance: 1_000.0,
            minimum_stake: 5.0,
            snapshot: NfiProfitSnapshot {
                stake: 0.0,
                ratio: 0.0,
                current_stake_ratio: 0.0,
                initial_stake_ratio: 0.0,
            },
            slice_amount: 100.0,
            slice_profit: 0.05,
            slice_profit_entry: 0.05,
            current_stake_amount: 157.5,
            rebuy_mode: false,
            is_grind_entry: false,
            extra_entry_checks: false,
        };
        let outcome = evaluate_compiled_system_action(
            &program.source_order[0],
            &program,
            &context,
            &trade,
            &state,
            &[(0.0, 0.0)],
            1,
        )
        .expect("valid short generic action");

        let CompiledActionOutcome::Signal(signal) = outcome else {
            panic!("short source action must emit a partial exit");
        };
        assert!((signal.stake_amount + 10.0).abs() < f64::EPSILON);
        assert_eq!(signal.tag, "source_exit 7");
    }
}
