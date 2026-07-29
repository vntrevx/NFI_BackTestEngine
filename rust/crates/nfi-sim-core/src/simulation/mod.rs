use std::collections::BTreeMap;
use std::time::Instant;

mod event;
mod output;

use event::simulation_event;
use output::finalize_simulation;

use crate::calculations::{
    available_stake_amount, duration_ns, logical_pair_event_count, scheduled_cursor,
};
use crate::callbacks::{callback_feature_index, evaluate_adjustment_bundle};
use crate::execution::{
    apply_adjustment, close_trade, evaluate_exit_confirm_program, exit_decision, rule_adjustment,
    update_extrema, EntryExecution,
};
use crate::futures::apply_funding;
use crate::nfi::{evaluate_nfi_position_adjustment, PositionAdjustmentRequest, ProfitTarget};
use crate::portfolio::{wallet_free, OpenTrade};
use crate::profiling::build_simulation_profile;
use crate::protections::ProtectionState;
use crate::validation::{freqtrade_entry_signal, validate_input};
use crate::{SimError, SimulationEvent, SimulationInput, SimulationProfile, SimulationResult};

#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub(super) fn simulate_internal(
    input: &SimulationInput,
    mut observer: Option<&mut dyn FnMut(&SimulationEvent)>,
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

    while let Some(timestamp_ms) = next_timestamps.iter().flatten().copied().min() {
        if !sparse_execution {
            timestamp_batches += 1;
        }
        processing_order.clear();
        for trade in &open_trades {
            if !open_pair_flags[trade.pair_index] {
                open_pair_flags[trade.pair_index] = true;
                processing_order.push(trade.pair_index);
            }
        }
        for (pair_index, is_open) in open_pair_flags.iter().copied().enumerate() {
            if !is_open {
                processing_order.push(pair_index);
            }
        }
        for pair_index in processing_order.iter().copied() {
            open_pair_flags[pair_index] = false;
        }

        for pair_index in processing_order.iter().copied() {
            let pair = &input.pairs[pair_index];
            if next_timestamps[pair_index] != Some(timestamp_ms) {
                continue;
            }
            let cursor = cursors[pair_index];
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
                        callback(&simulation_event(
                            timestamp_ms,
                            &pair.pair,
                            available_balance,
                            &open_trades,
                            &closed_trades,
                            rejected_signals,
                            protection_state.locks(),
                        ));
                    }
                }
                continue;
            }
            let candle_storage = pair
                .candles
                .get(cursor)
                .expect("cached pair timestamp identifies a readable candle");
            let candle = candle_storage.as_ref();
            debug_assert_eq!(candle.timestamp_ms, timestamp_ms);

            // Freqtrade includes the timerange stop-boundary row so callbacks
            // and force exits see its open price, but passes `can_enter=false`
            // for that row. Without this gate a shifted signal at the boundary
            // would create and immediately force-close a trade that Freqtrade
            // never opens.
            let can_enter = cursor + 1 < pair.candles.len();
            let entry_request = can_enter
                .then(|| freqtrade_entry_signal(candle, config.is_futures))
                .flatten();
            let opened_now =
                if let (Some((side, signal)), None) = (entry_request, existing_trade_index) {
                    EntryExecution {
                        config,
                        protection_state: &protection_state,
                        closed_trades: &closed_trades,
                        open_trades: &mut open_trades,
                        available_balance: &mut available_balance,
                        rejected_signals: &mut rejected_signals,
                        next_trade_id: &mut next_trade_id,
                        next_order_id: &mut next_order_id,
                        maximum_concurrent_trades: &mut maximum_concurrent_trades,
                    }
                    .try_open(pair_index, pair, candle, side, signal)?
                } else {
                    false
                };

            if !opened_now {
                let mut exited_side = None;
                if let Some(trade_index) = open_trades
                    .iter()
                    .position(|trade| trade.pair_index == pair_index)
                {
                    update_extrema(&mut open_trades[trade_index], candle);
                    apply_funding(
                        &mut open_trades[trade_index],
                        candle,
                        config.funding_fee_interval_ms,
                    );
                    // Freqtrade exposes `wallets.get_available_stake_amount()`
                    // as the callback's max_stake. This is smaller than raw
                    // free balance when tradable_balance_ratio keeps a wallet
                    // reserve, and NFI intentionally rejects an adjustment
                    // that exceeds this boundary instead of clamping it.
                    let tied_up_stake = open_trades
                        .iter()
                        .map(|trade| trade.stake_amount)
                        .sum::<f64>();
                    let adjustment_available = available_stake_amount(
                        available_balance,
                        tied_up_stake,
                        config.tradable_balance_ratio,
                    );
                    let adjustment = if let Some(adjustment) = &candle.adjustment {
                        Some(adjustment.clone())
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
                        )
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
                    if let Some(adjustment) = adjustment {
                        let order_count = open_trades[trade_index].orders.len();
                        apply_adjustment(
                            &mut open_trades[trade_index],
                            candle,
                            &adjustment,
                            config,
                            adjustment_available,
                            next_order_id,
                        )?;
                        if open_trades[trade_index].orders.len() > order_count {
                            next_order_id += 1;
                        }
                        available_balance =
                            wallet_free(config.starting_balance, &open_trades, &closed_trades);
                    }
                    if let Some(exit) = exit_decision(
                        &open_trades[trade_index],
                        pair,
                        cursor,
                        candle,
                        config,
                        &mut profit_targets,
                    )? {
                        let (confirmed, clear_profit_target) = if exit.requires_confirmation {
                            if let Some(program) = &config.exit_confirmation_program {
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
                        if confirmed {
                            if clear_profit_target {
                                profit_targets.remove(&pair.pair);
                            }
                            // LocalTrade removes a closed trade without
                            // reordering the remaining open-trade list. That
                            // insertion order becomes the processing prefix
                            // on the next candle and can affect shared-wallet
                            // decisions, so a swap removal is not equivalent.
                            let trade = open_trades.remove(trade_index);
                            exited_side = Some(trade.side);
                            let (closed, _) = close_trade(
                                trade,
                                candle.timestamp_ms,
                                exit.rate,
                                exit.reason,
                                config,
                                closed_trades.len(),
                                next_order_id,
                            );
                            next_order_id += 1;
                            closed_trades.push(closed);
                            if let Some(program) = &config.protection_program {
                                let closed_trade = closed_trades
                                    .last()
                                    .expect("a closed trade was appended immediately above");
                                protection_state.after_trade_close(
                                    program,
                                    closed_trade,
                                    &closed_trades,
                                    config.starting_balance,
                                );
                            }
                            available_balance =
                                wallet_free(config.starting_balance, &open_trades, &closed_trades);
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
                            EntryExecution {
                                config,
                                protection_state: &protection_state,
                                closed_trades: &closed_trades,
                                open_trades: &mut open_trades,
                                available_balance: &mut available_balance,
                                rejected_signals: &mut rejected_signals,
                                next_trade_id: &mut next_trade_id,
                                next_order_id: &mut next_order_id,
                                maximum_concurrent_trades: &mut maximum_concurrent_trades,
                            }
                            .try_open(pair_index, pair, candle, side, signal)?;
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
            if cursors[pair_index] > 1 {
                if let Some(callback) = observer.as_deref_mut() {
                    callback(&simulation_event(
                        candle.timestamp_ms,
                        &pair.pair,
                        available_balance,
                        &open_trades,
                        &closed_trades,
                        rejected_signals,
                        protection_state.locks(),
                    ));
                }
            }
        }
    }

    let event_loop_ns = duration_ns(event_loop_started.elapsed());
    let finalization_started = Instant::now();
    let result = finalize_simulation(
        input,
        open_trades,
        closed_trades,
        protection_state,
        rejected_signals,
        maximum_concurrent_trades,
        next_order_id,
    );
    let profile = build_simulation_profile(
        validation_ns,
        event_loop_ns,
        duration_ns(finalization_started.elapsed()),
        timestamp_batches,
        pair_events,
    );
    Ok((result, profile))
}
