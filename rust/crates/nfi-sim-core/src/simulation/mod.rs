use std::collections::BTreeMap;
use std::time::Instant;

mod event;
mod output;

use event::{simulation_event, EventProjection};
use output::{finalize_simulation, FinalizationInput};

use crate::calculations::{
    available_stake_amount, ceil_step, checked_float_sum, duration_ns, floor_step,
    logical_pair_event_count, scheduled_cursor,
};
use crate::callbacks::evaluate_adjustment_bundle;
use crate::execution::{
    adjustment_minimum_pair_stake, apply_adjustment, begin_callback_trace, close_trade,
    current_profit_ratio, evaluate_exit_confirm_program, evaluate_state_machine_adjustment,
    executable_custom_exit, executable_custom_stoploss, executable_exit_confirmation,
    executable_order_filled, executable_position_adjustment, exit_decisions, finish_callback_trace,
    liquidation_reached, minimum_pair_stake, ordered_risk_candidates, rule_adjustment,
    trace_trade_callback, update_extrema, CloseTradeContext, EntryExecution, ExecutableCallbacks,
    ExitDecision,
};
use crate::execution_observer::{self, AdjustmentEventInput, EventInput, ExitFillEventInput};
use crate::futures::apply_funding;
use crate::nfi::{evaluate_nfi_position_adjustment, PositionAdjustmentRequest, ProfitTarget};
use crate::portfolio::{wallet_free, OpenTrade, TradeSide};
use crate::profiling::build_simulation_profile;
use crate::protections::ProtectionState;
use crate::scheduler::{callback_feature_index, fill_pair_processing_order};
use crate::scheduler_observer::{self, BoundaryContext, BoundaryDetail};
use crate::validation::{freqtrade_entry_signal, validate_input};
use crate::{
    CallbackInvocation, CallbackProgramResult, CallbackProgramRuntime, ExecutionBoundary,
    ExecutionBoundaryEvent, OrderType, PortfolioBoundary, SimError, SimulationEvent,
    SimulationInput, SimulationProfile, SimulationResult,
};

pub(super) fn simulate_internal(
    input: &SimulationInput,
    observer: Option<&mut dyn FnMut(&SimulationEvent)>,
    portfolio_observer: Option<&mut dyn FnMut(&crate::PortfolioBoundaryEvent)>,
    execution_observer: Option<&mut dyn FnMut(&ExecutionBoundaryEvent)>,
) -> Result<(SimulationResult, SimulationProfile), SimError> {
    let outcome = simulate_internal_impl(input, observer, portfolio_observer, execution_observer);
    input
        .pairs
        .iter()
        .find_map(|pair| pair.candles.backing_failure())
        .map_or(outcome, Err)
}

#[allow(clippy::if_not_else, clippy::too_many_lines)]
fn simulate_internal_impl(
    input: &SimulationInput,
    mut observer: Option<&mut dyn FnMut(&SimulationEvent)>,
    mut portfolio_observer: Option<&mut dyn FnMut(&crate::PortfolioBoundaryEvent)>,
    mut execution_observer: Option<&mut dyn FnMut(&ExecutionBoundaryEvent)>,
) -> Result<(SimulationResult, SimulationProfile), SimError> {
    let validation_started = Instant::now();
    let validation = validate_input(input)?;
    let validation_ns = duration_ns(validation_started.elapsed());
    let event_loop_started = Instant::now();
    let sparse_execution = observer.is_none();
    let pair_events = logical_pair_event_count(&input.pairs);
    let mut timestamp_batches = if sparse_execution {
        validation.logical_timestamp_batches
    } else {
        0
    };
    let config = &input.config;
    // Each pair may retain a different amount of startup context. Initializing
    // from the sealed boundary excludes those rows from global time ordering,
    // order IDs, shared-wallet accounting, and observer traces.
    let mut cursors = input
        .pairs
        .iter()
        .map(|pair| scheduled_cursor(pair, pair.execution_start_index, false, sparse_execution))
        .collect::<Vec<_>>();
    // Reading every pair's file-backed timestamp twice for every global
    // timestamp batch dominated large pair universes. Cache only the next
    // timestamp for each cursor. Freqtrade processes pairs with open trades
    // first on every main candle, followed by the configured pair order. The
    // reusable buffers below reproduce that order without allocating a fresh
    // pair list for every timestamp.
    let mut next_timestamps = input
        .pairs
        .iter()
        .zip(&cursors)
        .map(|(pair, cursor)| pair.candles.timestamp_ms(*cursor))
        .collect::<Vec<_>>();
    let mut processing_order = Vec::with_capacity(input.pairs.len());
    let mut open_pair_flags = vec![false; input.pairs.len()];
    let mut open_trades: Vec<OpenTrade> = Vec::new();
    let mut closed_trades = Vec::new();
    let mut available_balance = config.starting_balance;
    let mut rejected_signals = 0_u64;
    let mut next_trade_id = 1_u64;
    let mut next_order_id = 1_u64;
    let mut maximum_concurrent_trades = 0_usize;
    let mut profit_targets: BTreeMap<String, ProfitTarget> = BTreeMap::new();
    let mut protection_state = ProtectionState::default();
    let mut executable_runtime = config
        .executable_callback_program
        .as_ref()
        .map(CallbackProgramRuntime::new)
        .transpose()?;
    let mut executable_events: Vec<CallbackProgramResult> = Vec::new();
    let mut portfolio_events = Vec::new();
    let mut portfolio_event_sequence = 0_u64;
    let mut execution_events = Vec::new();
    let mut execution_event_sequence = 0_u64;
    let mut lifecycle_started = false;

    while let Some(timestamp_ms) = next_timestamps.iter().flatten().copied().min() {
        if !sparse_execution {
            timestamp_batches += 1;
        }
        fill_pair_processing_order(
            open_trades.iter().map(|trade| trade.pair_index),
            input.pairs.len(),
            &mut processing_order,
            &mut open_pair_flags,
        );
        let timestamp_event_start = executable_events.len();
        if let (Some(program), Some(runtime)) = (
            config.executable_callback_program.as_ref(),
            executable_runtime.as_mut(),
        ) {
            let mut callbacks = ExecutableCallbacks::new(program, runtime, &mut executable_events);
            let mut strategy_state = BTreeMap::new();
            if !lifecycle_started {
                callbacks.invoke(
                    &CallbackInvocation::new(
                        "loop_cadence_startup_lookback",
                        timestamp_ms,
                        BTreeMap::new(),
                    ),
                    &mut strategy_state,
                )?;
                lifecycle_started = true;
            }
            let first_pair_index = next_timestamps
                .iter()
                .position(|value| *value == Some(timestamp_ms))
                .ok_or(SimError::InvalidCallbackRuntime)?;
            let cursor = cursors[first_pair_index];
            let start = input.pairs[first_pair_index].execution_start_index;
            let visible_rows = cursor.saturating_sub(start);
            let last_visible_timestamp_seconds = cursor
                .checked_sub(2)
                .filter(|index| *index >= start)
                .and_then(|index| input.pairs[first_pair_index].candles.timestamp_ms(index))
                .map_or(0, |value| value / 1000);
            callbacks.invoke(
                &CallbackInvocation::new(
                    "bot_loop_start",
                    timestamp_ms,
                    BTreeMap::from([
                        (
                            "current_time".to_owned(),
                            serde_json::Value::from(timestamp_ms),
                        ),
                        ("callback_dataframe".to_owned(), serde_json::json!({})),
                        (
                            "callback_dataframe_empty".to_owned(),
                            serde_json::Value::from(visible_rows == 0),
                        ),
                        (
                            "last_visible_timestamp_seconds".to_owned(),
                            serde_json::Value::from(last_visible_timestamp_seconds),
                        ),
                        (
                            "visible_rows".to_owned(),
                            serde_json::Value::from(visible_rows),
                        ),
                    ]),
                ),
                &mut strategy_state,
            )?;
        }
        let mut first_pair_at_timestamp = true;

        let mut actual_processing_order_index = 0_usize;
        for pair_index in processing_order.iter().copied() {
            let pair = &input.pairs[pair_index];
            if next_timestamps[pair_index] != Some(timestamp_ms) {
                continue;
            }
            let processing_order_index = actual_processing_order_index;
            actual_processing_order_index += 1;
            let cursor = cursors[pair_index];
            let portfolio_event_start = portfolio_events.len();
            let execution_event_start = execution_events.len();
            let boundary_context = BoundaryContext {
                timestamp_ms,
                pair: &pair.pair,
                configured_pair_index: pair_index,
                processing_order_index,
            };
            let visit_state = scheduler_observer::state(
                available_balance,
                &open_trades,
                &closed_trades,
                config.max_open_trades,
                next_trade_id,
                next_order_id,
                rejected_signals,
            )?;
            portfolio_events.push(scheduler_observer::event(
                &mut portfolio_event_sequence,
                &boundary_context,
                PortfolioBoundary::PairVisit,
                visit_state.clone(),
                visit_state,
                BoundaryDetail::plain(),
            ));
            let executable_event_start = if first_pair_at_timestamp {
                timestamp_event_start
            } else {
                executable_events.len()
            };
            first_pair_at_timestamp = false;
            let existing_trade_index = open_trades
                .iter()
                .position(|trade| trade.pair_index == pair_index);
            if existing_trade_index.is_none()
                && pair.candles.has_entry_signal(cursor) == Some(false)
            {
                // Most long-horizon pair rows have neither an open trade nor
                // an entry signal. Preserve their chronological trace event
                // without constructing the full OHLCV/funding/tag object.
                cursors[pair_index] = scheduled_cursor(pair, cursor + 1, false, sparse_execution);
                next_timestamps[pair_index] = pair.candles.timestamp_ms(cursors[pair_index]);
                if cursors[pair_index] > 1 {
                    if let Some(callback) = observer.as_deref_mut() {
                        let mut event = simulation_event(EventProjection {
                            timestamp_ms,
                            pair: &pair.pair,
                            quote_free: available_balance,
                            is_futures: config.is_futures,
                            configured_pair_index: pair_index,
                            processing_order_index,
                            candle_index: cursor,
                            next_candle_index: cursors[pair_index],
                            slot_limit: config.max_open_trades,
                            open_trades: &open_trades,
                            closed_trades: &closed_trades,
                            rejected_signals,
                            trade_id_counter: next_trade_id.saturating_sub(1),
                            order_id_counter: next_order_id.saturating_sub(1),
                            locks: protection_state.locks(),
                        })?;
                        event.executable_callback_events =
                            executable_events[executable_event_start..].to_vec();
                        event.portfolio_events = portfolio_events[portfolio_event_start..].to_vec();
                        event.execution_events = execution_events[execution_event_start..].to_vec();
                        callback(&event);
                    }
                }
                if let Some(callback) = portfolio_observer.as_deref_mut() {
                    for event in &portfolio_events[portfolio_event_start..] {
                        callback(event);
                    }
                }
                portfolio_events.clear();
                continue;
            }
            let candle_storage =
                pair.candles
                    .try_get(cursor)?
                    .ok_or_else(|| SimError::InvalidExecutionStart {
                        pair: pair.pair.clone(),
                        index: cursor,
                        rows: pair.candles.len(),
                    })?;
            let candle = candle_storage.as_ref();
            debug_assert_eq!(candle.timestamp_ms, timestamp_ms);
            begin_callback_trace(
                cursor,
                callback_feature_index(cursor),
                available_balance,
                existing_trade_index.map(|index| &open_trades[index]),
            )?;

            // Freqtrade includes the timerange stop-boundary row so callbacks
            // and force exits see its open price, but passes `can_enter=false`
            // for that row. Without this gate a shifted signal at the boundary
            // would create and immediately force-close a trade that Freqtrade
            // never opens.
            let can_enter = cursor + 1 < pair.candles.len();
            let entry_request = can_enter
                .then(|| freqtrade_entry_signal(candle, config.is_futures))
                .flatten();
            let opened_now = if let (Some((side, signal)), None) =
                (entry_request, existing_trade_index)
            {
                let entry_boundary_start = portfolio_events.len();
                if execution_observer.is_some() || observer.is_some() {
                    let state = scheduler_observer::state(
                        available_balance,
                        &open_trades,
                        &closed_trades,
                        config.max_open_trades,
                        next_trade_id,
                        next_order_id,
                        rejected_signals,
                    )?;
                    let mut event = execution_observer::event(
                        &mut execution_event_sequence,
                        EventInput {
                            timestamp_ms,
                            pair: &pair.pair,
                            candle,
                            phase: ExecutionBoundary::EntryCandidate,
                            order_type: config.entry_order_type.as_str(),
                            state_before: Some(state.clone()),
                            state_after: Some(state),
                        },
                    );
                    event.candidates.push("entry_signal".to_owned());
                    execution_events.push(event);
                }
                let mut entry = EntryExecution {
                    config,
                    protection_state: &protection_state,
                    closed_trades: &closed_trades,
                    open_trades: &mut open_trades,
                    available_balance: &mut available_balance,
                    rejected_signals: &mut rejected_signals,
                    next_trade_id: &mut next_trade_id,
                    next_order_id: &mut next_order_id,
                    maximum_concurrent_trades: &mut maximum_concurrent_trades,
                    processing_order_index,
                    portfolio_events: &mut portfolio_events,
                    portfolio_event_sequence: &mut portfolio_event_sequence,
                };
                let opened = if let (Some(program), Some(runtime)) = (
                    config.executable_callback_program.as_ref(),
                    executable_runtime.as_mut(),
                ) {
                    let mut callbacks =
                        ExecutableCallbacks::new(program, runtime, &mut executable_events);
                    entry.try_open(pair_index, pair, candle, side, signal, Some(&mut callbacks))?
                } else {
                    entry.try_open(pair_index, pair, candle, side, signal, None)?
                };
                if execution_observer.is_some() || observer.is_some() {
                    if let Some(boundary) = portfolio_events[entry_boundary_start..].last().cloned()
                    {
                        let phase = if opened {
                            ExecutionBoundary::EntryFill
                        } else {
                            ExecutionBoundary::EntryGate
                        };
                        let mut event = execution_observer::event(
                            &mut execution_event_sequence,
                            EventInput {
                                timestamp_ms,
                                pair: &pair.pair,
                                candle,
                                phase,
                                order_type: config.entry_order_type.as_str(),
                                state_before: Some(boundary.state_before),
                                state_after: Some(boundary.state_after),
                            },
                        );
                        event.winner = opened.then(|| "entry_signal".to_owned());
                        event.trade_id = boundary.allocated_trade_id;
                        event.order_id = boundary.allocated_order_id;
                        event.rejection_reason = boundary
                            .rejection_reason
                            .map(execution_observer::rejection_reason)
                            .map(str::to_owned);
                        let requested_entry_rate = if config.entry_order_type == OrderType::Market {
                            candle.open
                        } else {
                            config
                                .entry_rates_by_pair
                                .get(&pair.pair)
                                .and_then(|rates| rates.get(&candle.timestamp_ms))
                                .copied()
                                .unwrap_or(candle.open)
                        };
                        let filled_entry_rate = open_trades
                            .iter()
                            .find(|trade| trade.pair_index == pair_index)
                            .and_then(|trade| trade.orders.last())
                            .map(|order| order.price);
                        event.amount_step = Some(execution_observer::decimal(
                            pair.amount_step.unwrap_or(config.amount_step),
                        ));
                        event.price_input = Some(execution_observer::decimal(candle.open));
                        event.proposed_rate = Some(execution_observer::decimal(candle.open));
                        event.clamped_rate =
                            Some(execution_observer::decimal(requested_entry_rate));
                        event.precision_rate = filled_entry_rate.map(execution_observer::decimal);
                        event.within_candle = Some(
                            filled_entry_rate
                                .is_some_and(|rate| candle.low <= rate && rate <= candle.high),
                        );
                        event.order_status = opened.then_some("filled");
                        event.timeout_checked = Some(false);
                        event.timed_out = Some(false);
                        event.minimum_stake =
                            Some(execution_observer::decimal(minimum_pair_stake(
                                pair,
                                filled_entry_rate.unwrap_or(requested_entry_rate),
                                config.stoploss_ratio,
                                1.0,
                                config.amount_reserve_percent,
                            )));
                        event.minimum_stake_stage = Some("before_order_allocation");
                        event.minimum_stake_accepted = Some(
                            boundary.rejection_reason
                                != Some(crate::EntryRejectionReason::MinimumStake),
                        );
                        event.fee_open = Some(execution_observer::decimal(
                            config.fee_open_rate.unwrap_or(config.fee_rate),
                        ));
                        event.fee_close = Some(execution_observer::decimal(
                            config.fee_close_rate.unwrap_or(config.fee_rate),
                        ));
                        if let Some(proposed_stake) = boundary.proposed_stake {
                            event.intermediates.insert(
                                "proposed_stake".to_owned(),
                                execution_observer::decimal(proposed_stake),
                            );
                        }
                        if let Some(trade) = open_trades
                            .iter()
                            .find(|trade| trade.pair_index == pair_index)
                        {
                            if let Some(order) = trade.orders.last() {
                                event.amount_output =
                                    Some(execution_observer::decimal(order.amount));
                                event.price_step =
                                    Some(execution_observer::decimal(trade.price_step));
                                event.price_output = Some(execution_observer::decimal(order.price));
                                event.fee_applied = Some(execution_observer::fee_amount(
                                    order.amount,
                                    order.price,
                                    config.fee_open_rate.unwrap_or(config.fee_rate),
                                ));
                            }
                        }
                        execution_events.push(event);
                    }
                }
                opened
            } else {
                false
            };

            if opened_now {
                if let (Some(program), Some(runtime), Some(trade_index)) = (
                    config.executable_callback_program.as_ref(),
                    executable_runtime.as_mut(),
                    open_trades
                        .iter()
                        .position(|trade| trade.pair_index == pair_index),
                ) {
                    let current_profit = current_profit_ratio(
                        &open_trades[trade_index],
                        candle.open,
                        config.fee_close_rate.unwrap_or(config.fee_rate),
                    );
                    let mut callbacks =
                        ExecutableCallbacks::new(program, runtime, &mut executable_events);
                    let _ = executable_custom_stoploss(
                        &mut callbacks,
                        &mut open_trades[trade_index],
                        candle,
                        current_profit,
                        true,
                    )?;
                }
                // Freqtrade invokes adjust_trade_position after a new entry fill on
                // the same backtest candle. The legacy adapters never depended on
                // that callback point, but a generic compiled state machine may
                // have a fee-aware guard or source-visible state transition there.
                if let Some(program) = &config.state_machine_program {
                    if program.entrypoints.contains_key("adjust_trade_position") {
                        let trade_index = open_trades
                            .iter()
                            .position(|trade| trade.pair_index == pair_index)
                            .ok_or(SimError::InvalidStateMachineProgram)?;
                        let tied_up_stake = checked_float_sum(
                            &open_trades
                                .iter()
                                .map(|trade| trade.stake_amount)
                                .collect::<Vec<_>>(),
                            "adjustment-tied-up-stake",
                        )?;
                        let adjustment_available = available_stake_amount(
                            available_balance,
                            tied_up_stake,
                            config.tradable_balance_ratio,
                        )?;
                        let feature_index = callback_feature_index(cursor)
                            .ok_or(SimError::InvalidStateMachineProgram)?;
                        let adjustment = evaluate_state_machine_adjustment(
                            program,
                            &mut open_trades[trade_index],
                            pair,
                            feature_index,
                            candle,
                            config,
                            adjustment_available,
                        )?;
                        trace_trade_callback(
                            crate::CallbackPhase::PositionAdjustment,
                            adjustment
                                .as_ref()
                                .map_or(crate::CallbackOutcome::None, |_| {
                                    crate::CallbackOutcome::Value
                                }),
                            crate::CallbackTransaction::Committed,
                            adjustment_available,
                            &open_trades[trade_index],
                            None,
                        )?;
                        if let Some(adjustment) = adjustment {
                            let order_count = open_trades[trade_index].orders.len();
                            let state_before = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            apply_adjustment(
                                &mut open_trades[trade_index],
                                pair,
                                candle,
                                &adjustment,
                                config,
                                adjustment_available,
                                next_order_id,
                            )?;
                            if open_trades[trade_index].orders.len() > order_count {
                                next_order_id += 1;
                            }
                            available_balance = wallet_free(
                                config.starting_balance,
                                &open_trades,
                                &closed_trades,
                                config.is_futures,
                            )?;
                            if open_trades[trade_index].orders.len() > order_count {
                                let state_after = scheduler_observer::state(
                                    available_balance,
                                    &open_trades,
                                    &closed_trades,
                                    config.max_open_trades,
                                    next_trade_id,
                                    next_order_id,
                                    rejected_signals,
                                )?;
                                portfolio_events.push(scheduler_observer::adjustment_event(
                                    &mut portfolio_event_sequence,
                                    &boundary_context,
                                    state_before.clone(),
                                    state_after.clone(),
                                    adjustment.stake_amount < 0.0,
                                ));
                                if execution_observer.is_some() || observer.is_some() {
                                    if let Some(event) = execution_observer::adjustment_event(
                                        &mut execution_event_sequence,
                                        AdjustmentEventInput {
                                            pair,
                                            candle,
                                            trade: &open_trades[trade_index],
                                            adjustment: &adjustment,
                                            config,
                                            state_before,
                                            state_after,
                                        },
                                    ) {
                                        execution_events.push(event);
                                    }
                                }
                                trace_trade_callback(
                                    crate::CallbackPhase::OrderFilled,
                                    crate::CallbackOutcome::Accepted,
                                    crate::CallbackTransaction::Committed,
                                    available_balance,
                                    &open_trades[trade_index],
                                    None,
                                )?;
                            }
                        }
                    }
                } else if let (Some(manager), Some(feature_index)) =
                    (&config.nfi_x7_trade_manager, callback_feature_index(cursor))
                {
                    let trade_index = open_trades
                        .iter()
                        .position(|trade| trade.pair_index == pair_index)
                        .ok_or_else(|| SimError::InvalidPositionAdjustment {
                            pair: pair.pair.clone(),
                            timestamp_ms: candle.timestamp_ms,
                        })?;
                    let tied_up_stake = checked_float_sum(
                        &open_trades
                            .iter()
                            .map(|trade| trade.stake_amount)
                            .collect::<Vec<_>>(),
                        "adjustment-tied-up-stake",
                    )?;
                    let adjustment_available = available_stake_amount(
                        available_balance,
                        tied_up_stake,
                        config.tradable_balance_ratio,
                    )?;
                    let adjustment = evaluate_nfi_position_adjustment(
                        manager,
                        &mut open_trades[trade_index],
                        &PositionAdjustmentRequest {
                            pair,
                            candle_index: feature_index,
                            candle,
                            config,
                            available_balance: adjustment_available,
                        },
                    )?
                    .ok_or_else(|| SimError::InvalidPositionAdjustment {
                        pair: pair.pair.clone(),
                        timestamp_ms: candle.timestamp_ms,
                    })?;
                    if let Some(adjustment) = adjustment {
                        let order_count = open_trades[trade_index].orders.len();
                        let state_before = scheduler_observer::state(
                            available_balance,
                            &open_trades,
                            &closed_trades,
                            config.max_open_trades,
                            next_trade_id,
                            next_order_id,
                            rejected_signals,
                        )?;
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            pair,
                            candle,
                            &adjustment,
                            config,
                            adjustment_available,
                            next_order_id,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            next_order_id += 1;
                        }
                        available_balance = wallet_free(
                            config.starting_balance,
                            &open_trades,
                            &closed_trades,
                            config.is_futures,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            let state_after = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            portfolio_events.push(scheduler_observer::adjustment_event(
                                &mut portfolio_event_sequence,
                                &boundary_context,
                                state_before.clone(),
                                state_after.clone(),
                                adjustment.stake_amount < 0.0,
                            ));
                            if execution_observer.is_some() || observer.is_some() {
                                if let Some(event) = execution_observer::adjustment_event(
                                    &mut execution_event_sequence,
                                    AdjustmentEventInput {
                                        pair,
                                        candle,
                                        trade: &open_trades[trade_index],
                                        adjustment: &adjustment,
                                        config,
                                        state_before,
                                        state_after,
                                    },
                                ) {
                                    execution_events.push(event);
                                }
                            }
                            trace_trade_callback(
                                crate::CallbackPhase::OrderFilled,
                                crate::CallbackOutcome::Accepted,
                                crate::CallbackTransaction::Committed,
                                available_balance,
                                &open_trades[trade_index],
                                None,
                            )?;
                        }
                    }
                }
            }

            let same_candle_liquidation = opened_now
                && open_trades
                    .iter()
                    .find(|trade| trade.pair_index == pair_index)
                    .is_some_and(|trade| liquidation_reached(trade, candle));
            // Official Backtesting evaluates ROI and futures liquidation after
            // a successful entry on the same candle. Legacy callback routes
            // retain their prior first-candle boundary for all other exits
            // until they carry an ROI table.
            if !opened_now
                || config.executable_callback_program.is_some()
                || !config.minimal_roi.is_empty()
                || same_candle_liquidation
            {
                let mut exited_side = None;
                if let Some(trade_index) = open_trades
                    .iter()
                    .position(|trade| trade.pair_index == pair_index)
                {
                    if !opened_now {
                        update_extrema(&mut open_trades[trade_index], candle);
                        apply_funding(
                            &mut open_trades[trade_index],
                            candle,
                            config.funding_fee_interval_ms,
                        )?;
                    }
                    // Freqtrade exposes `wallets.get_available_stake_amount()`
                    // as the callback's max_stake. This is smaller than raw
                    // free balance when tradable_balance_ratio keeps a wallet
                    // reserve, and NFI intentionally rejects an adjustment
                    // that exceeds this boundary instead of clamping it.
                    let tied_up_stake = checked_float_sum(
                        &open_trades
                            .iter()
                            .map(|trade| trade.stake_amount)
                            .collect::<Vec<_>>(),
                        "adjustment-tied-up-stake",
                    )?;
                    let adjustment_available = available_stake_amount(
                        available_balance,
                        tied_up_stake,
                        config.tradable_balance_ratio,
                    )?;
                    let adjustment_already_processed =
                        opened_now && config.executable_callback_program.is_none();
                    let adjustment = if adjustment_already_processed {
                        None
                    } else if let (Some(program), Some(runtime)) = (
                        config.executable_callback_program.as_ref(),
                        executable_runtime.as_mut(),
                    ) {
                        let current_profit = current_profit_ratio(
                            &open_trades[trade_index],
                            candle.open,
                            config.fee_close_rate.unwrap_or(config.fee_rate),
                        );
                        let minimum_stake = adjustment_minimum_pair_stake(
                            pair,
                            candle.open,
                            config.amount_reserve_percent,
                        );
                        let mut callbacks =
                            ExecutableCallbacks::new(program, runtime, &mut executable_events);
                        executable_position_adjustment(
                            &mut callbacks,
                            &mut open_trades[trade_index],
                            pair,
                            candle,
                            minimum_stake,
                            adjustment_available,
                            current_profit,
                        )?
                    } else if let Some(adjustment) = &candle.adjustment {
                        Some(adjustment.clone())
                    } else if let Some(program) = &config.state_machine_program {
                        if program.entrypoints.contains_key("adjust_trade_position") {
                            let feature_index = callback_feature_index(cursor)
                                .ok_or(SimError::InvalidStateMachineProgram)?;
                            evaluate_state_machine_adjustment(
                                program,
                                &mut open_trades[trade_index],
                                pair,
                                feature_index,
                                candle,
                                config,
                                adjustment_available,
                            )?
                        } else {
                            None
                        }
                    } else if let Some(manager) = &config.nfi_x7_trade_manager {
                        let feature_index = callback_feature_index(cursor).ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?;
                        evaluate_nfi_position_adjustment(
                            manager,
                            &mut open_trades[trade_index],
                            &PositionAdjustmentRequest {
                                pair,
                                candle_index: feature_index,
                                candle,
                                config,
                                available_balance: adjustment_available,
                            },
                        )?
                        .ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?
                    } else if let Some(bundle) = &config.adjust_trade_position_program {
                        let feature_index = callback_feature_index(cursor).ok_or_else(|| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?;
                        evaluate_adjustment_bundle(
                            bundle,
                            &open_trades[trade_index],
                            pair,
                            feature_index,
                            candle,
                            config,
                            adjustment_available,
                        )
                        .map_err(|()| {
                            SimError::InvalidPositionAdjustment {
                                pair: pair.pair.clone(),
                                timestamp_ms: candle.timestamp_ms,
                            }
                        })?
                    } else {
                        rule_adjustment(&open_trades[trade_index], candle, config)
                    };
                    let generic_adjustment_callback = config
                        .state_machine_program
                        .as_ref()
                        .is_some_and(|program| {
                            program.entrypoints.contains_key("adjust_trade_position")
                        })
                        || config.adjust_trade_position_program.is_some();
                    if generic_adjustment_callback && !adjustment_already_processed {
                        trace_trade_callback(
                            crate::CallbackPhase::PositionAdjustment,
                            adjustment
                                .as_ref()
                                .map_or(crate::CallbackOutcome::None, |_| {
                                    crate::CallbackOutcome::Value
                                }),
                            crate::CallbackTransaction::Committed,
                            adjustment_available,
                            &open_trades[trade_index],
                            None,
                        )?;
                    }
                    if let Some(adjustment) = adjustment {
                        let order_count = open_trades[trade_index].orders.len();
                        let adjustment_state_before = scheduler_observer::state(
                            available_balance,
                            &open_trades,
                            &closed_trades,
                            config.max_open_trades,
                            next_trade_id,
                            next_order_id,
                            rejected_signals,
                        )?;
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            pair,
                            candle,
                            &adjustment,
                            config,
                            adjustment_available,
                            next_order_id,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            next_order_id += 1;
                        }
                        available_balance = wallet_free(
                            config.starting_balance,
                            &open_trades,
                            &closed_trades,
                            config.is_futures,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            let adjustment_state_after = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            portfolio_events.push(scheduler_observer::adjustment_event(
                                &mut portfolio_event_sequence,
                                &boundary_context,
                                adjustment_state_before.clone(),
                                adjustment_state_after.clone(),
                                adjustment.stake_amount < 0.0,
                            ));
                            if execution_observer.is_some() || observer.is_some() {
                                if let Some(event) = execution_observer::adjustment_event(
                                    &mut execution_event_sequence,
                                    AdjustmentEventInput {
                                        pair,
                                        candle,
                                        trade: &open_trades[trade_index],
                                        adjustment: &adjustment,
                                        config,
                                        state_before: adjustment_state_before,
                                        state_after: adjustment_state_after,
                                    },
                                ) {
                                    execution_events.push(event);
                                }
                            }
                            if let (Some(program), Some(runtime)) = (
                                config.executable_callback_program.as_ref(),
                                executable_runtime.as_mut(),
                            ) {
                                let mut callbacks = ExecutableCallbacks::new(
                                    program,
                                    runtime,
                                    &mut executable_events,
                                );
                                executable_order_filled(
                                    &mut callbacks,
                                    &mut open_trades[trade_index],
                                    available_balance,
                                )?;
                                let current_profit = current_profit_ratio(
                                    &open_trades[trade_index],
                                    candle.open,
                                    config.fee_close_rate.unwrap_or(config.fee_rate),
                                );
                                if let Some(stop_ratio) = executable_custom_stoploss(
                                    &mut callbacks,
                                    &mut open_trades[trade_index],
                                    candle,
                                    current_profit,
                                    true,
                                )? {
                                    open_trades[trade_index].custom_stop_loss_ratio =
                                        Some(-stop_ratio.abs());
                                }
                            } else {
                                trace_trade_callback(
                                    crate::CallbackPhase::OrderFilled,
                                    crate::CallbackOutcome::Accepted,
                                    crate::CallbackTransaction::Committed,
                                    available_balance,
                                    &open_trades[trade_index],
                                    None,
                                )?;
                            }
                        }
                    }
                    if let (Some(program), Some(runtime)) = (
                        config.executable_callback_program.as_ref(),
                        executable_runtime.as_mut(),
                    ) {
                        let current_profit = current_profit_ratio(
                            &open_trades[trade_index],
                            candle.open,
                            config.fee_close_rate.unwrap_or(config.fee_rate),
                        );
                        let mut callbacks =
                            ExecutableCallbacks::new(program, runtime, &mut executable_events);
                        if let Some(stop_ratio) = executable_custom_stoploss(
                            &mut callbacks,
                            &mut open_trades[trade_index],
                            candle,
                            current_profit,
                            false,
                        )? {
                            open_trades[trade_index].custom_stop_loss_ratio =
                                Some(-stop_ratio.abs());
                            let candidate = match open_trades[trade_index].side {
                                TradeSide::Long => ceil_step(
                                    candle.high
                                        * (1.0
                                            - stop_ratio.abs() / open_trades[trade_index].leverage),
                                    open_trades[trade_index].price_step,
                                )?,
                                TradeSide::Short => floor_step(
                                    candle.low
                                        * (1.0
                                            + stop_ratio.abs() / open_trades[trade_index].leverage),
                                    open_trades[trade_index].price_step,
                                )?,
                            };
                            match open_trades[trade_index].side {
                                TradeSide::Long => {
                                    open_trades[trade_index].stop_loss =
                                        open_trades[trade_index].stop_loss.max(candidate);
                                }
                                TradeSide::Short => {
                                    open_trades[trade_index].stop_loss =
                                        open_trades[trade_index].stop_loss.min(candidate);
                                }
                            }
                        }
                    } else {
                        trace_trade_callback(
                            crate::CallbackPhase::CustomStoploss,
                            crate::CallbackOutcome::None,
                            crate::CallbackTransaction::Committed,
                            available_balance,
                            &open_trades[trade_index],
                            None,
                        )?;
                    }
                    let executable_exit = if let (Some(program), Some(runtime)) = (
                        config.executable_callback_program.as_ref(),
                        executable_runtime.as_mut(),
                    ) {
                        let current_profit = current_profit_ratio(
                            &open_trades[trade_index],
                            candle.open,
                            config.fee_close_rate.unwrap_or(config.fee_rate),
                        );
                        let mut callbacks =
                            ExecutableCallbacks::new(program, runtime, &mut executable_events);
                        executable_custom_exit(
                            &mut callbacks,
                            &mut open_trades[trade_index],
                            candle,
                            current_profit,
                        )?
                        .map(|reason| ExitDecision {
                            rate: candle.open,
                            reason,
                            requires_confirmation: true,
                        })
                    } else {
                        None
                    };
                    let exit_candidates =
                        if opened_now && config.executable_callback_program.is_none() {
                            trace_trade_callback(
                                crate::CallbackPhase::CustomExit,
                                crate::CallbackOutcome::None,
                                crate::CallbackTransaction::Committed,
                                available_balance,
                                &open_trades[trade_index],
                                None,
                            )?;
                            ordered_risk_candidates(
                                &mut open_trades[trade_index],
                                candle,
                                config,
                                None,
                            )?
                        } else if let Some(exit) = executable_exit {
                            ordered_risk_candidates(
                                &mut open_trades[trade_index],
                                candle,
                                config,
                                Some(exit),
                            )?
                        } else {
                            exit_decisions(
                                &mut open_trades[trade_index],
                                pair,
                                cursor,
                                candle,
                                config,
                                &mut profit_targets,
                            )?
                        };
                    if (execution_observer.is_some() || observer.is_some())
                        && !exit_candidates.is_empty()
                    {
                        let state = scheduler_observer::state(
                            available_balance,
                            &open_trades,
                            &closed_trades,
                            config.max_open_trades,
                            next_trade_id,
                            next_order_id,
                            rejected_signals,
                        )?;
                        let mut event = execution_observer::event(
                            &mut execution_event_sequence,
                            EventInput {
                                timestamp_ms,
                                pair: &pair.pair,
                                candle,
                                phase: ExecutionBoundary::ExitCompetition,
                                order_type: config.exit_order_type.as_str(),
                                state_before: Some(state.clone()),
                                state_after: Some(state),
                            },
                        );
                        event.candidates = exit_candidates
                            .iter()
                            .map(|candidate| candidate.reason.clone())
                            .collect();
                        execution_events.push(event);
                    }
                    for exit in exit_candidates {
                        let (confirmed, clear_profit_target) = if exit.requires_confirmation {
                            if let (Some(program), Some(runtime)) = (
                                config.executable_callback_program.as_ref(),
                                executable_runtime.as_mut(),
                            ) {
                                let mut callbacks = ExecutableCallbacks::new(
                                    program,
                                    runtime,
                                    &mut executable_events,
                                );
                                (
                                    executable_exit_confirmation(
                                        &mut callbacks,
                                        &mut open_trades[trade_index],
                                        candle,
                                        &exit,
                                        config.is_futures,
                                        config.exit_order_type,
                                    )?,
                                    false,
                                )
                            } else if let Some(program) = &config.exit_confirmation_program {
                                evaluate_exit_confirm_program(
                                    program,
                                    &open_trades[trade_index],
                                    candle.timestamp_ms,
                                    exit.rate,
                                    &exit.reason,
                                    config,
                                )
                                .ok_or_else(|| {
                                    SimError::InvalidExitConfirmation {
                                        pair: pair.pair.clone(),
                                        timestamp_ms: candle.timestamp_ms,
                                    }
                                })?
                            } else {
                                (true, false)
                            }
                        } else {
                            // Freqtrade deliberately bypasses
                            // confirm_trade_exit for liquidation orders.
                            (true, false)
                        };
                        if execution_observer.is_some() || observer.is_some() {
                            let state = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            let mut event = execution_observer::event(
                                &mut execution_event_sequence,
                                EventInput {
                                    timestamp_ms,
                                    pair: &pair.pair,
                                    candle,
                                    phase: ExecutionBoundary::ExitConfirmation,
                                    order_type: config.exit_order_type.as_str(),
                                    state_before: Some(state.clone()),
                                    state_after: Some(state),
                                },
                            );
                            event.winner = Some(exit.reason.clone());
                            event.confirmation = Some(confirmed);
                            event.trade_id = Some(open_trades[trade_index].id);
                            event.price_input = Some(execution_observer::decimal(exit.rate));
                            execution_events.push(event);
                        }
                        if exit.requires_confirmation
                            && config.executable_callback_program.is_none()
                        {
                            trace_trade_callback(
                                crate::CallbackPhase::ExitConfirmation,
                                if confirmed {
                                    crate::CallbackOutcome::Accepted
                                } else {
                                    crate::CallbackOutcome::Rejected
                                },
                                crate::CallbackTransaction::Committed,
                                available_balance,
                                &open_trades[trade_index],
                                None,
                            )?;
                        }
                        if confirmed {
                            if clear_profit_target {
                                profit_targets.remove(&pair.pair);
                            }
                            let close_state_before = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            let allocated_order_id = next_order_id;
                            let frozen_price_step = open_trades[trade_index].price_step;
                            // LocalTrade removes a closed trade without
                            // reordering the remaining open-trade list. That
                            // insertion order becomes the processing prefix
                            // on the next candle and can affect shared-wallet
                            // decisions, so a swap removal is not equivalent.
                            let trade = open_trades.remove(trade_index);
                            exited_side = Some(trade.side);
                            let (closed, _) = if let (Some(program), Some(runtime)) = (
                                config.executable_callback_program.as_ref(),
                                executable_runtime.as_mut(),
                            ) {
                                let mut callbacks = ExecutableCallbacks::new(
                                    program,
                                    runtime,
                                    &mut executable_events,
                                );
                                close_trade(
                                    trade,
                                    candle.timestamp_ms,
                                    exit.rate,
                                    exit.reason,
                                    config,
                                    CloseTradeContext {
                                        sequence: closed_trades.len(),
                                        order_id: next_order_id,
                                        executable_callbacks: Some(&mut callbacks),
                                        wallet_available_before: available_balance,
                                    },
                                )?
                            } else {
                                close_trade(
                                    trade,
                                    candle.timestamp_ms,
                                    exit.rate,
                                    exit.reason,
                                    config,
                                    CloseTradeContext {
                                        sequence: closed_trades.len(),
                                        order_id: next_order_id,
                                        executable_callbacks: None,
                                        wallet_available_before: available_balance,
                                    },
                                )?
                            };
                            next_order_id += 1;
                            closed_trades.push(closed);
                            if let (Some(program), Some(closed_trade)) =
                                (&config.protection_program, closed_trades.last())
                            {
                                protection_state.after_trade_close(
                                    program,
                                    closed_trade,
                                    &closed_trades,
                                    config.starting_balance,
                                )?;
                            }
                            available_balance = wallet_free(
                                config.starting_balance,
                                &open_trades,
                                &closed_trades,
                                config.is_futures,
                            )?;
                            let close_state_after = scheduler_observer::state(
                                available_balance,
                                &open_trades,
                                &closed_trades,
                                config.max_open_trades,
                                next_trade_id,
                                next_order_id,
                                rejected_signals,
                            )?;
                            let mut detail = BoundaryDetail::plain();
                            detail.allocated_order_id = Some(allocated_order_id);
                            portfolio_events.push(scheduler_observer::event(
                                &mut portfolio_event_sequence,
                                &boundary_context,
                                PortfolioBoundary::TradeClose,
                                close_state_before.clone(),
                                close_state_after.clone(),
                                detail,
                            ));
                            if execution_observer.is_some() || observer.is_some() {
                                let closed = closed_trades
                                    .last()
                                    .ok_or(SimError::InvalidCallbackRuntime)?;
                                let order = closed
                                    .orders
                                    .last()
                                    .ok_or(SimError::InvalidCallbackRuntime)?;
                                execution_events.push(execution_observer::exit_fill_event(
                                    &mut execution_event_sequence,
                                    ExitFillEventInput {
                                        pair,
                                        candle,
                                        closed,
                                        order,
                                        requested_rate: exit.rate,
                                        frozen_price_step,
                                        config,
                                        state_before: close_state_before,
                                        state_after: close_state_after,
                                    },
                                ));
                            }
                            break;
                        }
                    }
                }
                // Freqtrade's futures backtest loop receives the current entry
                // direction before processing the existing trade. If that
                // trade closes and the signal points the other way, it invokes
                // the same pair row a second time and may open the reversal at
                // the identical timestamp. A same-direction signal does not
                // get this second pass.
                if config.is_futures {
                    if let (Some(previous_side), Some((side, signal))) =
                        (exited_side, entry_request)
                    {
                        if side != previous_side {
                            let mut entry = EntryExecution {
                                config,
                                protection_state: &protection_state,
                                closed_trades: &closed_trades,
                                open_trades: &mut open_trades,
                                available_balance: &mut available_balance,
                                rejected_signals: &mut rejected_signals,
                                next_trade_id: &mut next_trade_id,
                                next_order_id: &mut next_order_id,
                                maximum_concurrent_trades: &mut maximum_concurrent_trades,
                                processing_order_index,
                                portfolio_events: &mut portfolio_events,
                                portfolio_event_sequence: &mut portfolio_event_sequence,
                            };
                            if let (Some(program), Some(runtime)) = (
                                config.executable_callback_program.as_ref(),
                                executable_runtime.as_mut(),
                            ) {
                                let mut callbacks = ExecutableCallbacks::new(
                                    program,
                                    runtime,
                                    &mut executable_events,
                                );
                                entry.try_open(
                                    pair_index,
                                    pair,
                                    candle,
                                    side,
                                    signal,
                                    Some(&mut callbacks),
                                )?;
                            } else {
                                entry.try_open(pair_index, pair, candle, side, signal, None)?;
                            }
                        }
                    }
                }
            }
            let next_cursor = cursor + 1;
            let pair_has_open_trade = open_trades
                .iter()
                .any(|trade| trade.pair_index == pair_index);
            cursors[pair_index] =
                scheduled_cursor(pair, next_cursor, pair_has_open_trade, sparse_execution);
            next_timestamps[pair_index] = pair.candles.timestamp_ms(cursors[pair_index]);
            let callback_events = finish_callback_trace()?;
            if cursors[pair_index] > 1 {
                if let Some(callback) = observer.as_deref_mut() {
                    let mut event = simulation_event(EventProjection {
                        timestamp_ms: candle.timestamp_ms,
                        pair: &pair.pair,
                        quote_free: available_balance,
                        is_futures: config.is_futures,
                        configured_pair_index: pair_index,
                        processing_order_index,
                        candle_index: cursor,
                        next_candle_index: cursors[pair_index],
                        slot_limit: config.max_open_trades,
                        open_trades: &open_trades,
                        closed_trades: &closed_trades,
                        rejected_signals,
                        trade_id_counter: next_trade_id.saturating_sub(1),
                        order_id_counter: next_order_id.saturating_sub(1),
                        locks: protection_state.locks(),
                    })?;
                    event.callback_events = callback_events;
                    event.executable_callback_events =
                        executable_events[executable_event_start..].to_vec();
                    event.portfolio_events = portfolio_events[portfolio_event_start..].to_vec();
                    event.execution_events = execution_events[execution_event_start..].to_vec();
                    callback(&event);
                }
            }
            if let Some(callback) = portfolio_observer.as_deref_mut() {
                for event in &portfolio_events[portfolio_event_start..] {
                    callback(event);
                }
            }
            if let Some(callback) = execution_observer.as_deref_mut() {
                for event in &execution_events[execution_event_start..] {
                    callback(event);
                }
            }
            portfolio_events.clear();
            execution_events.clear();
        }
    }

    let event_loop_ns = duration_ns(event_loop_started.elapsed());
    let finalization_started = Instant::now();
    let result = finalize_simulation(FinalizationInput {
        input,
        open_trades,
        closed_trades,
        protection_state,
        rejected_signals,
        maximum_concurrent_trades,
        next_trade_id,
        next_order_id,
        portfolio_event_sequence: &mut portfolio_event_sequence,
        portfolio_observer,
        execution_event_sequence: &mut execution_event_sequence,
        execution_observer,
    })?;
    let profile = build_simulation_profile(
        validation_ns,
        event_loop_ns,
        duration_ns(finalization_started.elapsed()),
        timestamp_batches,
        pair_events,
    );
    Ok((result, profile))
}
