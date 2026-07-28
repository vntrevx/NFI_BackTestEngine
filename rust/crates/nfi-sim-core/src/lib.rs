//! Deterministic global chronological portfolio simulator.
//!
//! Signals cross this boundary as complete arrays. The core never calls Python
//! per candle and never simulates pairs independently before merging results.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use num_traits::ToPrimitive;
use serde_json::Value;

mod calculations;
use calculations::{
    available_stake_amount, duration_ns, fee_close, fee_open, logical_pair_event_count,
    pairwise_sum, python_float_sum, scheduled_cursor,
};
#[cfg(test)]
use calculations::{
    ceil_step, entry_sizing, exact_rational, floor_step, ft_precise_division, precise_product,
    precise_product_quotient, round_eight, round_step,
};
mod domain;
mod io;
use io::CALLBACK_FEATURE_LOOKBACK_ROWS;
pub use io::{
    parse_simulation_input, serialize_simulation_result, CandleSeries, CandleSeriesIter,
    FeatureColumn, FileBackedFeatureKind, FileBackedRows, FILE_BACKED_FEATURE_BYTES,
    FILE_BACKED_ROW_HEADER_BYTES, TRADE_SURFACE_SCHEMA_VERSION,
};
mod portfolio;
mod validation;
use portfolio::{wallet_free, OpenTrade, TradeSide};
mod execution;
use domain::FeatureProjection;
pub use domain::*;
use execution::{
    adjustment_minimum_pair_stake, apply_adjustment, close_trade, current_profit_ratio,
    evaluate_exit_confirm_program, exit_decision, rule_adjustment, update_extrema, EntryExecution,
};
#[cfg(test)]
use execution::{
    enter_trade, evaluate_confirm_program, evaluate_stake_program, minimum_pair_stake,
    pair_price_step, ConfirmInputs, EntryRequest, EntryStake, StakeInputs,
};
use validation::{
    freqtrade_entry_signal, nfi_entry_signal_is_supported, nfi_managed_route_supports_tags,
    nfi_managed_short_route_supports_tags, validate_input,
};

mod nfi_adjustment;
use nfi_adjustment::evaluate_nfi_position_adjustment as evaluate_nfi_system_v3_adjustment;
mod nfi_legacy_grind;
use nfi_legacy_grind::evaluate_nfi_legacy_grind_adjustment;
mod nfi_regular_adjustment;
use nfi_regular_adjustment::{evaluate_nfi_regular_adjustment, RegularAdjustmentOutcome};
mod nfi_rebuy;
use nfi_rebuy::{evaluate_nfi_rebuy_adjustment, evaluate_nfi_short_rebuy_adjustment};
mod protections;
use protections::{PairLockState, ProtectionState};

/// Version of the simulator input/result contract.
pub const SIMULATOR_SCHEMA_VERSION: &str = "1.0.0";
/// Immutable inputs shared by every NFI position-adjustment route.
///
/// Keeping the callback boundary in one value makes route dispatch readable
/// and prevents future callback fields from expanding every function
/// signature independently.
#[derive(Clone, Copy)]
struct PositionAdjustmentRequest<'a> {
    pair: &'a PairSeries,
    candle_index: usize,
    candle: &'a Candle,
    config: &'a PortfolioConfig,
    available_balance: f64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct NfiProfitSnapshot {
    stake: f64,
    ratio: f64,
    current_stake_ratio: f64,
    initial_stake_ratio: f64,
}

#[derive(Debug, Clone, PartialEq)]
struct ProfitTarget {
    rate: f64,
    profit: f64,
    sell_reason: String,
    time_profit_reached_ms: i64,
}

/// Reports whether the compiled chronological simulator is present.
#[must_use]
pub const fn simulator_available() -> bool {
    true
}

/// Validate and run one global portfolio stream.
///
/// # Errors
///
/// Returns [`SimError`] when the version, configuration, candle ordering,
/// OHLCV values, or adjustment request cannot be represented exactly by this
/// supported simulator subset.
///
/// # Panics
///
/// Panics only if an internally created open trade points outside the already
/// validated immutable pair array. Public input cannot construct that state.
#[allow(clippy::too_many_lines)]
pub fn simulate(input: &SimulationInput) -> Result<SimulationResult, SimError> {
    simulate_internal(input, None).map(|(result, _)| result)
}

/// Run the simulator and stream one compact state projection after each
/// Freqtrade-visible pair candle. Freqtrade reserves the first row for shifted
/// signals and does expose the final row before its separate force-exit pass.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<SimulationResult, SimError>
where
    F: FnMut(&SimulationEvent),
{
    simulate_internal(input, Some(&mut observer)).map(|(result, _)| result)
}

/// Run the simulator and return aggregate phase timings beside the result.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
pub fn simulate_profiled(
    input: &SimulationInput,
) -> Result<(SimulationResult, SimulationProfile), SimError> {
    simulate_internal(input, None)
}

/// Run with an observer and return aggregate phase timings.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer_profiled<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<(SimulationResult, SimulationProfile), SimError>
where
    F: FnMut(&SimulationEvent),
{
    simulate_internal(input, Some(&mut observer))
}

#[allow(clippy::if_not_else, clippy::too_many_lines)]
fn simulate_internal(
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
    // Freqtrade's LocalTrade registry exposes still-open trades newest first
    // when the backtest force-closes its remaining positions. Preserve that
    // insertion-stack order here; otherwise the trades themselves are exact,
    // but their final exported sequence numbers are reversed.
    for trade in open_trades.into_iter().rev() {
        let last = input.pairs[trade.pair_index]
            .candles
            .last()
            .expect("validated non-empty candles");
        let (closed, _) = close_trade(
            trade,
            last.timestamp_ms,
            last.open,
            "force_exit".to_owned(),
            config,
            closed_trades.len(),
            next_order_id,
        );
        next_order_id += 1;
        closed_trades.push(closed);
        if let Some(program) = &config.protection_program {
            let closed_trade = closed_trades
                .last()
                .expect("a force-closed trade was appended immediately above");
            protection_state.after_trade_close(
                program,
                closed_trade,
                &closed_trades,
                config.starting_balance,
            );
        }
    }
    available_balance = wallet_free(config.starting_balance, &[], &closed_trades);
    // LocalTrade appends a trade to its closed-trade collection when that
    // trade closes. Freqtrade therefore exports closure order, including the
    // processing order of trades that close on the same timestamp. Sorting by
    // open time here looked deterministic but changed the public trade array
    // whenever overlapping positions closed in a different order.
    for (sequence, trade) in closed_trades.iter_mut().enumerate() {
        trade.sequence = sequence;
    }
    // Freqtrade exports `profit_total_abs` from Pandas' reduction of the
    // per-trade profit column. It is not derived from final wallet balance.
    // Pairwise summation mirrors NumPy's stable reduction and avoids the ulp
    // drift of a left-to-right iterator fold on long NFI result sets.
    let profit_total_abs = pairwise_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
    );
    let per_trade_volumes = closed_trades
        .iter()
        // Freqtrade calls Python `sum()` once per trade. CPython 3.14 uses a
        // compensated float accumulator, so Rust's ordinary Iterator::sum
        // differs by a few ulps on adjustment-heavy trades.
        .map(|trade| python_float_sum(trade.orders.iter().map(|order| order.cost)))
        .collect::<Vec<_>>();
    // Freqtrade then calls Python `sum()` over the per-trade subtotals. Keep
    // that second reduction boundary: flattening all orders is observably
    // different even when every order itself already matches.
    let total_volume = python_float_sum(per_trade_volumes);
    let result = SimulationResult {
        schema_version: SIMULATOR_SCHEMA_VERSION,
        starting_balance: config.starting_balance,
        final_balance: available_balance,
        profit_total_abs,
        total_volume,
        rejected_signals,
        maximum_concurrent_trades,
        locks: protection_state.locks().to_vec(),
        trades: closed_trades,
    };
    let profile = SimulationProfile {
        schema_version: "1.0.0",
        validation_ns,
        event_loop_ns,
        finalization_ns: duration_ns(finalization_started.elapsed()),
        timestamp_batches,
        pair_events,
    };
    Ok((result, profile))
}

fn simulation_event(
    timestamp_ms: i64,
    pair: &str,
    quote_free: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    rejected_signals: u64,
    locks: &[PairLockState],
) -> SimulationEvent {
    let mut base_balances: Vec<AssetBalance> = open_trades
        .iter()
        .map(|trade| AssetBalance {
            currency: trade
                .pair
                .split_once('/')
                .map_or_else(|| trade.pair.clone(), |(base, _)| base.to_owned()),
            free: if trade.side == TradeSide::Short {
                -trade.amount
            } else {
                trade.amount
            },
        })
        .collect();
    base_balances.sort_by(|left, right| left.currency.cmp(&right.currency));
    let realized_profit = closed_trades.iter().map(|trade| trade.profit_abs).sum();
    let trade_id_counter = open_trades
        .iter()
        .map(|trade| trade.id)
        .chain(closed_trades.iter().map(|trade| trade.id))
        .max()
        .unwrap_or_default();
    let order_id_counter = closed_trades
        .iter()
        .map(|trade| trade.orders.len())
        .sum::<usize>()
        + open_trades
            .iter()
            .map(|trade| trade.orders.len())
            .sum::<usize>();
    SimulationEvent {
        timestamp_ms,
        pair: pair.to_owned(),
        state: SimulationState {
            quote_free,
            base_balances,
            open_trade_count: open_trades.len(),
            realized_profit,
            closed_trade_count: closed_trades.len(),
            rejected_signals,
            trade_id_counter,
            order_id_counter,
            locks: locks.to_vec(),
        },
    }
}

#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is a valid no-op.
fn evaluate_nfi_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    if trade.side == TradeSide::Short {
        return evaluate_nfi_short_position_adjustment(manager, trade, request);
    }
    let mut initial_stake_multiplier = 1.0;
    let mut rebuy_mode = false;
    if let Some(route) = manager
        .managed_long_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy)
    {
        let words = trade
            .entry_tag
            .as_deref()
            .unwrap_or("")
            .split_whitespace()
            .collect::<Vec<_>>();
        if nfi_managed_route_supports_tags(manager, route, &words) {
            let first_exit_is_level_three = trade
                .orders
                .iter()
                .find(|order| !order.is_entry)
                .and_then(|order| order.tag.as_deref())
                == Some("derisk_level_3");
            if !first_exit_is_level_three {
                return evaluate_nfi_rebuy_adjustment(
                    &manager.rebuy_adjustment,
                    trade,
                    request.pair,
                    request.candle_index,
                    request.candle,
                    request.config,
                    request.available_balance,
                );
            }
            // X7 permanently transfers a rebuy trade to the shared grind-v3
            // state machine after its first level-3 de-risk fill. The source
            // first reverses the reduced rebuy entry stake back to the normal
            // slice size; schema 0.9 did not carry this constant and must fail
            // closed if such a transition is reached.
            initial_stake_multiplier = manager
                .position_adjustment
                .as_ref()?
                .constants
                .rebuy_stake_multiplier?;
            rebuy_mode = true;
        }
    }
    if let Some(route) = manager.long_grind.as_ref() {
        if nfi_long_grind_supports_trade(route, trade) {
            return evaluate_nfi_legacy_grind_adjustment(
                manager,
                route,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
    }
    if let Some(route) = manager.long_btc.as_ref() {
        if nfi_long_grind_supports_trade(route, trade) {
            return evaluate_nfi_long_btc_adjustment(
                manager,
                route,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
    }
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let uses_regular_adjustment = rebuy_mode
        || manager.managed_long_routes.iter().any(|route| {
            route.profile != NfiManagedLongProfile::Rebuy
                && words
                    .iter()
                    .any(|word| route.entry_tags.iter().any(|tag| tag == word))
        });
    if !uses_regular_adjustment {
        // This is the source's valid no-op branch for compounds such as a
        // rebuy/grind tag plus an opposite-side tag. Do not accidentally
        // promote those trades into the regular long adjustment machine.
        return Some(None);
    }
    evaluate_nfi_system_v3_adjustment(
        manager,
        manager.position_adjustment.as_ref()?,
        TradeSide::Long,
        trade,
        request,
        initial_stake_multiplier,
        rebuy_mode,
    )
}

#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is a valid no-op.
fn evaluate_nfi_short_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let rebuy_route = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")?;
    if nfi_managed_short_route_supports_tags(manager, rebuy_route, &words) {
        let first_exit_is_level_three = trade
            .orders
            .iter()
            .find(|order| !order.is_entry)
            .and_then(|order| order.tag.as_deref())
            == Some("derisk_level_3");
        if !first_exit_is_level_three {
            return evaluate_nfi_short_rebuy_adjustment(
                &manager.short_rebuy_adjustment,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
        let adjustment = manager.short_position_adjustment.as_ref()?;
        let initial_stake_multiplier = adjustment.constants.rebuy_stake_multiplier?;
        return evaluate_nfi_system_v3_adjustment(
            manager,
            adjustment,
            TradeSide::Short,
            trade,
            request,
            initial_stake_multiplier,
            true,
        );
    }
    let Some(adjustment) = manager.short_position_adjustment.as_ref() else {
        // Older descriptors only compile short-rebuy. If its all-tags
        // predicate did not match, upstream has no reachable short adjustment
        // branch for the remaining compiled cross-side compound.
        return Some(None);
    };
    let uses_regular_adjustment = words.iter().any(|word| {
        adjustment
            .entry_tags
            .iter()
            .any(|supported| supported == word)
    });
    if !uses_regular_adjustment {
        // A simultaneous long signal can append a long word to X7's shared
        // entry-tag column. Compounds containing only short-rebuy and long
        // words match neither source adjustment branch and are a valid no-op.
        return Some(None);
    }
    evaluate_nfi_system_v3_adjustment(
        manager,
        adjustment,
        TradeSide::Short,
        trade,
        request,
        1.0,
        false,
    )
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is callback no-op.
fn evaluate_nfi_long_btc_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    match evaluate_nfi_regular_adjustment(
        manager,
        route,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )? {
        RegularAdjustmentOutcome::Return(signal) => Some(signal),
        RegularAdjustmentOutcome::ContinueLegacy => evaluate_nfi_legacy_grind_adjustment(
            manager,
            route,
            trade,
            pair,
            candle_index,
            candle,
            config,
            available_balance,
        ),
    }
}

fn nfi_long_grind_supports_trade(route: &NfiLongGrindRoute, trade: &OpenTrade) -> bool {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    // X7 uses ``all(c in long_grind_mode_tags for c in enter_tags)`` for
    // this route. Requiring every word matters for mixed NFI tags: top-coins
    // intentionally uses a different, any-tag routing rule.
    !words.is_empty()
        && words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word))
}

fn entry_leverage(
    signal: &EntrySignal,
    config: &PortfolioConfig,
    pair: &PairSeries,
    candle: &Candle,
    proposed_stake: f64,
) -> Result<f64, SimError> {
    let proposed = signal
        .leverage
        .or_else(|| {
            config
                .nfi_leverage_program
                .as_ref()
                .map(|program| evaluate_nfi_leverage(program, signal.tag.as_deref()))
        })
        .or(config.leverage)
        .unwrap_or(1.0);
    let tier_limits = config
        .liquidation_model
        .as_ref()
        .and_then(|model| model.tiers_by_pair.get(&pair.pair));
    let maximum = if let Some(tiers) = tier_limits {
        Some(
            maximum_leverage_for_stake(tiers, proposed_stake).ok_or_else(|| {
                SimError::InvalidLeverage {
                    pair: pair.pair.clone(),
                    timestamp_ms: candle.timestamp_ms,
                }
            })?,
        )
    } else {
        config.maximum_leverage_by_pair.get(&pair.pair).copied()
    };
    let leverage = maximum
        .map_or(proposed, |value| proposed.min(value))
        .max(1.0);
    if leverage.is_finite() && leverage > 0.0 {
        Ok(leverage)
    } else {
        Err(SimError::InvalidLeverage {
            pair: pair.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })
    }
}

fn maximum_leverage_for_stake(tiers: &[LeverageTier], stake_amount: f64) -> Option<f64> {
    if stake_amount == 0.0 {
        return tiers.first().map(|tier| tier.maximum_leverage);
    }
    let mut prior_maximum = None;
    for tier in tiers {
        let minimum_stake = tier.min_notional / prior_maximum.unwrap_or(tier.maximum_leverage);
        let maximum_stake = tier
            .max_notional
            .map_or(f64::INFINITY, |value| value / tier.maximum_leverage);
        prior_maximum = Some(tier.maximum_leverage);
        if minimum_stake <= stake_amount && stake_amount <= maximum_stake {
            return Some(tier.maximum_leverage);
        }
        if stake_amount < minimum_stake && stake_amount <= maximum_stake {
            // Freqtrade intentionally selects this tier when the stake falls
            // below its nominal floor but still fits under the tier ceiling.
            return Some(tier.maximum_leverage);
        }
    }
    None
}

fn update_isolated_liquidation_price(
    trade: &mut OpenTrade,
    config: &PortfolioConfig,
    timestamp_ms: i64,
) -> Result<(), SimError> {
    if !config.is_futures || trade.liquidation_price_is_explicit {
        return Ok(());
    }
    let Some(model) = &config.liquidation_model else {
        // Generic simulator inputs may still omit a model and provide no
        // liquidation price. The X7 adapter has a stricter futures preflight.
        return Ok(());
    };
    let tiers =
        model
            .tiers_by_pair
            .get(&trade.pair)
            .ok_or_else(|| SimError::InvalidLiquidationPrice {
                pair: trade.pair.clone(),
                timestamp_ms,
            })?;
    // Freqtrade selects the last Binance tier whose minimum notional is not
    // greater than the current isolated stake amount.
    let tier = tiers
        .iter()
        .rev()
        .find(|tier| trade.stake_amount >= tier.min_notional)
        .ok_or_else(|| SimError::InvalidLiquidationPrice {
            pair: trade.pair.clone(),
            timestamp_ms,
        })?;
    let maintenance_amount =
        tier.maintenance_amount
            .ok_or_else(|| SimError::InvalidLiquidationPrice {
                pair: trade.pair.clone(),
                timestamp_ms,
            })?;
    let direction = if trade.side == TradeSide::Short {
        -1.0
    } else {
        1.0
    };
    let numerator =
        trade.stake_amount + maintenance_amount - direction * trade.amount * trade.open_rate;
    let denominator = trade.amount * tier.maintenance_margin_rate - direction * trade.amount;
    let raw_price = numerator / denominator;
    let buffer_amount = (trade.open_rate - raw_price).abs() * model.buffer;
    let buffered = if trade.side == TradeSide::Short {
        raw_price - buffer_amount
    } else {
        raw_price + buffer_amount
    }
    .max(0.0);
    if !buffered.is_finite() || buffered <= 0.0 {
        return Err(SimError::InvalidLiquidationPrice {
            pair: trade.pair.clone(),
            timestamp_ms,
        });
    }
    trade.liquidation_price = Some(buffered);
    Ok(())
}

fn evaluate_nfi_leverage(program: &NfiLeverageProgram, entry_tag: Option<&str>) -> f64 {
    let words = entry_tag.unwrap_or_default().split_whitespace();
    for rule in &program.ordered_tag_overrides {
        if words
            .clone()
            .all(|word| rule.entry_tags.iter().any(|tag| tag == word))
        {
            return rule.leverage;
        }
    }
    program.default
}

fn valid_vm_value(value: &Value) -> bool {
    match value {
        Value::Bool(_) | Value::Number(_) | Value::String(_) => true,
        Value::Array(values) => values
            .iter()
            .all(|item| matches!(item, Value::Bool(_) | Value::Number(_) | Value::String(_))),
        Value::Null | Value::Object(_) => false,
    }
}

fn number_value(value: f64) -> Option<Value> {
    serde_json::Number::from_f64(value).map(Value::Number)
}

#[allow(clippy::float_cmp)] // A VM index is valid only when its float token is exactly integral.
fn integer_value(value: &Value) -> Option<i64> {
    if let Some(integer) = value.as_i64() {
        return Some(integer);
    }
    // Arithmetic expressions such as unary minus are serialized through
    // `Number::from_f64`, so JSON `-1.0` no longer answers `as_i64()` even
    // though Python treats it as the exact list index -1. Accept only finite,
    // integral values inside i64's exactly checked conversion range.
    let numeric = value.as_f64()?;
    if !numeric.is_finite() || numeric.fract() != 0.0 {
        return None;
    }
    numeric.to_i64()
}

enum ScalarControl {
    Continue,
    Return(Value),
}

/// Per-program mutable scope over an immutable callback input map.
///
/// NFI evaluates several pure exit programs against the same dataframe window.
/// Keeping program-local writes in this overlay avoids deep-cloning the trade
/// and seven projected row objects for every program while preserving the
/// fresh Python local scope each method receives.
struct ScalarScope<'a> {
    base: &'a BTreeMap<String, Value>,
    local: BTreeMap<String, Value>,
}

impl<'a> ScalarScope<'a> {
    fn new(base: &'a BTreeMap<String, Value>) -> Self {
        Self {
            base,
            local: BTreeMap::new(),
        }
    }

    fn get(&self, name: &str) -> Option<&Value> {
        self.local.get(name).or_else(|| self.base.get(name))
    }

    fn insert(&mut self, name: String, value: Value) {
        self.local.insert(name, value);
    }
}

/// Evaluate a compact scalar-decision program without entering Python.
///
/// Inputs are the already-normalized method arguments. The function returns
/// `None` when either the program contract or a runtime value is invalid.
#[must_use]
pub fn evaluate_scalar_decision_program(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(program, variables, None, 0)
}

/// Evaluate one entry method in a hash-bound scalar program bundle.
///
/// Calls are resolved only inside `programs`; missing methods, arity drift,
/// recursive overflow, and malformed values all fail closed.
#[must_use]
pub fn evaluate_scalar_program_bundle(
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    entry: &str,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(programs.get(entry)?, variables, Some(programs), 0)
}

fn evaluate_scalar_program_bundle_from_base(
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    entry: &str,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program_from_base(programs.get(entry)?, variables, Some(programs), 0)
}

fn evaluate_scalar_program(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    evaluate_scalar_program_from_base(program, variables, programs, depth)
}

fn evaluate_scalar_program_from_base(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 64
        || !matches!(program.schema_version.as_str(), "1.0.0" | "1.1.0" | "1.2.0")
        || program.opcode != "scalar-decision-program-v1"
    {
        return None;
    }
    if program
        .parameters
        .iter()
        .any(|parameter| !variables.contains_key(parameter))
    {
        return None;
    }
    let mut scope = ScalarScope::new(variables);
    let ScalarControl::Return(value) =
        evaluate_scalar_statements(&program.statements, &mut scope, program, programs, depth)?
    else {
        return None;
    };
    Some(value)
}

fn evaluate_scalar_statements(
    statements: &[Value],
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    if depth > 256 {
        return None;
    }
    for statement in statements {
        let fields = statement.as_array()?;
        match fields.first()?.as_str()? {
            "set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(fields.get(1)?.as_str()?.to_owned(), value);
            }
            "ephemeral-set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(format!("$ephemeral.{}", fields.get(1)?.as_str()?), value);
            }
            "unpack" if fields.len() == 3 => {
                let names = fields.get(1)?.as_array()?;
                let values = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let values = values.as_array()?;
                if names.len() != values.len() {
                    return None;
                }
                for (name, value) in names.iter().zip(values) {
                    variables.insert(name.as_str()?.to_owned(), value.clone());
                }
            }
            "if" if fields.len() == 4 => {
                let condition = evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let branch = if scalar_truthy(&condition) {
                    fields.get(2)?
                } else {
                    fields.get(3)?
                };
                if let control @ ScalarControl::Return(_) = evaluate_scalar_statements(
                    branch.as_array()?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )? {
                    return Some(control);
                }
            }
            "if-chain" if fields.len() == 3 => {
                if let control @ ScalarControl::Return(_) =
                    evaluate_scalar_if_chain(fields, variables, program, programs, depth)?
                {
                    return Some(control);
                }
            }
            "return" if fields.len() == 2 => {
                return Some(ScalarControl::Return(evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?));
            }
            "pass" if fields.len() == 1 => {}
            _ => return None,
        }
    }
    Some(ScalarControl::Continue)
}

fn evaluate_scalar_if_chain(
    fields: &[Value],
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    let mut selected = None;
    for branch in fields.get(1)?.as_array()? {
        let branch = branch.as_array()?;
        if branch.len() != 2 {
            return None;
        }
        let condition = evaluate_scalar_expression(
            value_index(branch.first()?)?,
            variables,
            program,
            programs,
            depth + 1,
        )?;
        if scalar_truthy(&condition) {
            selected = Some(branch.get(1)?);
            break;
        }
    }
    let branch = selected.unwrap_or(fields.get(2)?);
    evaluate_scalar_statements(branch.as_array()?, variables, program, programs, depth + 1)
}

#[allow(clippy::too_many_lines)]
fn evaluate_scalar_expression(
    index: usize,
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 256 {
        return None;
    }
    let fields = program.expressions.get(index)?.as_array()?;
    let opcode = fields.first()?.as_str()?;
    match opcode {
        "literal" if fields.len() == 2 => fields.get(1).cloned(),
        "variable" if fields.len() == 2 => variables.get(fields.get(1)?.as_str()?).cloned(),
        "attribute" if fields.len() == 3 => {
            if let Some(value) =
                scalar_direct_variable(program, value_index(fields.get(1)?)?, variables)
            {
                return value.as_object()?.get(fields.get(2)?.as_str()?).cloned();
            }
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            value.as_object()?.get(fields.get(2)?.as_str()?).cloned()
        }
        "index" if fields.len() == 3 => {
            let base_index = value_index(fields.get(1)?)?;
            let key_index = value_index(fields.get(2)?)?;
            if let (Some(value), Some(index)) = (
                scalar_direct_variable(program, base_index, variables),
                scalar_direct_literal(program, key_index),
            ) {
                // Dataframe access is represented as
                // `last_candle["field"]`. Resolve that immutable lookup by
                // reference instead of cloning the complete projected row
                // before selecting one scalar.
                return scalar_index(value, index);
            }
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let index = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_index(&value, &index)
        }
        "not" if fields.len() == 2 => Some(Value::Bool(!scalar_truthy(&scalar_operand(
            fields, 1, variables, program, programs, depth,
        )?))),
        "negative" | "positive" if fields.len() == 2 => {
            let value = scalar_number(&scalar_operand(
                fields, 1, variables, program, programs, depth,
            )?)?;
            scalar_number_value(if opcode == "negative" { -value } else { value })
        }
        "add" | "subtract" | "multiply" | "divide" | "floor-divide" | "modulo" | "power"
            if fields.len() == 3 =>
        {
            let left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let right = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_binary(opcode, &left, &right)
        }
        "and" | "or" if fields.len() == 2 => {
            let operands = fields.get(1)?.as_array()?;
            let mut last = Value::Bool(opcode == "and");
            for operand in operands {
                last = evaluate_scalar_expression(
                    value_index(operand)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if (opcode == "and" && !scalar_truthy(&last))
                    || (opcode == "or" && scalar_truthy(&last))
                {
                    break;
                }
            }
            Some(last)
        }
        "compare" if fields.len() == 3 => {
            let mut left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            for comparison in fields.get(2)?.as_array()? {
                let comparison = comparison.as_array()?;
                if comparison.len() != 2 {
                    return None;
                }
                let right = evaluate_scalar_expression(
                    value_index(comparison.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if !scalar_compare(comparison.first()?.as_str()?, &left, &right)? {
                    return Some(Value::Bool(false));
                }
                left = right;
            }
            Some(Value::Bool(true))
        }
        "if-expression" if fields.len() == 4 => {
            let condition = scalar_operand(fields, 1, variables, program, programs, depth)?;
            scalar_operand(
                fields,
                if scalar_truthy(&condition) { 2 } else { 3 },
                variables,
                program,
                programs,
                depth,
            )
        }
        "tuple" | "list" | "set-literal" if fields.len() == 2 => Some(Value::Array(
            fields
                .get(1)?
                .as_array()?
                .iter()
                .map(|item| {
                    evaluate_scalar_expression(
                        value_index(item)?,
                        variables,
                        program,
                        programs,
                        depth + 1,
                    )
                })
                .collect::<Option<Vec<_>>>()?,
        )),
        "dict" if fields.len() == 2 => {
            let mut result = serde_json::Map::new();
            for item in fields.get(1)?.as_array()? {
                let item = item.as_array()?;
                if item.len() != 2 {
                    return None;
                }
                let key = evaluate_scalar_expression(
                    value_index(item.first()?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let value = evaluate_scalar_expression(
                    value_index(item.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                result.insert(scalar_string(&key), value);
            }
            Some(Value::Object(result))
        }
        "format" if fields.len() == 2 => {
            let mut result = String::new();
            for part in fields.get(1)?.as_array()? {
                let part = part.as_array()?;
                match part.first()?.as_str()? {
                    "text" if part.len() == 2 => result.push_str(part.get(1)?.as_str()?),
                    "value" if part.len() == 2 => {
                        let value = evaluate_scalar_expression(
                            value_index(part.get(1)?)?,
                            variables,
                            program,
                            programs,
                            depth + 1,
                        )?;
                        result.push_str(&scalar_string(&value));
                    }
                    _ => return None,
                }
            }
            Some(Value::String(result))
        }
        "call-program" if fields.len() == 3 => {
            let programs = programs?;
            let callee = programs.get(fields.get(1)?.as_str()?)?;
            let arguments = fields.get(2)?.as_array()?;
            if arguments.len() != callee.parameters.len() {
                return None;
            }
            let mut callee_variables = BTreeMap::new();
            for (parameter, argument) in callee.parameters.iter().zip(arguments) {
                let value = evaluate_scalar_expression(
                    value_index(argument)?,
                    variables,
                    program,
                    Some(programs),
                    depth + 1,
                )?;
                callee_variables.insert(parameter.clone(), value);
            }
            evaluate_scalar_program(callee, &callee_variables, Some(programs), depth + 1)
        }
        "is-instance" if fields.len() == 3 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let matches = match fields.get(2)?.as_str()? {
                "bool" => value.is_boolean(),
                "float" | "np.float64" => scalar_number(&value).is_some(),
                "int" => value
                    .as_i64()
                    .or_else(|| value.as_u64().and_then(|item| i64::try_from(item).ok()))
                    .is_some(),
                "str" => value.is_string(),
                _ => return None,
            };
            Some(Value::Bool(matches))
        }
        "length" if fields.len() == 2 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let length = match value {
                Value::Array(values) => values.len(),
                Value::Object(values) => values.len(),
                Value::String(value) => value.chars().count(),
                _ => return None,
            };
            Some(Value::Number(u64::try_from(length).ok()?.into()))
        }
        _ => None,
    }
}

fn scalar_direct_variable<'a>(
    program: &ScalarDecisionProgram,
    index: usize,
    variables: &'a ScalarScope<'_>,
) -> Option<&'a Value> {
    let expression = program.expressions.get(index)?.as_array()?;
    (expression.first()?.as_str()? == "variable")
        .then(|| expression.get(1)?.as_str())
        .flatten()
        .and_then(|name| variables.get(name))
}

fn scalar_direct_literal(program: &ScalarDecisionProgram, index: usize) -> Option<&Value> {
    let expression = program.expressions.get(index)?.as_array()?;
    (expression.first()?.as_str()? == "literal")
        .then(|| expression.get(1))
        .flatten()
}

fn scalar_operand(
    fields: &[Value],
    position: usize,
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    evaluate_scalar_expression(
        value_index(fields.get(position)?)?,
        variables,
        program,
        programs,
        depth + 1,
    )
}

fn value_index(value: &Value) -> Option<usize> {
    usize::try_from(value.as_u64()?).ok()
}

fn scalar_number(value: &Value) -> Option<f64> {
    if let Some(value) = value.as_f64() {
        return Some(value);
    }
    let marker = value.as_object()?.get("$float")?.as_str()?;
    match marker {
        "nan" => Some(f64::NAN),
        "inf" | "infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        _ => None,
    }
}

fn scalar_number_value(value: f64) -> Option<Value> {
    if value.is_finite() {
        return number_value(value);
    }
    let marker = if value.is_nan() {
        "nan"
    } else if value.is_sign_positive() {
        "inf"
    } else {
        "-inf"
    };
    Some(serde_json::json!({"$float": marker}))
}

fn scalar_binary(opcode: &str, left: &Value, right: &Value) -> Option<Value> {
    if opcode == "add" {
        if let (Some(left), Some(right)) = (left.as_str(), right.as_str()) {
            return Some(Value::String(format!("{left}{right}")));
        }
        if let (Some(left), Some(right)) = (left.as_array(), right.as_array()) {
            return Some(Value::Array(
                left.iter().chain(right).cloned().collect::<Vec<_>>(),
            ));
        }
    }
    let left = scalar_number(left)?;
    let right = scalar_number(right)?;
    let result = match opcode {
        "add" => left + right,
        "subtract" => left - right,
        "multiply" => left * right,
        "divide" => left / right,
        "floor-divide" => (left / right).floor(),
        "modulo" => left - (left / right).floor() * right,
        "power" => left.powf(right),
        _ => return None,
    };
    scalar_number_value(result)
}

fn scalar_compare(opcode: &str, left: &Value, right: &Value) -> Option<bool> {
    match opcode {
        "equal" | "is" => Some(scalar_equal(left, right)),
        "not-equal" | "is-not" => Some(!scalar_equal(left, right)),
        "less" | "less-equal" | "greater" | "greater-equal" => {
            if let (Some(left), Some(right)) = (scalar_number(left), scalar_number(right)) {
                return Some(match opcode {
                    "less" => left < right,
                    "less-equal" => left <= right,
                    "greater" => left > right,
                    "greater-equal" => left >= right,
                    _ => unreachable!(),
                });
            }
            let (left, right) = (left.as_str()?, right.as_str()?);
            Some(match opcode {
                "less" => left < right,
                "less-equal" => left <= right,
                "greater" => left > right,
                "greater-equal" => left >= right,
                _ => unreachable!(),
            })
        }
        "in" | "not-in" => {
            let included = match right {
                Value::Array(values) => values.iter().any(|value| scalar_equal(left, value)),
                Value::Object(values) => left.as_str().is_some_and(|key| values.contains_key(key)),
                Value::String(value) => left.as_str().is_some_and(|item| value.contains(item)),
                _ => return None,
            };
            Some(if opcode == "in" { included } else { !included })
        }
        _ => None,
    }
}

#[allow(clippy::float_cmp)]
fn scalar_equal(left: &Value, right: &Value) -> bool {
    match (scalar_number(left), scalar_number(right)) {
        (Some(left), Some(right)) => left == right,
        (Some(left), None) if right.is_boolean() => {
            left == f64::from(u8::from(right.as_bool().unwrap_or(false)))
        }
        (None, Some(right)) if left.is_boolean() => {
            f64::from(u8::from(left.as_bool().unwrap_or(false))) == right
        }
        _ => left == right,
    }
}

fn scalar_index(value: &Value, index: &Value) -> Option<Value> {
    match value {
        Value::Object(values) => values.get(index.as_str()?).cloned(),
        Value::Array(values) => {
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(values.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            values.get(usize::try_from(normalized).ok()?).cloned()
        }
        Value::String(value) => {
            let characters = value.chars().collect::<Vec<_>>();
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(characters.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            Some(Value::String(
                characters
                    .get(usize::try_from(normalized).ok()?)?
                    .to_string(),
            ))
        }
        _ => None,
    }
}

fn scalar_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => scalar_number(&Value::Object(value.clone()))
            .map_or(!value.is_empty(), |number| number != 0.0),
    }
}

fn scalar_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Object(_) if scalar_number(value).is_some() => {
            let number = scalar_number(value).unwrap_or(f64::NAN);
            if number.is_nan() {
                "nan".to_owned()
            } else if number.is_sign_positive() {
                "inf".to_owned()
            } else {
                "-inf".to_owned()
            }
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

enum CustomExitDecision {
    NoExit,
    Exit(String),
}

const NFI_LONG_EXIT_PROGRAMS: &[&str] = &[
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
    "long_exit_dec",
];
const NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING: &[&str] = &[
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
];
const NFI_SHORT_EXIT_PROGRAMS: &[&str] = &[
    "short_exit_signals",
    "short_exit_main",
    "short_exit_williams_r",
    "short_exit_dec",
];

/// Route NFI custom exits in the exact order used by the strategy.
///
/// A route that does not exit may still update the pair-level target cache.
/// Therefore this loop must continue through later matching routes instead of
/// selecting one route up front. That distinction is observable for mixed NFI
/// entry tags and is why ``route_order`` is part of the sealed input.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    for key in &manager.route_order {
        if let Some(route) = manager
            .managed_long_routes
            .iter()
            .find(|route| &route.key == key)
        {
            if !nfi_managed_route_supports_tags(manager, route, &words) {
                continue;
            }
            match evaluate_nfi_managed_long_exit(
                manager,
                route,
                nfi_profile_program_order(route.profile),
                trade,
                pair,
                candle_index,
                candle,
                config,
                profit_targets,
            )? {
                CustomExitDecision::Exit(reason) => {
                    return Some(CustomExitDecision::Exit(reason));
                }
                CustomExitDecision::NoExit => continue,
            }
        }

        let legacy = match key.as_str() {
            "long_grind" => manager.long_grind.as_ref(),
            "long_btc" => manager.long_btc.as_ref(),
            _ => None,
        };
        if let Some(route) = legacy.filter(|route| nfi_long_grind_supports_trade(route, trade)) {
            let snapshot = nfi_profit_snapshot(
                trade,
                candle.open,
                fee_open(config),
                fee_close(config),
                config.is_futures,
            )?;
            if snapshot.initial_stake_ratio > route.exit_profit_threshold {
                let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
                let reason = format!("exit_{}_g", route.mode_name);
                return Some(CustomExitDecision::Exit(nfi_exit_reason(
                    &reason, entry_tag,
                )));
            }
        }
    }
    // X7's custom_exit callback checks every long block before every short
    // block without filtering on trade.is_short. This is observable when its
    // shared enter_tag column contains labels from both sides.
    if let Some(decision) = evaluate_nfi_short_exit(
        manager,
        trade,
        pair,
        candle_index,
        candle,
        config,
        profit_targets,
    ) {
        if let CustomExitDecision::Exit(_) = decision {
            return Some(decision);
        }
    }
    // A compound of individually compiled words may intentionally match no
    // all-tags route. The source callback returns None in that case.
    Some(CustomExitDecision::NoExit)
}

/// Execute the bounded short-rebuy branch in source order.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_short_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let mut matched = false;
    for key in &manager.short_route_order {
        let route = manager
            .managed_short_routes
            .iter()
            .find(|route| &route.key == key)?;
        if !nfi_managed_short_route_supports_tags(manager, route, &words) {
            continue;
        }
        matched = true;
        match evaluate_nfi_managed_long_exit(
            manager,
            route,
            NFI_SHORT_EXIT_PROGRAMS,
            trade,
            pair,
            candle_index,
            candle,
            config,
            profit_targets,
        )? {
            CustomExitDecision::Exit(reason) => {
                return Some(CustomExitDecision::Exit(reason));
            }
            CustomExitDecision::NoExit => {}
        }
    }
    matched.then_some(CustomExitDecision::NoExit)
}

/// Execute one source-bound NFI X7 managed custom-exit route.
///
/// Every profile follows the source callback's order: pure signal programs,
/// optional inline quick/rapid logic, profile stoploss, existing target,
/// target mutation, then the profile's ignored-signal filter. Target writes
/// happen even when `confirm_trade_exit` later rejects the candidate, exactly
/// as in Freqtrade.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_managed_long_exit(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
    let enter_tags = entry_tag
        .split_whitespace()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let (mut sell, mut signal_name) = nfi_managed_long_signals(
        manager,
        route,
        program_order,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        &enter_tags,
    )?;

    // X7 places rapid's inline RSI/MFI checks before its custom stop, while
    // quick places the same-shaped checks after `long_exit_stoploss()`. The
    // distinction matters when both predicates are true because the returned
    // reason changes.
    if !sell && route.profile == NfiManagedLongProfile::Rapid {
        (sell, signal_name) =
            nfi_inline_profile_exit(route, pair, candle_index, snapshot, trade.side)?;
    }
    if !sell {
        (sell, signal_name) = nfi_managed_long_stoploss(
            manager,
            route,
            trade,
            pair,
            candle_index,
            snapshot,
            config.is_futures,
        )?;
    }
    if !sell && route.profile == NfiManagedLongProfile::Quick {
        (sell, signal_name) =
            nfi_inline_profile_exit(route, pair, candle_index, snapshot, trade.side)?;
    }

    let previous_target = profit_targets.get(&trade.pair).cloned();
    if let NfiExistingTargetOutcome::Exit(reason) = evaluate_existing_nfi_target(
        route,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        previous_target.as_ref(),
        profit_targets,
    )? {
        return Some(CustomExitDecision::Exit(nfi_exit_reason(
            &reason, entry_tag,
        )));
    }
    update_nfi_target_candidate(
        route,
        trade,
        candle,
        snapshot,
        sell,
        signal_name.as_deref(),
        previous_target.as_ref(),
        profit_targets,
    );

    if let Some(reason) = signal_name {
        if sell && !nfi_ignored_signal(route, &reason) {
            return Some(CustomExitDecision::Exit(nfi_exit_reason(
                &reason, entry_tag,
            )));
        }
    }
    if route.terminal_exit.as_ref().is_some_and(|terminal| {
        enter_tags == terminal.entry_tags
            && candle.timestamp_ms - trade.open_timestamp_ms >= terminal.minimum_age_ms
            && snapshot.initial_stake_ratio >= terminal.minimum_profit_ratio
    }) {
        let reason = &route
            .terminal_exit
            .as_ref()
            .expect("terminal exit was checked immediately above")
            .reason;
        return Some(CustomExitDecision::Exit(nfi_exit_reason(reason, entry_tag)));
    }
    Some(CustomExitDecision::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_signals(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    enter_tags: &[String],
) -> Option<(bool, Option<String>)> {
    if nfi_profile_requires_positive_profit(route.profile) && snapshot.initial_stake_ratio <= 0.0 {
        return Some((false, None));
    }
    let mut base_variables = BTreeMap::from([
        (
            "mode_name".to_owned(),
            Value::String(route.mode_name.clone()),
        ),
        (
            "current_profit".to_owned(),
            number_value(if route.profile == NfiManagedLongProfile::Rebuy {
                snapshot.current_stake_ratio
            } else {
                snapshot.initial_stake_ratio
            })?,
        ),
        ("max_profit".to_owned(), number_value(0.0)?),
        ("max_loss".to_owned(), number_value(0.0)?),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "buy_tag".to_owned(),
            Value::Array(enter_tags.iter().cloned().map(Value::String).collect()),
        ),
    ]);
    // All methods in this source-ordered callback see the same analyzed
    // dataframe window. Materialize the union once, then give each scalar
    // program a fresh local overlay so temporary assignments cannot leak into
    // the next method.
    insert_projected_feature_window(
        &mut base_variables,
        pair,
        candle_index,
        manager.feature_projection_union(program_order)?,
    )?;
    let mut result = (false, None);
    for program_name in program_order {
        let value = evaluate_scalar_program_bundle_from_base(
            &manager.programs,
            program_name,
            &base_variables,
        )?;
        let fields = value.as_array()?;
        if fields.len() != 2 {
            return None;
        }
        result.0 = fields.first()?.as_bool()?;
        result.1 = match fields.get(1)? {
            Value::Null => None,
            Value::String(reason) => Some(reason.clone()),
            _ => return None,
        };
        if result.0 {
            break;
        }
    }
    Some(result)
}

fn nfi_profile_program_order(profile: NfiManagedLongProfile) -> &'static [&'static str] {
    match profile {
        NfiManagedLongProfile::HighProfit => NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING,
        _ => NFI_LONG_EXIT_PROGRAMS,
    }
}

fn nfi_profile_requires_positive_profit(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Pump
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
    )
}

fn nfi_inline_profile_exit(
    route: &NfiManagedLongRoute,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    side: TradeSide,
) -> Option<(bool, Option<String>)> {
    let suffix_prefix = match route.profile {
        NfiManagedLongProfile::Quick
            if snapshot.initial_stake_ratio > 0.02 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "q"
        }
        NfiManagedLongProfile::Rapid
            if snapshot.initial_stake_ratio > 0.005 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "rpd"
        }
        _ => return Some((false, None)),
    };
    let rsi_14 = feature_number_at(pair, candle_index, "RSI_14")?;
    let mfi_14 = feature_number_at(pair, candle_index, "MFI_14")?;
    let willr_14 = feature_number_at(pair, candle_index, "WILLR_14")?;
    let rsi_3 = feature_number_at(pair, candle_index, "RSI_3")?;
    let rsi_3_15m = feature_number_at(pair, candle_index, "RSI_3_15m")?;
    let conditions = match side {
        TradeSide::Long => [
            rsi_14 > 78.0,
            mfi_14 > 84.0,
            willr_14 >= -0.1,
            rsi_14 >= 72.0 && rsi_3 > 90.0 && rsi_3_15m > 90.0,
            rsi_3_15m > 96.0,
            rsi_3 > 85.0 && rsi_3_15m > 85.0,
            rsi_3 > 90.0 && rsi_3_15m > 80.0,
            rsi_3 > 92.0 && rsi_3_15m > 75.0,
            rsi_3 > 94.0 && rsi_3_15m > 70.0,
            rsi_3 > 99.0,
        ],
        TradeSide::Short => {
            let fourth_rsi_limit = if route.profile == NfiManagedLongProfile::Quick {
                18.0
            } else {
                28.0
            };
            [
                rsi_14 < 22.0,
                mfi_14 < 16.0,
                willr_14 <= -99.9,
                rsi_14 <= fourth_rsi_limit && rsi_3 < 10.0 && rsi_3_15m < 10.0,
                rsi_3_15m < 4.0,
                rsi_3 < 15.0 && rsi_3_15m < 15.0,
                rsi_3 < 10.0 && rsi_3_15m < 20.0,
                rsi_3 < 8.0 && rsi_3_15m < 25.0,
                rsi_3 < 6.0 && rsi_3_15m < 30.0,
                rsi_3 < 1.0,
            ]
        }
    };
    let reason = conditions
        .iter()
        .position(|condition| *condition)
        .map(|index| format!("exit_{}_{}_{}", route.mode_name, suffix_prefix, index + 1));
    Some((reason.is_some(), reason))
}

#[allow(clippy::too_many_arguments)]
fn evaluate_existing_nfi_target(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<NfiExistingTargetOutcome> {
    let Some(previous) = previous else {
        return Some(NfiExistingTargetOutcome::NoExit);
    };
    let decision =
        nfi_managed_long_profit_target_exit(route, trade, pair, candle_index, snapshot, previous)?;
    if decision.remove {
        profit_targets.remove(&trade.pair);
    }
    if let Some(reason) = decision.exit_reason {
        return Some(NfiExistingTargetOutcome::Exit(format!("{reason}_m")));
    }
    let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
    let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
    if previous.sell_reason == stoploss_u_e
        && snapshot.ratio > previous.profit + nfi_u_e_raise_delta(route.profile)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.ratio,
        );
    } else if snapshot.initial_stake_ratio > previous.profit + 0.001
        && previous.sell_reason != stoploss_doom
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.initial_stake_ratio,
        );
    }
    Some(NfiExistingTargetOutcome::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn update_nfi_target_candidate(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    sell: bool,
    reason: Option<&str>,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) {
    if let (true, Some(reason)) = (sell, reason) {
        let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
        let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
        let blocked_u_e = format!("exit_profit_{}_stoploss_u_e", route.mode_name);
        let protected = reason == stoploss_doom || reason == stoploss_u_e;
        let blocked_previous = previous.is_some_and(|previous| {
            previous.sell_reason == stoploss_doom || previous.sell_reason == blocked_u_e
        });
        let target_profit = if protected {
            snapshot.ratio
        } else {
            snapshot.initial_stake_ratio
        };
        let should_mark = (protected
            && (!nfi_protected_target_has_reentry_guard(route.profile) || !blocked_previous))
            || (!protected
                && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio));
        if should_mark {
            set_profit_target(
                profit_targets,
                trade,
                candle,
                reason.to_owned(),
                target_profit,
            );
        }
    } else if snapshot.initial_stake_ratio >= nfi_max_target_floor(route.profile)
        && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            format!("exit_profit_{}_max", route.mode_name),
            snapshot.initial_stake_ratio,
        );
    }
}

fn nfi_ignored_signal(route: &NfiManagedLongRoute, reason: &str) -> bool {
    let maximum = format!("exit_profit_{}_max", route.mode_name);
    if reason == maximum {
        return true;
    }
    // X7 high-profit writes the stop target and still returns the stop in the
    // same callback. Every other managed-long mode suppresses that immediate
    // candidate and lets the target helper decide on a later candle.
    route.profile != NfiManagedLongProfile::HighProfit
        && [
            format!("exit_{}_stoploss_doom", route.mode_name),
            format!("exit_{}_stoploss_u_e", route.mode_name),
        ]
        .iter()
        .any(|ignored| ignored == reason)
}

fn nfi_u_e_raise_delta(profile: NfiManagedLongProfile) -> f64 {
    match profile {
        NfiManagedLongProfile::Normal
        | NfiManagedLongProfile::Pump
        | NfiManagedLongProfile::TopCoins
        | NfiManagedLongProfile::Scalp => 0.005,
        NfiManagedLongProfile::Quick
        | NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::HighProfit
        | NfiManagedLongProfile::Rapid => 0.001,
    }
}

fn nfi_max_target_floor(profile: NfiManagedLongProfile) -> f64 {
    if profile == NfiManagedLongProfile::HighProfit {
        0.03
    } else {
        0.005
    }
}

fn nfi_protected_target_has_reentry_guard(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
            | NfiManagedLongProfile::TopCoins
    )
}

enum NfiExistingTargetOutcome {
    NoExit,
    Exit(String),
}

#[derive(Debug, Default)]
struct NfiTargetDecision {
    exit_reason: Option<String>,
    remove: bool,
}

#[derive(Debug, Clone, Copy)]
struct NfiTargetIndicators {
    rsi: f64,
    previous_rsi: f64,
    cmf: f64,
    cmf_1h: f64,
    cmf_4h: f64,
    roc_4h: f64,
}

/// Return the first ordinary trailing branch selected by the source helper.
///
/// Keeping the mirrored long/short predicates together makes direction
/// changes reviewable without obscuring the surrounding target lifecycle.
fn nfi_profit_target_trailing_suffix(
    side: TradeSide,
    initial_stake_ratio: f64,
    previous_profit: f64,
    indicators: NfiTargetIndicators,
) -> Option<usize> {
    let dropped_by = |delta| initial_stake_ratio < previous_profit - delta;
    let branches = match side {
        TradeSide::Long => [
            dropped_by(0.03)
                && indicators.rsi < 50.0
                && indicators.rsi < indicators.previous_rsi
                && indicators.cmf < -0.0,
            dropped_by(0.03)
                && indicators.cmf < -0.0
                && indicators.cmf_1h < -0.0
                && indicators.cmf_4h < -0.0,
            dropped_by(0.05) && indicators.roc_4h > 40.0,
        ],
        TradeSide::Short => [
            dropped_by(0.03)
                && indicators.rsi > 50.0
                && indicators.rsi > indicators.previous_rsi
                && indicators.cmf > 0.0,
            dropped_by(0.03)
                && indicators.cmf > 0.0
                && indicators.cmf_1h > 0.0
                && indicators.cmf_4h > 0.0,
            dropped_by(0.05) && indicators.roc_4h < -40.0,
        ],
    };
    branches
        .iter()
        .position(|selected| *selected)
        .map(|index| index + 1)
}

/// Evaluate the shared profit-target helper for either source side.
///
/// The scalp bucket thresholds are common, while ordinary trailing indicators
/// are mirrored inside upstream's `trade.is_short` branch.
#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_profit_target_exit(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    previous: &ProfitTarget,
) -> Option<NfiTargetDecision> {
    let mode = &route.mode_name;
    let doom = format!("exit_{mode}_stoploss_doom");
    let ordinary_stop = format!("exit_{mode}_stoploss");
    let u_e = format!("exit_{mode}_stoploss_u_e");
    if previous.sell_reason == doom || previous.sell_reason == ordinary_stop {
        // This adapter is structurally gated to `system_name_use ==
        // system_v3_2_name`; X7 returns the cached stop immediately for all
        // system-v3 variants.
        return Some(NfiTargetDecision {
            exit_reason: Some(previous.sell_reason.clone()),
            remove: false,
        });
    }
    if previous.sell_reason == u_e {
        if snapshot.initial_stake_ratio > 0.0 || nfi_trade_is_derisked(trade)? {
            return Some(NfiTargetDecision {
                exit_reason: None,
                remove: true,
            });
        }
        if snapshot.ratio < previous.profit - 0.04 / trade.leverage {
            return Some(NfiTargetDecision {
                exit_reason: Some(previous.sell_reason.clone()),
                remove: false,
            });
        }
        return Some(NfiTargetDecision::default());
    }
    if previous.sell_reason != format!("exit_profit_{mode}_max") {
        return Some(NfiTargetDecision::default());
    }
    if snapshot.initial_stake_ratio < -0.08 {
        return Some(NfiTargetDecision {
            exit_reason: None,
            remove: true,
        });
    }

    let previous_index = candle_index.checked_sub(1)?;
    let indicators = NfiTargetIndicators {
        rsi: feature_number_at(pair, candle_index, "RSI_14")?,
        previous_rsi: feature_number_at(pair, previous_index, "RSI_14")?,
        cmf: feature_number_at(pair, candle_index, "CMF_20")?,
        cmf_1h: feature_number_at(pair, candle_index, "CMF_20_1h")?,
        cmf_4h: feature_number_at(pair, candle_index, "CMF_20_4h")?,
        roc_4h: feature_number_at(pair, candle_index, "ROC_9_4h")?,
    };
    let Some(bucket) = nfi_profit_bucket(snapshot.initial_stake_ratio) else {
        return Some(NfiTargetDecision::default());
    };
    let pure_scalp_tags = route.profile == NfiManagedLongProfile::Scalp
        && trade.entry_tag.as_deref().is_some_and(|entry_tag| {
            let words = entry_tag.split_whitespace().collect::<Vec<_>>();
            !words.is_empty()
                && words
                    .iter()
                    .all(|word| route.entry_tags.iter().any(|tag| tag == word))
        });
    if pure_scalp_tags {
        let trailing_delta = match bucket {
            0 => 0.008,
            1 | 2 => 0.01,
            3..=6 => 0.015,
            7..=9 => 0.02,
            10..=12 => 0.025,
            _ => return None,
        };
        return Some(NfiTargetDecision {
            exit_reason: (snapshot.initial_stake_ratio < previous.profit - trailing_delta)
                .then(|| format!("exit_profit_{mode}_t_{bucket}_1")),
            remove: false,
        });
    }
    let suffix = nfi_profit_target_trailing_suffix(
        trade.side,
        snapshot.initial_stake_ratio,
        previous.profit,
        indicators,
    );
    Some(NfiTargetDecision {
        exit_reason: suffix.map(|suffix| format!("exit_profit_{mode}_t_{bucket}_{suffix}")),
        remove: false,
    })
}

fn nfi_managed_long_stoploss(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    is_futures: bool,
) -> Option<(bool, Option<String>)> {
    let constants = &manager.constants;
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let entry_cost = first_entry.amount * first_entry.price;
    let system_version = trade.custom_data.get("system_version")?.as_str()?;
    if system_version != constants.system_name_use {
        return None;
    }

    if matches!(
        route.profile,
        NfiManagedLongProfile::Rebuy | NfiManagedLongProfile::Rapid | NfiManagedLongProfile::Scalp
    ) {
        if !constants.system_v3_2_stops_enable {
            return Some((false, None));
        }
        let threshold = if is_futures {
            route.stop_threshold_futures?
        } else {
            route.stop_threshold_spot?
        };
        let stopped = snapshot.stake < -(entry_cost * threshold / trade.leverage);
        return Some((
            stopped,
            stopped.then(|| format!("exit_{}_stoploss_doom", route.mode_name)),
        ));
    }

    if !constants.stops_enable {
        return Some((false, None));
    }
    if constants.system_v3_2_stops_enable {
        let threshold = if is_futures {
            constants.system_v3_2_stop_threshold_doom_futures
        } else {
            constants.system_v3_2_stop_threshold_doom_spot
        };
        if snapshot.stake < -(entry_cost * threshold / trade.leverage) {
            return Some((
                true,
                Some(format!("exit_{}_stoploss_doom", route.mode_name)),
            ));
        }
    }
    if !constants.u_e_stops_enable {
        return Some((false, None));
    }
    let previous_index = candle_index.checked_sub(1)?;
    let close = feature_number_at(pair, candle_index, "close")?;
    let ema_200 = feature_number_at(pair, candle_index, "EMA_200")?;
    let rsi = feature_number_at(pair, candle_index, "RSI_14")?;
    let cmf = feature_number_at(pair, candle_index, "CMF_20")?;
    let rsi_1h = feature_number_at(pair, candle_index, "RSI_14_1h")?;
    let previous_rsi = feature_number_at(pair, previous_index, "RSI_14")?;
    let threshold = if is_futures {
        constants.stop_threshold_futures
    } else {
        constants.stop_threshold_spot
    };
    let directional_guard = match trade.side {
        TradeSide::Long => {
            close < ema_200
                && cmf < -0.0
                && (ema_200 - close) / close < 0.010
                && rsi > previous_rsi
                && rsi > rsi_1h + 24.0
        }
        TradeSide::Short => {
            close > ema_200
                && cmf > 0.0
                && (close - ema_200) / ema_200 < 0.010
                && rsi < previous_rsi
                && rsi < rsi_1h - 24.0
        }
    };
    let should_stop = snapshot.stake < -(entry_cost * threshold) && directional_guard;
    Some((
        should_stop,
        should_stop.then(|| format!("exit_{}_stoploss_u_e", route.mode_name)),
    ))
}

fn nfi_trade_is_derisked(trade: &OpenTrade) -> Option<bool> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let tagged_exit = trade
        .orders
        .iter()
        .filter(|order| !order.is_entry)
        .any(|order| {
            order
                .tag
                .as_deref()
                .and_then(|tag| tag.split_whitespace().next())
                .is_some_and(|tag| {
                    matches!(
                        tag,
                        "d" | "d1" | "derisk_level_1" | "derisk_level_2" | "derisk_level_3"
                    )
                })
        });
    Some(tagged_exit || trade.amount < first_entry.amount * 0.95)
}

fn set_profit_target(
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
    trade: &OpenTrade,
    candle: &Candle,
    sell_reason: String,
    profit: f64,
) {
    profit_targets.insert(
        trade.pair.clone(),
        ProfitTarget {
            rate: candle.open,
            profit,
            sell_reason,
            time_profit_reached_ms: candle.timestamp_ms,
        },
    );
}

fn nfi_profit_bucket(profit: f64) -> Option<u8> {
    if profit < 0.001 {
        return None;
    }
    if profit >= 0.12 {
        return Some(12);
    }
    let mut bucket = 0_u8;
    for candidate in 1_u8..=11 {
        if profit >= f64::from(candidate) / 100.0 {
            bucket = candidate;
        }
    }
    Some(bucket)
}

fn nfi_exit_reason(reason: &str, entry_tag: &str) -> String {
    format!("{reason} ( {entry_tag})")
}

fn feature_number_at(pair: &PairSeries, index: usize, name: &str) -> Option<f64> {
    let candle = pair.candles.get(index)?;
    match name {
        "open" => Some(candle.open),
        "high" => Some(candle.high),
        "low" => Some(candle.low),
        "close" => Some(candle.close),
        "volume" => Some(candle.volume),
        _ => pair.feature_columns.get(name)?.number(index),
    }
}

fn feature_bool_at(pair: &PairSeries, index: usize, name: &str) -> Option<bool> {
    pair.feature_columns.get(name)?.boolean(index)
}

fn evaluate_custom_exit_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<CustomExitDecision> {
    let trade_value = scalar_trade_value(trade)?;
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        ("trade".to_owned(), trade_value),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        ("current_rate".to_owned(), number_value(candle.open)?),
        (
            "current_profit".to_owned(),
            number_value(current_profit_ratio(trade, candle.open, fee_close(config)))?,
        ),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index)?;
    let value = evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, &variables)?;
    if !scalar_truthy(&value) {
        return Some(CustomExitDecision::NoExit);
    }
    // Freqtrade preserves a truthy string as the custom reason. Any other
    // truthy Python value exits with ExitType.CUSTOM_EXIT's default reason.
    let reason = value.as_str().map_or_else(
        || "custom_exit".to_owned(),
        |value| value.chars().take(255).collect(),
    );
    Some(CustomExitDecision::Exit(reason))
}

fn scalar_trade_value(trade: &OpenTrade) -> Option<Value> {
    let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
    let exit_count = trade.orders.iter().filter(|order| !order.is_entry).count();
    Some(Value::Object(serde_json::Map::from_iter([
        ("id".to_owned(), Value::Number(trade.id.into())),
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        (
            "is_short".to_owned(),
            Value::Bool(trade.side == TradeSide::Short),
        ),
        ("amount".to_owned(), number_value(trade.amount)?),
        ("stake_amount".to_owned(), number_value(trade.stake_amount)?),
        ("open_rate".to_owned(), number_value(trade.open_rate)?),
        ("leverage".to_owned(), number_value(trade.leverage)?),
        (
            "open_date_utc".to_owned(),
            Value::Number(trade.open_timestamp_ms.into()),
        ),
        (
            "enter_tag".to_owned(),
            trade
                .entry_tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
        (
            "nr_of_successful_entries".to_owned(),
            Value::Number(u64::try_from(entry_count).ok()?.into()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            Value::Number(u64::try_from(exit_count).ok()?.into()),
        ),
        (
            "custom_data".to_owned(),
            Value::Object(trade.custom_data.clone().into_iter().collect()),
        ),
    ])))
}

fn evaluate_adjustment_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<Option<AdjustmentSignal>, ()> {
    let has_minimum = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    // Adjustment callbacks use Freqtrade's unleveraged minimum-stake
    // boundary, not the leverage-aware entry-order boundary.
    let minimum_stake = if has_minimum {
        number_value(adjustment_minimum_pair_stake(
            pair,
            candle.open,
            config.amount_reserve_percent,
        ))
        .ok_or(())?
    } else {
        Value::Null
    };
    let current_profit =
        number_value(current_profit_ratio(trade, candle.open, fee_close(config))).ok_or(())?;
    let mut variables = BTreeMap::from([
        ("trade".to_owned(), scalar_trade_value(trade).ok_or(())?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "current_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_profit".to_owned(), current_profit.clone()),
        ("min_stake".to_owned(), minimum_stake),
        (
            "max_stake".to_owned(),
            number_value(available_balance).ok_or(())?,
        ),
        (
            "current_entry_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        (
            "current_exit_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_entry_profit".to_owned(), current_profit.clone()),
        ("current_exit_profit".to_owned(), current_profit),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index).ok_or(())?;
    let value =
        evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, &variables).ok_or(())?;
    let (stake_amount, tag) = match value {
        Value::Null => return Ok(None),
        Value::Array(values) => {
            let stake = scalar_adjustment_number(values.first().ok_or(())?).ok_or(())?;
            let tag = match values.get(1) {
                None | Some(Value::Null | Value::Bool(false)) => String::new(),
                Some(Value::String(tag)) => tag.clone(),
                _ => return Err(()),
            };
            (stake, tag)
        }
        value => (scalar_adjustment_number(&value).ok_or(())?, String::new()),
    };
    if !stake_amount.is_finite() || stake_amount == 0.0 {
        return Ok(None);
    }
    if stake_amount > 0.0 && config.max_entry_position_adjustment >= 0 {
        let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
        if i64::try_from(entry_count).map_err(|_| ())? > config.max_entry_position_adjustment {
            return Ok(None);
        }
    }
    Ok(Some(AdjustmentSignal { stake_amount, tag }))
}

/// Map an execution candle to the last analyzed candle visible to callbacks.
///
/// Freqtrade shifts entry/exit signals onto the next candle before simulation,
/// but its data provider still ends at the last fully analyzed candle. At an
/// execution time of 15:30, callbacks therefore see the 15:25 row. Keeping
/// this translation at the callback boundary prevents order prices/timestamps
/// from being shifted along with indicator data.
fn callback_feature_index(execution_index: usize) -> Option<usize> {
    execution_index.checked_sub(1)
}

/// Materialize one strategy-visible dataframe row from the pair-level columns.
///
/// Freqtrade callbacks see the current analyzed row plus recent predecessors,
/// while the transport keeps those values columnar to avoid repeating 100+
/// field names for every NFI candle. Validation has already guaranteed equal
/// column lengths, but this helper still returns `None` so any internal/schema
/// mismatch fails closed instead of silently substituting a value.
fn feature_row(pair: &PairSeries, index: usize) -> Option<Value> {
    let candle = pair.candles.get(index)?;
    let mut row = serde_json::Map::from_iter([
        ("open".to_owned(), number_value(candle.open)?),
        ("high".to_owned(), number_value(candle.high)?),
        ("low".to_owned(), number_value(candle.low)?),
        ("close".to_owned(), number_value(candle.close)?),
        ("volume".to_owned(), number_value(candle.volume)?),
    ]);
    for (name, values) in &pair.feature_columns {
        row.insert(name.clone(), values.value(index)?);
    }
    Some(Value::Object(row))
}

impl NfiX7TradeManager {
    fn feature_projection(&self, program_name: &str) -> Option<&FeatureProjection> {
        self.feature_projections
            .get_or_init(|| {
                self.programs
                    .iter()
                    .map(|(name, program)| {
                        (name.clone(), scalar_program_feature_projection(program))
                    })
                    .collect()
            })
            .get(program_name)
    }

    fn feature_projection_union(&self, program_order: &[&str]) -> Option<&FeatureProjection> {
        let key = if program_order == NFI_LONG_EXIT_PROGRAMS {
            "long-all"
        } else if program_order == NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING {
            "long-without-descending"
        } else if program_order == NFI_SHORT_EXIT_PROGRAMS {
            "short-all"
        } else {
            return None;
        };
        self.feature_projection_unions
            .get_or_init(|| {
                [
                    ("long-all", NFI_LONG_EXIT_PROGRAMS),
                    (
                        "long-without-descending",
                        NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING,
                    ),
                    ("short-all", NFI_SHORT_EXIT_PROGRAMS),
                ]
                .into_iter()
                .filter_map(|(name, programs)| {
                    let mut union = FeatureProjection::new();
                    for program in programs {
                        let projection = self.feature_projection(program)?;
                        for (variable, columns) in projection {
                            union
                                .entry(variable.clone())
                                .or_default()
                                .extend(columns.iter().cloned());
                        }
                    }
                    Some((name.to_owned(), union))
                })
                .collect()
            })
            .get(key)
    }
}

/// Derive dataframe field access directly from the immutable scalar arena.
///
/// The compiler represents `last_candle["RSI_14"]` as an `index` expression
/// whose operands point at a `variable` and a literal string expression. We do
/// not accept a serialized projection list: deriving it here prevents an input
/// from omitting a field that executable bytecode can read.
fn scalar_program_feature_projection(program: &ScalarDecisionProgram) -> FeatureProjection {
    let mut projection = FeatureProjection::new();
    for expression in &program.expressions {
        let Some(fields) = expression.as_array() else {
            continue;
        };
        if fields.first().and_then(Value::as_str) != Some("index") {
            continue;
        }
        let Some(base_index) = fields.get(1).and_then(value_index) else {
            continue;
        };
        let Some(key_index) = fields.get(2).and_then(value_index) else {
            continue;
        };
        let Some(base) = program
            .expressions
            .get(base_index)
            .and_then(Value::as_array)
        else {
            continue;
        };
        let Some(key) = program.expressions.get(key_index).and_then(Value::as_array) else {
            continue;
        };
        if base.first().and_then(Value::as_str) != Some("variable")
            || key.first().and_then(Value::as_str) != Some("literal")
        {
            continue;
        }
        let Some(variable) = base.get(1).and_then(Value::as_str) else {
            continue;
        };
        if !is_feature_row_variable(variable) {
            continue;
        }
        if let Some(column) = key.get(1).and_then(Value::as_str) {
            projection
                .entry(variable.to_owned())
                .or_default()
                .insert(column.to_owned());
        }
    }
    projection
}

fn is_feature_row_variable(name: &str) -> bool {
    name == "last_candle"
        || name == "previous_candle"
        || (1..=CALLBACK_FEATURE_LOOKBACK_ROWS)
            .any(|offset| name == format!("previous_candle_{offset}"))
}

fn projected_feature_row(
    pair: &PairSeries,
    index: usize,
    columns: Option<&BTreeSet<String>>,
) -> Option<Value> {
    let candle = pair.candles.get(index)?;
    // OHLCV is always present in Freqtrade's analyzed row. Keeping these five
    // fields also preserves row truthiness if a future compiled branch checks
    // the row object itself without indexing a feature.
    let mut row = serde_json::Map::from_iter([
        ("open".to_owned(), number_value(candle.open)?),
        ("high".to_owned(), number_value(candle.high)?),
        ("low".to_owned(), number_value(candle.low)?),
        ("close".to_owned(), number_value(candle.close)?),
        ("volume".to_owned(), number_value(candle.volume)?),
    ]);
    for name in columns.into_iter().flatten() {
        if row.contains_key(name) {
            continue;
        }
        row.insert(name.clone(), pair.feature_columns.get(name)?.value(index)?);
    }
    Some(Value::Object(row))
}

fn insert_projected_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
    projection: &FeatureProjection,
) -> Option<()> {
    variables.insert(
        "last_candle".to_owned(),
        projected_feature_row(pair, candle_index, projection.get("last_candle"))?,
    );
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
        let name = format!("previous_candle_{offset}");
        let value = candle_index
            .checked_sub(offset)
            .and_then(|index| projected_feature_row(pair, index, projection.get(&name)))
            .unwrap_or(Value::Null);
        variables.insert(name, value);
    }
    let previous = candle_index
        .checked_sub(1)
        .and_then(|index| projected_feature_row(pair, index, projection.get("previous_candle")))
        .unwrap_or(Value::Null);
    variables.insert("previous_candle".to_owned(), previous);
    Some(())
}

/// Add the six analyzed dataframe rows used by NFI scalar decisions.
///
/// `candle_index` is already the callback-visible feature index, not the
/// execution-candle index. The names intentionally match the strategy method
/// parameters. A missing predecessor is represented as `None`; accessing a
/// field on it makes the scalar VM reject the callback. Real NFI signals only
/// become executable after `startup_candle_count`, so valid reference runs
/// always have the full lookback instead of receiving fabricated warm-up data.
fn insert_feature_window(
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<()> {
    variables.insert("last_candle".to_owned(), feature_row(pair, candle_index)?);
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
        let value = candle_index
            .checked_sub(offset)
            .and_then(|index| feature_row(pair, index))
            .unwrap_or(Value::Null);
        variables.insert(format!("previous_candle_{offset}"), value.clone());
        if offset == 1 {
            // Grind entry helpers use the shorter historical parameter name.
            variables.insert("previous_candle".to_owned(), value);
        }
    }
    Some(())
}

fn scalar_adjustment_number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        value => scalar_number(value),
    }
}

fn apply_funding(trade: &mut OpenTrade, candle: &Candle, funding_fee_interval_ms: Option<i64>) {
    let scheduled_refresh =
        funding_fee_interval_ms.is_some_and(|interval| candle.timestamp_ms % interval == 0);
    let mut changed = false;
    if scheduled_refresh {
        if let Some(seed) = trade.funding_rebase_seed.take() {
            reset_running_funding(trade, seed);
            changed = true;
        }
    }

    if let Some(signed) = funding_fee_at_candle(trade.side, trade.amount, candle) {
        // Inputs created before the refresh cadence became explicit still
        // rebase on the next sparse event. Exact X7 manifests always carry the
        // cadence and take the scheduled branch above.
        if funding_fee_interval_ms.is_none() {
            if let Some(seed) = trade.funding_rebase_seed.take() {
                reset_running_funding(trade, seed);
            }
        }
        add_running_funding(trade, signed);
        changed = true;
    }

    if changed {
        // `Trade.set_funding_fees()` separately performs Python `sum()` over
        // the already-filled orders, then adds the current running segment.
        let prior_funding = python_float_sum(trade.orders.iter().map(|order| order.funding_fee));
        trade.funding_fees_total = prior_funding + trade.funding_fees;
    }
}

fn funding_fee_at_candle(side: TradeSide, amount: f64, candle: &Candle) -> Option<f64> {
    let (Some(rate), Some(mark_price)) = (candle.funding_rate, candle.funding_mark_price) else {
        return None;
    };
    // Pandas evaluates Freqtrade's expression left-to-right as
    // `(open_fund * open_mark) * amount`. Multiplying amount first is
    // mathematically equivalent but changes exported float tokens.
    let fee = rate * mark_price * amount;
    // Freqtrade's persisted convention is positive when the trade receives
    // funding and negative when it pays. A positive market funding rate is
    // therefore income for shorts and a cost for longs.
    Some(match side {
        TradeSide::Long => -fee,
        TradeSide::Short => fee,
    })
}

fn add_running_funding(trade: &mut OpenTrade, signed: f64) {
    // `Exchange.calculate_funding_fees()` uses Python `sum()` over all
    // funding rows since the most recent filled order. CPython 3.14 uses a
    // Neumaier correction for float iterables, so a plain `+=` can differ by
    // an exported ulp on long-running adjustment trades.
    let next = trade.funding_sum_high + signed;
    if trade.funding_sum_high.abs() >= signed.abs() {
        trade.funding_sum_low += (trade.funding_sum_high - next) + signed;
    } else {
        trade.funding_sum_low += (signed - next) + trade.funding_sum_high;
    }
    trade.funding_sum_high = next;
    trade.funding_fees = compensated_sum_result(trade.funding_sum_high, trade.funding_sum_low);
}

fn reset_running_funding(trade: &mut OpenTrade, value: f64) {
    trade.funding_sum_high = value;
    trade.funding_sum_low = 0.0;
    trade.funding_fees = value;
}

/// Reproduce Freqtrade's forced funding refresh after an additional entry.
///
/// Backtesting first calculates funding before `adjust_trade_position`, moves
/// that running segment onto the newly filled order, and then calls
/// `_run_funding_fees(..., force=True)`. The exchange filter is inclusive at
/// both ends, so a fill exactly on a funding timestamp sees that row again
/// using the post-entry amount. A later exit attaches this refreshed running
/// segment to its order. Candles without funding data remain a no-op.
fn reapply_inclusive_funding_after_entry_fill(
    trade: &mut OpenTrade,
    candle: &Candle,
    funding_fee_interval_ms: Option<i64>,
) {
    apply_funding(trade, candle, funding_fee_interval_ms);
}

/// Preserve Freqtrade's two-stage funding state after a partial exit.
///
/// The fill first attaches the pre-exit running segment to the exit order.
/// Freqtrade then force-refreshes the inclusive range while the trade still
/// exposes its pre-exit amount. After order replay reduces the position,
/// `funding_fee_running` keeps that temporary value but the callback-visible
/// total contains filled-order funding only. The next scheduled funding tick
/// recalculates from the fill timestamp with the reduced amount. Retaining the
/// post-exit seed lets `apply_funding` replace the temporary segment exactly.
fn preserve_partial_exit_funding_refresh(
    trade: &mut OpenTrade,
    candle: &Candle,
    amount_before_fill: f64,
) {
    let Some(pre_exit_fee) = funding_fee_at_candle(trade.side, amount_before_fill, candle) else {
        return;
    };
    let post_exit_fee = funding_fee_at_candle(trade.side, trade.amount, candle)
        .expect("the same validated funding candle remains available");
    reset_running_funding(trade, pre_exit_fee);
    trade.funding_rebase_seed = Some(post_exit_fee);
    // `recalc_trade_from_orders()` runs after the forced refresh and resets
    // `funding_fees` to filled-order funding without clearing the separate
    // running value.
    recalculate_order_funding_total(trade);
}

fn compensated_sum_result(high: f64, low: f64) -> f64 {
    if low != 0.0 && low.is_finite() {
        high + low
    } else {
        high
    }
}

/// Move the current funding segment to a newly filled order.
///
/// Freqtrade resets `funding_fee_running` after every non-stoploss fill. The
/// compensated state must be reset at the same boundary or later segments
/// would retain an invisible correction from an earlier order.
fn take_running_funding(trade: &mut OpenTrade) -> f64 {
    trade.funding_sum_high = 0.0;
    trade.funding_sum_low = 0.0;
    trade.funding_rebase_seed = None;
    std::mem::take(&mut trade.funding_fees)
}

/// Mirror the ordinary left-to-right accumulation in
/// `LocalTrade.recalc_trade_from_orders()`.
///
/// This intentionally does not use `python_float_sum`: Freqtrade's order
/// replay is an explicit `+=` loop, which has different rounding behavior.
fn recalculate_order_funding_total(trade: &mut OpenTrade) {
    trade.funding_fees_total = trade
        .orders
        .iter()
        .fold(0.0, |total, order| total + order.funding_fee);
}

fn nfi_profit_snapshot(
    trade: &OpenTrade,
    exit_rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    is_futures: bool,
) -> Option<NfiProfitSnapshot> {
    if !exit_rate.is_finite()
        || !open_fee_rate.is_finite()
        || !close_fee_rate.is_finite()
        || trade.orders.is_empty()
    {
        return None;
    }
    let mut total_amount = 0.0;
    let mut total_stake = 0.0;
    let mut total_profit = 0.0;
    let (open_multiplier, close_multiplier) = if trade.side == TradeSide::Short {
        (1.0 - open_fee_rate, 1.0 + close_fee_rate)
    } else {
        (1.0 + open_fee_rate, 1.0 - close_fee_rate)
    };
    let mut first_entry_cost = None;
    for order in &trade.orders {
        let stake = order.amount * order.price;
        if order.is_entry {
            first_entry_cost.get_or_insert(stake);
            let entry_stake = stake * open_multiplier;
            total_amount += order.amount;
            total_stake += entry_stake;
            if trade.side == TradeSide::Short {
                total_profit += entry_stake;
            } else {
                total_profit -= entry_stake;
            }
        } else {
            let exit_stake = stake * close_multiplier;
            total_amount -= order.amount;
            if trade.side == TradeSide::Short {
                total_profit -= exit_stake;
            } else {
                total_profit += exit_stake;
            }
        }
    }
    let current_stake = total_amount * exit_rate * close_multiplier;
    if trade.side == TradeSide::Short {
        total_profit -= current_stake;
    } else {
        total_profit += current_stake;
    }
    if is_futures {
        // NFI reads `trade.funding_fees`, which Freqtrade keeps as the
        // cumulative fee across filled orders plus the current running
        // interval. A partial exit realizes part of the position but does not
        // reduce this callback-visible cumulative value.
        total_profit += trade.funding_fees_total;
    }
    let first_entry_cost = first_entry_cost?;
    if total_stake == 0.0 || current_stake == 0.0 || first_entry_cost == 0.0 {
        return None;
    }
    Some(NfiProfitSnapshot {
        stake: total_profit,
        ratio: total_profit / total_stake,
        current_stake_ratio: total_profit / current_stake,
        initial_stake_ratio: total_profit / first_entry_cost,
    })
}

#[cfg(test)]
#[allow(clippy::float_cmp)] // These tests assert exact Freqtrade float tokens.
mod tests {
    use super::io::FILE_BACKED_READ_BUFFER_BYTES;
    use super::protections::{
        DrawdownMode, ProtectionHandler, ProtectionProgram, ProtectionTiming,
    };
    use super::validation::valid_nfi_managed_long_route;
    use super::*;
    use std::io::Write as _;
    use std::sync::OnceLock;

    fn candle(timestamp_ms: i64, open: f64, low: f64) -> Candle {
        Candle {
            timestamp_ms,
            open,
            high: open + 10.0,
            low,
            close: open,
            volume: 1.0,
            previous_close: None,
            enter_long: None,
            enter_short: None,
            exit_long: None,
            exit_short: None,
            funding_rate: None,
            funding_mark_price: None,
            adjustment: None,
        }
    }

    #[test]
    fn file_backed_rows_preserve_forward_and_backward_window_access() {
        let mut file = tempfile::tempfile().expect("create private row spool");
        let row_stride = FILE_BACKED_ROW_HEADER_BYTES + FILE_BACKED_FEATURE_BYTES;
        let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
        let row_count = rows_per_window * 2 + 3;
        for index in 0..row_count {
            let feature_value = index.to_f64().expect("test row index fits f64") + 0.5;
            let mut row = vec![0_u8; row_stride];
            row[..8].copy_from_slice(
                &i64::try_from(index)
                    .expect("test row index fits i64")
                    .to_le_bytes(),
            );
            row[FILE_BACKED_ROW_HEADER_BYTES..].copy_from_slice(&feature_value.to_le_bytes());
            file.write_all(&row).expect("write normalized test row");
        }
        let rows =
            FileBackedRows::new(file, row_count, 1, Vec::new()).expect("open verified test spool");

        for index in [
            0,
            rows_per_window - 1,
            rows_per_window + 10,
            2,
            row_count - 1,
        ] {
            assert_eq!(rows.timestamp_ms(index), i64::try_from(index).ok());
            assert_eq!(
                rows.feature_number(index, 0),
                index.to_f64().map(|value| value + 0.5)
            );
        }
    }

    #[test]
    fn file_backed_rows_keep_callback_lookback_in_the_current_window() {
        let mut file = tempfile::tempfile().expect("create private row spool");
        let row_stride = FILE_BACKED_ROW_HEADER_BYTES + FILE_BACKED_FEATURE_BYTES;
        let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
        assert!(rows_per_window > CALLBACK_FEATURE_LOOKBACK_ROWS);
        let row_count = rows_per_window + CALLBACK_FEATURE_LOOKBACK_ROWS + 1;
        for index in 0..row_count {
            let mut row = vec![0_u8; row_stride];
            row[..8].copy_from_slice(
                &i64::try_from(index)
                    .expect("test row index fits i64")
                    .to_le_bytes(),
            );
            file.write_all(&row).expect("write normalized test row");
        }
        let rows =
            FileBackedRows::new(file, row_count, 1, Vec::new()).expect("open verified test spool");

        assert_eq!(
            rows.timestamp_ms(rows_per_window),
            i64::try_from(rows_per_window).ok()
        );
        let retained_start = rows.buffered_window_start();
        assert_eq!(
            retained_start,
            rows_per_window - CALLBACK_FEATURE_LOOKBACK_ROWS
        );
        for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
            assert_eq!(
                rows.timestamp_ms(rows_per_window - offset),
                i64::try_from(rows_per_window - offset).ok()
            );
            assert_eq!(rows.buffered_window_start(), retained_start);
        }
    }

    #[test]
    fn file_backed_entry_index_reuses_validated_signal_positions() {
        let mut file = tempfile::tempfile().expect("create private row spool");
        let row_stride = FILE_BACKED_ROW_HEADER_BYTES;
        let row_count = 12;
        for index in 0..row_count {
            let mut row = vec![0_u8; row_stride];
            row[..8].copy_from_slice(
                &i64::try_from(index)
                    .expect("test row index fits i64")
                    .to_le_bytes(),
            );
            if matches!(index, 2 | 7) {
                row[72] |= 1 << 3;
            }
            file.write_all(&row).expect("write normalized test row");
        }
        let rows =
            FileBackedRows::new(file, row_count, 0, Vec::new()).expect("open verified test spool");

        assert_eq!(rows.next_entry_index(0), Some(2));
        rows.install_entry_indices(vec![2, 7]);
        assert_eq!(rows.next_entry_index(3), Some(7));
        assert_eq!(rows.next_entry_index(8), None);
        assert_eq!(rows.installed_entry_indices(), Some(&[2, 7][..]));
    }

    fn config(max_open_trades: usize) -> PortfolioConfig {
        PortfolioConfig {
            starting_balance: 1_000.0,
            max_open_trades,
            stake_amount: 100.0,
            fee_rate: 0.001,
            fee_open_rate: None,
            fee_close_rate: None,
            leverage: None,
            nfi_leverage_program: None,
            maximum_leverage_by_pair: BTreeMap::new(),
            liquidation_model: None,
            protection_program: None,
            stoploss_ratio: -0.01,
            amount_step: 0.00001,
            price_step: 0.01,
            custom_exit_after_ms: None,
            adjustment_rule: None,
            callback_program: None,
            stake_program: None,
            amount_reserve_percent: 0.05,
            unlimited_stake: false,
            tradable_balance_ratio: 1.0,
            entry_confirmation_program: None,
            exit_confirmation_program: None,
            custom_exit_program: None,
            adjust_trade_position_program: None,
            nfi_x7_trade_manager: None,
            max_entry_position_adjustment: -1,
            is_futures: false,
            funding_fee_interval_ms: None,
        }
    }

    fn remaining_after_partial_exit(orders: &[FilledOrder], exit_index: usize) -> f64 {
        orders[..exit_index]
            .iter()
            .filter(|order| order.is_entry)
            .map(|order| order.amount)
            .sum::<f64>()
            - orders[exit_index].amount
    }

    fn isolated_model(pair: &str, tiers: Vec<LeverageTier>) -> IsolatedLiquidationModel {
        IsolatedLiquidationModel {
            exchange: "binance".to_owned(),
            margin_mode: "isolated".to_owned(),
            buffer: 0.05,
            tiers_by_pair: BTreeMap::from([(pair.to_owned(), tiers)]),
        }
    }

    fn leverage_tier(
        min_notional: f64,
        max_notional: Option<f64>,
        maximum_leverage: f64,
        maintenance_margin_rate: f64,
        maintenance_amount: f64,
    ) -> LeverageTier {
        LeverageTier {
            min_notional,
            max_notional,
            maximum_leverage,
            maintenance_margin_rate,
            maintenance_amount: Some(maintenance_amount),
        }
    }

    fn buffered_liquidation_price(
        side: TradeSide,
        stake_amount: f64,
        amount: f64,
        open_rate: f64,
        maintenance_margin_rate: f64,
        maintenance_amount: f64,
        buffer: f64,
    ) -> f64 {
        let direction = if side == TradeSide::Short { -1.0 } else { 1.0 };
        let raw = (stake_amount + maintenance_amount - direction * amount * open_rate)
            / (amount * maintenance_margin_rate - direction * amount);
        let offset = (open_rate - raw).abs() * buffer;
        if side == TradeSide::Short {
            raw - offset
        } else {
            raw + offset
        }
    }

    fn protection_timing(
        lookback_ms: i64,
        duration_ms: i64,
        lookback_text: &str,
        lock_text: &str,
    ) -> ProtectionTiming {
        ProtectionTiming {
            lookback_ms,
            lookback_text: lookback_text.to_owned(),
            duration_ms: Some(duration_ms),
            unlock_at_minute_utc: None,
            lock_text: lock_text.to_owned(),
        }
    }

    fn protection_trade(
        id: u64,
        pair: &str,
        close_timestamp_ms: i64,
        profit_ratio: f64,
        exit_reason: &str,
        side: TradeSide,
    ) -> ClosedTrade {
        ClosedTrade {
            sequence: usize::try_from(id - 1).expect("small fixture id"),
            id,
            pair: pair.to_owned(),
            is_short: side == TradeSide::Short,
            leverage: 1.0,
            open_timestamp_ms: close_timestamp_ms - 60_000,
            close_timestamp_ms,
            open_rate: 100.0,
            close_rate: 100.0 * (1.0 + profit_ratio),
            amount: 1.0,
            stake_amount: 100.0,
            max_stake_amount: 100.0,
            entry_tag: None,
            exit_reason: exit_reason.to_owned(),
            fee_open: 0.0,
            fee_close: 0.0,
            funding_fees: 0.0,
            liquidation_price: None,
            profit_abs: profit_ratio * 100.0,
            profit_ratio,
            initial_stop_loss: 1.0,
            stop_loss: 1.0,
            minimum_rate: 100.0,
            maximum_rate: 100.0,
            orders: Vec::new(),
        }
    }

    fn nfi_false_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [
                ["literal", false],
                ["literal", null],
                ["tuple", [0, 1]]
            ],
            "statements": [["return", 2]]
        }))
        .expect("valid false decision")
    }

    fn nfi_boolean_false_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [["literal", false]],
            "statements": [["return", 0]]
        }))
        .expect("valid false predicate")
    }

    fn nfi_boolean_true_program() -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": [],
            "expressions": [["literal", true]],
            "statements": [["return", 0]]
        }))
        .expect("valid true predicate")
    }

    fn nfi_profit_program(threshold: f64, reason: &str) -> ScalarDecisionProgram {
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["current_profit"],
            "expressions": [
                ["variable", "current_profit"],
                ["literal", threshold],
                ["compare", 0, [["greater", 1]]],
                ["literal", true],
                ["literal", reason],
                ["tuple", [3, 4]],
                ["literal", false],
                ["literal", null],
                ["tuple", [6, 7]]
            ],
            "statements": [
                ["if", 2, [["return", 5]], []],
                ["return", 8]
            ]
        }))
        .expect("valid profit decision")
    }

    fn nfi_managed_route(
        key: &str,
        profile: NfiManagedLongProfile,
        mode_name: &str,
        entry_tags: &[&str],
    ) -> NfiManagedLongRoute {
        let has_dedicated_stop = matches!(
            profile,
            NfiManagedLongProfile::Rebuy
                | NfiManagedLongProfile::Rapid
                | NfiManagedLongProfile::Scalp
        );
        NfiManagedLongRoute {
            key: key.to_owned(),
            profile,
            mode_name: mode_name.to_owned(),
            entry_tags: entry_tags.iter().map(ToString::to_string).collect(),
            stop_threshold_futures: has_dedicated_stop.then_some(0.35),
            stop_threshold_spot: has_dedicated_stop.then_some(0.12),
            terminal_exit: None,
        }
    }

    fn nfi_legacy_grind_constants() -> NfiLegacyGrindConstants {
        let tags = [
            ("gd1", "dd1"),
            ("gd2", "dd2"),
            ("gd3", "dd3"),
            ("gd4", "dd4"),
            ("gd5", "dd5"),
            ("gd6", "dd6"),
            ("dl1", "ddl1"),
            ("dl2", "ddl2"),
        ];
        NfiLegacyGrindConstants {
            max_stake_multiplier: 1.0,
            stake_multipliers_futures: vec![0.2, 0.3, 0.4, 0.5],
            stake_multipliers_spot: vec![0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
            derisk_1_reentry_futures: -0.08,
            derisk_1_reentry_spot: -0.08,
            clusters: tags
                .into_iter()
                .map(|(entry_tag, stop_tag)| NfiLegacyGrindCluster {
                    entry_tag: entry_tag.to_owned(),
                    stop_tag: stop_tag.to_owned(),
                    stakes_futures: vec![0.2, 0.24, 0.28],
                    stakes_spot: vec![0.2, 0.24, 0.28],
                    thresholds_futures: vec![-0.12, -0.16, -0.20],
                    thresholds_spot: vec![-0.12, -0.16, -0.20],
                    stop_threshold_futures: -0.06,
                    stop_threshold_spot: -0.06,
                    profit_threshold_futures: 0.018,
                    profit_threshold_spot: 0.018,
                })
                .collect(),
        }
    }

    fn nfi_regular_adjustment_constants() -> NfiRegularAdjustmentConstants {
        NfiRegularAdjustmentConstants {
            use_grind_stops: true,
            derisk_enable: true,
            rebuy_stakes_futures: vec![0.2, 0.25],
            rebuy_stakes_spot: vec![0.2, 0.25],
            rebuy_thresholds_futures: vec![-0.08, -0.12],
            rebuy_thresholds_spot: vec![-0.08, -0.12],
            derisk_threshold_futures: -0.6,
            derisk_threshold_spot: -0.6,
            derisk_level_1_threshold_futures: -0.4,
            derisk_level_1_threshold_spot: -0.4,
            grinds: (1..=6)
                .map(|level| NfiRegularGrind {
                    entry_tag: format!("g{level}"),
                    stop_tag: format!("sg{level}"),
                    stakes_futures: vec![0.2, 0.25],
                    stakes_spot: vec![0.2, 0.25],
                    thresholds_futures: vec![-0.08, -0.12],
                    thresholds_spot: vec![-0.08, -0.12],
                    stop_threshold_futures: -0.2,
                    stop_threshold_spot: -0.2,
                    profit_threshold_futures: 0.018,
                    profit_threshold_spot: 0.018,
                })
                .collect(),
            policy: NfiRegularAdjustmentPolicy {
                entry_retry_ms: 10 * 60 * 1_000,
                grind_force_order_age_ms: 2 * 60 * 60 * 1_000,
                grind_order_age_ms: 6 * 60 * 60 * 1_000,
                rebuy_order_age_ms: 12 * 60 * 60 * 1_000,
                grind_entry_profit_gate: -0.02,
                additional_grind_profit_gate: -0.03,
                forced_age_profit_gate: -0.06,
                minimum_entry_multiplier: 1.5,
                minimum_remaining_multiplier: 1.55,
            },
        }
    }

    fn enable_test_long_btc(
        manager: &mut NfiX7TradeManager,
        constants: NfiRegularAdjustmentConstants,
        regular_program: ScalarDecisionProgram,
    ) {
        manager
            .programs
            .insert("long_grind_entry".to_owned(), regular_program);
        manager.long_btc = Some(NfiLongGrindRoute {
            mode_name: "long_btc".to_owned(),
            entry_tags: vec!["121".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "regular-backtest-v2".to_owned(),
            grind_mode: false,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: None,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
            regular_decision_program: Some("long_grind_entry".to_owned()),
            regular_constants: Some(constants),
        });
        manager.route_order.insert(6, "long_btc".to_owned());
    }

    #[allow(clippy::too_many_lines)] // Full valid manager fixture is intentionally explicit.
    fn nfi_top_coins_manager(first: ScalarDecisionProgram) -> NfiX7TradeManager {
        let false_program = nfi_false_program();
        let managed_long_routes = vec![
            nfi_managed_route(
                "long_normal",
                NfiManagedLongProfile::Normal,
                "long_normal",
                &["1"],
            ),
            nfi_managed_route(
                "long_pump",
                NfiManagedLongProfile::Pump,
                "long_pump",
                &["21"],
            ),
            nfi_managed_route(
                "long_quick",
                NfiManagedLongProfile::Quick,
                "long_quick",
                &["41"],
            ),
            nfi_managed_route(
                "long_rebuy",
                NfiManagedLongProfile::Rebuy,
                "long_rebuy",
                &["61", "62", "63", "64", "65"],
            ),
            nfi_managed_route(
                "long_high_profit",
                NfiManagedLongProfile::HighProfit,
                "long_hp",
                &["81"],
            ),
            nfi_managed_route(
                "long_rapid",
                NfiManagedLongProfile::Rapid,
                "long_rapid",
                &["101"],
            ),
            nfi_managed_route(
                "long_top_coins",
                NfiManagedLongProfile::TopCoins,
                "long_tc",
                &["141", "142", "143", "144", "145"],
            ),
            nfi_managed_route(
                "long_scalp",
                NfiManagedLongProfile::Scalp,
                "long_scalp",
                &["161"],
            ),
        ];
        let adjustment_tags = managed_long_routes
            .iter()
            .flat_map(|route| route.entry_tags.clone())
            .collect();
        let mut short_rebuy_route = nfi_managed_route(
            "short_rebuy",
            NfiManagedLongProfile::Rebuy,
            "short_rebuy",
            &["561", "562", "563"],
        );
        short_rebuy_route.stop_threshold_futures = Some(1.4);
        short_rebuy_route.stop_threshold_spot = Some(0.48);
        let rebuy_constants = NfiX7RebuyConstants {
            derisk_enable: true,
            stakes_futures: vec![1.0, 1.0, 1.0, 1.0],
            stakes_spot: vec![1.0, 1.0, 1.0, 1.0],
            thresholds_futures: vec![-0.08, -0.12, -0.16, -0.20],
            thresholds_spot: vec![-0.08, -0.12, -0.16, -0.20],
            derisk_futures: -1.40,
            derisk_spot: -0.48,
        };
        NfiX7TradeManager {
            schema_version: "0.13.0".to_owned(),
            source_sha256: "a".repeat(64),
            route_order: [
                "long_normal",
                "long_pump",
                "long_quick",
                "long_rebuy",
                "long_high_profit",
                "long_rapid",
                "long_top_coins",
                "long_scalp",
            ]
            .into_iter()
            .map(ToOwned::to_owned)
            .collect(),
            managed_long_routes,
            short_route_order: vec!["short_rebuy".to_owned()],
            managed_short_routes: vec![short_rebuy_route],
            long_grind: None,
            long_btc: None,
            rebuy_adjustment: NfiX7RebuyAdjustment {
                enabled: true,
                entry_tags: ["61", "62", "63", "64", "65"]
                    .into_iter()
                    .map(ToOwned::to_owned)
                    .collect(),
                system_version: "system_v3_2".to_owned(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: rebuy_constants.clone(),
            },
            short_rebuy_adjustment: NfiX7ShortRebuyAdjustment {
                enabled: true,
                entry_tags: ["561", "562", "563"]
                    .into_iter()
                    .map(ToOwned::to_owned)
                    .collect(),
                system_version: "system_v3_2".to_owned(),
                execution_scope: "pre-derisk-only-v1".to_owned(),
                post_derisk_action: "fail-simulation".to_owned(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: rebuy_constants,
            },
            position_adjustment: Some(NfiX7PositionAdjustment {
                enabled: false,
                entry_tags: adjustment_tags,
                system_version: "system_v3_2".to_owned(),
                decision_program: "long_grind_entry_v3".to_owned(),
                program_order: [
                    "derisk_level_1",
                    "derisk_level_2",
                    "derisk_level_3",
                    "grind_1_entry",
                    "grind_1_exit",
                    "grind_1_derisk",
                    "grind_2_entry",
                    "grind_2_exit",
                    "grind_2_derisk",
                    "grind_3_entry",
                    "grind_3_exit",
                    "grind_3_derisk",
                    "grind_4_entry",
                    "grind_4_exit",
                    "grind_4_derisk",
                    "grind_5_entry",
                    "grind_5_exit",
                    "grind_5_derisk",
                ]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
                stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
                constants: NfiX7AdjustmentConstants {
                    derisk_enable: false,
                    max_stake_multiplier: 1.0,
                    rebuy_stake_multiplier: Some(0.25),
                    derisk_levels: (1..=3)
                        .map(|level| NfiX7DeriskLevel {
                            level,
                            enabled: false,
                            threshold_futures: -0.1,
                            threshold_spot: -0.1,
                            stake_futures: 0.1,
                            stake_spot: 0.1,
                        })
                        .collect(),
                    grinds: (1..=5)
                        .map(|level| NfiX7GrindLevel {
                            level,
                            enabled: false,
                            use_derisk: false,
                            derisk_futures: -0.2,
                            derisk_spot: -0.2,
                            profit_threshold_futures: 0.02,
                            profit_threshold_spot: 0.02,
                            stakes_futures: vec![0.1],
                            stakes_spot: vec![0.1],
                            thresholds_futures: vec![-0.1],
                            thresholds_spot: vec![-0.1],
                        })
                        .collect(),
                    policy: Some(nfi_adjustment_policy()),
                },
            }),
            short_position_adjustment: None,
            constants: NfiManagedLongConstants {
                stops_enable: true,
                stop_threshold_futures: 0.1,
                stop_threshold_spot: 0.1,
                system_name_use: "system_v3_2".to_owned(),
                system_v3_2_name: "system_v3_2".to_owned(),
                system_v3_2_stop_threshold_doom_futures: 0.35,
                system_v3_2_stop_threshold_doom_spot: 0.12,
                system_v3_2_stops_enable: false,
                u_e_stops_enable: false,
            },
            programs: BTreeMap::from([
                ("long_exit_signals".to_owned(), first),
                ("long_exit_main".to_owned(), false_program.clone()),
                ("long_exit_williams_r".to_owned(), false_program.clone()),
                ("long_exit_dec".to_owned(), false_program.clone()),
                ("short_exit_signals".to_owned(), false_program.clone()),
                ("short_exit_main".to_owned(), false_program.clone()),
                ("short_exit_williams_r".to_owned(), false_program.clone()),
                ("short_exit_dec".to_owned(), false_program),
                (
                    "long_grind_entry_v3".to_owned(),
                    nfi_boolean_false_program(),
                ),
            ]),
            feature_projections: OnceLock::new(),
            feature_projection_unions: OnceLock::new(),
        }
    }

    fn enable_test_full_short_manager(manager: &mut NfiX7TradeManager) {
        let routes = vec![
            nfi_managed_route(
                "short_normal",
                NfiManagedLongProfile::Normal,
                "short_normal",
                &["501", "502"],
            ),
            nfi_managed_route(
                "short_pump",
                NfiManagedLongProfile::Pump,
                "short_pump",
                &["521"],
            ),
            nfi_managed_route(
                "short_quick",
                NfiManagedLongProfile::Quick,
                "short_quick",
                &["542"],
            ),
            {
                let mut route = nfi_managed_route(
                    "short_rebuy",
                    NfiManagedLongProfile::Rebuy,
                    "short_rebuy",
                    &["561", "562", "563"],
                );
                route.stop_threshold_futures = Some(1.4);
                route.stop_threshold_spot = Some(0.48);
                route
            },
            nfi_managed_route(
                "short_high_profit",
                NfiManagedLongProfile::HighProfit,
                "short_hp",
                &["581"],
            ),
            nfi_managed_route(
                "short_rapid",
                NfiManagedLongProfile::Rapid,
                "short_rapid",
                &["601"],
            ),
            nfi_managed_route(
                "short_scalp",
                NfiManagedLongProfile::Scalp,
                "short_scalp",
                &["661"],
            ),
            nfi_managed_route(
                "short_top_coins_fallback",
                NfiManagedLongProfile::Normal,
                "short_normal",
                &["641"],
            ),
        ];
        let regular_tags = routes
            .iter()
            .filter(|route| route.key != "short_rebuy")
            .flat_map(|route| route.entry_tags.clone())
            .collect();
        let mut short_adjustment = manager
            .position_adjustment
            .clone()
            .expect("test manager has source adjustment constants");
        short_adjustment.enabled = true;
        short_adjustment.entry_tags = regular_tags;
        short_adjustment.decision_program = "short_grind_entry_v3".to_owned();

        manager.schema_version = "0.15.0".to_owned();
        manager.short_route_order = routes.iter().map(|route| route.key.clone()).collect();
        manager.managed_short_routes = routes;
        manager.short_rebuy_adjustment.execution_scope = "rebuy-and-grind-v2".to_owned();
        manager.short_rebuy_adjustment.post_derisk_action = "short-position-adjustment".to_owned();
        manager.short_position_adjustment = Some(short_adjustment);
        manager.programs.insert(
            "short_grind_entry_v3".to_owned(),
            nfi_boolean_false_program(),
        );
    }

    fn nfi_adjustment_policy() -> NfiX7AdjustmentPolicy {
        let variable = |name: &str| NfiX7AdjustmentOperand::Variable {
            name: name.to_owned(),
        };
        let feature = |name: &str, multiplier: f64| NfiX7AdjustmentOperand::Feature {
            name: name.to_owned(),
            multiplier,
        };
        let literal = |value| NfiX7AdjustmentOperand::Literal { value };
        let condition = |left, operator, right| NfiX7AdjustmentCondition {
            left,
            operator,
            right,
        };
        let mut fallbacks = (1..=5)
            .map(|level| NfiX7GrindFallbackLevel {
                level,
                predicates: Vec::new(),
            })
            .collect::<Vec<_>>();
        fallbacks[3].predicates = vec![NfiX7AdjustmentPredicate {
            any_derisk_levels: Vec::new(),
            conditions: vec![
                condition(
                    variable("slice_profit_entry"),
                    NfiX7AdjustmentComparison::Lt,
                    literal(-0.06),
                ),
                condition(
                    variable("num_open_grinds_and_buybacks"),
                    NfiX7AdjustmentComparison::Eq,
                    literal(0.0),
                ),
                condition(
                    feature("RSI_14", 1.0),
                    NfiX7AdjustmentComparison::Lt,
                    literal(30.0),
                ),
                condition(
                    feature("close", 1.0),
                    NfiX7AdjustmentComparison::Lt,
                    feature("EMA_20", 0.98),
                ),
            ],
        }];
        fallbacks[4].predicates = vec![NfiX7AdjustmentPredicate {
            any_derisk_levels: vec![1, 2, 3],
            conditions: vec![
                condition(
                    variable("slice_profit_entry"),
                    NfiX7AdjustmentComparison::Lt,
                    literal(-0.06),
                ),
                condition(
                    feature("RSI_3", 1.0),
                    NfiX7AdjustmentComparison::Gt,
                    literal(10.0),
                ),
                condition(
                    feature("RSI_3_15m", 1.0),
                    NfiX7AdjustmentComparison::Gt,
                    literal(20.0),
                ),
                condition(
                    feature("AROONU_14", 1.0),
                    NfiX7AdjustmentComparison::Lt,
                    literal(50.0),
                ),
            ],
        }];
        NfiX7AdjustmentPolicy {
            entry_retry_ms: 5 * 60 * 1_000,
            stale_order_ms: 6 * 60 * 60 * 1_000,
            extra_entry_profit_condition: condition(
                variable("slice_profit"),
                NfiX7AdjustmentComparison::Lt,
                literal(-0.06),
            ),
            extra_entry_derisk_levels: vec![3],
            grind_entry_fallbacks: fallbacks,
        }
    }

    fn enable_nfi_manager(config: &mut PortfolioConfig, manager: NfiX7TradeManager) {
        config.stoploss_ratio = -0.99;
        config.callback_program = Some(CallbackProgram {
            order_filled: Some(OrderFilledProgram {
                initial_successful_entry_writes: vec![CustomDataWrite {
                    key: "system_version".to_owned(),
                    value: Value::String("system_v3_2".to_owned()),
                }],
                order_tag_actions: BTreeMap::new(),
            }),
        });
        config.nfi_x7_trade_manager = Some(manager);
    }

    fn nfi_pair(candles: Vec<Candle>, feature_columns: BTreeMap<String, Vec<Value>>) -> PairSeries {
        PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: feature_columns
                .into_iter()
                .map(|(name, values)| {
                    let encoded = serde_json::to_value(values).expect("test feature values encode");
                    let column = serde_json::from_value(encoded)
                        .expect("test feature values form one homogeneous column");
                    (name, column)
                })
                .collect(),
            candles: candles.into(),
        }
    }

    #[test]
    fn futures_ignores_simultaneous_long_and_short_entries() {
        let signal = EntrySignal {
            tag: Some("conflict".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let mut conflict = candle(1, 100.0, 100.0);
        conflict.enter_long = Some(signal.clone());
        conflict.enter_short = Some(signal);
        let mut futures_config = config(1);
        futures_config.is_futures = true;
        futures_config.leverage = Some(3.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: futures_config,
            pairs: vec![nfi_pair(
                vec![conflict, candle(2, 101.0, 101.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("Freqtrade suppresses a conflicting entry candle");

        assert!(result.trades.is_empty());
    }

    #[test]
    fn same_side_exit_signal_suppresses_entry() {
        let mut conflict = candle(1, 100.0, 100.0);
        conflict.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        conflict.exit_long = Some(ExitSignal {
            reason: "exit_signal".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![nfi_pair(
                vec![conflict, candle(2, 101.0, 101.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("Freqtrade suppresses entry beside a same-side exit");

        assert!(result.trades.is_empty());
    }

    #[test]
    fn futures_reopens_the_opposite_side_after_a_same_candle_exit() {
        let mut short_entry = candle(0, 100.0, 100.0);
        short_entry.enter_short = Some(EntrySignal {
            tag: Some("short".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut reversal = candle(1, 95.0, 95.0);
        reversal.enter_long = Some(EntrySignal {
            tag: Some("long".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        reversal.exit_short = Some(ExitSignal {
            reason: "short-exit".to_owned(),
        });
        let mut long_exit = candle(2, 96.0, 96.0);
        long_exit.exit_long = Some(ExitSignal {
            reason: "long-exit".to_owned(),
        });
        let mut futures_config = config(1);
        futures_config.is_futures = true;
        futures_config.leverage = Some(2.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: futures_config,
            pairs: vec![nfi_pair(
                vec![short_entry, reversal, long_exit],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("Freqtrade futures same-candle reversal");

        assert_eq!(result.trades.len(), 2);
        assert!(result.trades[0].is_short);
        assert_eq!(result.trades[0].close_timestamp_ms, 1);
        assert!(!result.trades[1].is_short);
        assert_eq!(result.trades[1].open_timestamp_ms, 1);
        assert_eq!(result.trades[1].entry_tag.as_deref(), Some("long"));
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn adjustment_minimum_stake_uses_unleveraged_freqtrade_boundary() {
        let mut pair = nfi_pair(Vec::new(), BTreeMap::new());
        pair.minimum_amount = Some(1.0);
        pair.minimum_cost = Some(5.0);

        let adjustment_minimum = adjustment_minimum_pair_stake(&pair, 17.213, 0.05);
        let leverage_aware_minimum = minimum_pair_stake(&pair, 17.213, -0.1, 3.0, 0.05);

        // The APE futures market is amount-limited at one contract. Freqtrade
        // exposes 17.213 * 1.05 to adjust_trade_position even on a 3x trade.
        assert!((adjustment_minimum - 18.07365).abs() < 1e-12);
        assert!((leverage_aware_minimum * 3.0 - adjustment_minimum).abs() < 1e-12);
    }

    #[test]
    fn ft_precise_partial_exit_division_preserves_integer_contract() {
        let raw_amount =
            precise_product_quotient(2_913.868_487_754_348_3, 2_616.0, 2_927.296_453_135_704_3)
                .expect("valid Freqtrade partial-exit conversion");

        // These are the pinned X7 trade values immediately before order 145.
        // Unlimited rational division lands just below 2604 and loses one
        // integer contract; CCXT Precise's 18-place division lands above it.
        assert_eq!(floor_step(raw_amount, 1.0), 2_604.0);
    }

    #[test]
    fn nfi_grind_wallet_rejection_stops_source_order_evaluation() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment_candle = candle(7 * HOUR, 90.0, 90.0);
        let mut force_exit = candle(8 * HOUR, 90.0, 90.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        let adjustment = manager
            .position_adjustment
            .as_mut()
            .expect("test manager has position adjustment");
        adjustment.enabled = true;
        // With 900 USDT already tied up, the first source-ordered grind asks
        // for 180 USDT while the wallet has less than 100 USDT available.
        // Grind 4 would fit at 45 USDT, but NFI returns None at grind 1 and
        // never evaluates that later branch.
        adjustment.constants.grinds[0].enabled = true;
        adjustment.constants.grinds[0].stakes_spot = vec![0.2];
        adjustment.constants.grinds[3].enabled = true;
        adjustment.constants.grinds[3].stakes_spot = vec![0.05];

        let mut portfolio = config(1);
        portfolio.starting_balance = 1_000.0;
        portfolio.stake_amount = 900.0;
        enable_nfi_manager(&mut portfolio, manager);
        let values = |value| vec![Value::from(value), Value::from(value), Value::from(value)];
        let mut pair = nfi_pair(
            vec![entry, adjustment_candle, force_exit],
            BTreeMap::from([
                ("RSI_3".to_owned(), values(50.0)),
                ("RSI_3_15m".to_owned(), values(50.0)),
                ("RSI_14".to_owned(), values(50.0)),
                ("close".to_owned(), values(90.0)),
                ("EMA_20".to_owned(), values(90.0)),
            ]),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![pair],
        })
        .expect("wallet rejection is a normal NFI callback result");

        assert_eq!(result.trades[0].orders.len(), 2);
        assert_eq!(result.trades[0].orders[0].tag.as_deref(), Some("141 "));
        assert_eq!(
            result.trades[0].orders[1].tag.as_deref(),
            Some("force_exit")
        );
    }

    #[test]
    fn global_slot_competition_uses_pair_order() {
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("first".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut second = first.clone();
        second.enter_long = Some(EntrySignal {
            tag: Some("second".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![
                PairSeries {
                    pair: "AAA/USDT".to_owned(),
                    execution_start_index: 0,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![first, candle(2, 100.0, 100.0)].into(),
                },
                PairSeries {
                    pair: "BBB/USDT".to_owned(),
                    execution_start_index: 0,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![second, candle(2, 100.0, 100.0)].into(),
                },
            ],
        };

        let result = simulate(&input).expect("valid simulation");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].pair, "AAA/USDT");
        assert_eq!(result.rejected_signals, 1);
    }

    #[test]
    fn final_force_exits_export_newest_open_trade_first() {
        let pair = |name: &str, entry_timestamp_ms: i64| {
            let mut entry = candle(entry_timestamp_ms, 100.0, 100.0);
            entry.enter_long = Some(EntrySignal {
                tag: Some(name.to_owned()),
                leverage: None,
                liquidation_price: None,
            });
            PairSeries {
                pair: name.to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, candle(2, 100.0, 100.0)].into(),
            }
        };
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(2),
            pairs: vec![pair("OLDER/USDT", 0), pair("NEWER/USDT", 1)],
        };

        let result = simulate(&input).expect("valid force-exit ordering simulation");

        assert_eq!(result.trades.len(), 2);
        assert_eq!(result.trades[0].pair, "NEWER/USDT");
        assert_eq!(result.trades[1].pair, "OLDER/USDT");
        assert!(result
            .trades
            .iter()
            .all(|trade| trade.exit_reason == "force_exit"));
    }

    #[test]
    fn open_trade_pairs_run_before_configured_pair_order() {
        let mut later_entry = candle(1, 100.0, 100.0);
        later_entry.enter_long = Some(EntrySignal {
            tag: Some("after-close".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut initial_entry = candle(0, 100.0, 100.0);
        initial_entry.enter_long = Some(EntrySignal {
            tag: Some("initial".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut same_candle_exit = candle(1, 101.0, 101.0);
        same_candle_exit.exit_long = Some(ExitSignal {
            reason: "scheduled-exit".to_owned(),
        });
        let pair = |name: &str, candles: Vec<Candle>| PairSeries {
            pair: name.to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: candles.into(),
        };
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            // AAA is deliberately first in configured order. Freqtrade still
            // processes BBB first at timestamp 1 because BBB has an open
            // trade, freeing the sole slot before AAA's entry is evaluated.
            pairs: vec![
                pair(
                    "AAA/USDT",
                    vec![
                        candle(0, 100.0, 100.0),
                        later_entry,
                        candle(2, 100.0, 100.0),
                    ],
                ),
                pair(
                    "BBB/USDT",
                    vec![initial_entry, same_candle_exit, candle(2, 101.0, 101.0)],
                ),
            ],
        };

        let result = simulate(&input).expect("valid open-trade-first simulation");

        assert_eq!(result.trades.len(), 2);
        assert_eq!(result.trades[0].pair, "BBB/USDT");
        assert_eq!(result.trades[1].pair, "AAA/USDT");
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn profiled_simulation_preserves_result_and_counts_only_visible_rows() {
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![candle(0, 100.0, 100.0), first, candle(2, 101.0, 101.0)].into(),
            }],
        };

        let ordinary = simulate(&input).expect("valid ordinary simulation");
        let (profiled, profile) = simulate_profiled(&input).expect("valid profiled simulation");

        assert_eq!(profiled, ordinary);
        assert_eq!(profile.timestamp_batches, 2);
        assert_eq!(profile.pair_events, 2);
    }

    #[test]
    fn sparse_profile_counts_distinct_visible_timestamps_during_validation() {
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![
                PairSeries {
                    pair: "AAA/USDT".to_owned(),
                    execution_start_index: 1,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![
                        candle(0, 100.0, 100.0),
                        candle(1, 100.0, 100.0),
                        candle(2, 100.0, 100.0),
                    ]
                    .into(),
                },
                PairSeries {
                    pair: "BBB/USDT".to_owned(),
                    execution_start_index: 1,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![
                        candle(0, 100.0, 100.0),
                        candle(2, 100.0, 100.0),
                        candle(3, 100.0, 100.0),
                    ]
                    .into(),
                },
            ],
        };

        let (_, profile) = simulate_profiled(&input).expect("valid profiled simulation");

        assert_eq!(profile.timestamp_batches, 3);
        assert_eq!(profile.pair_events, 4);
    }

    #[test]
    fn timerange_stop_boundary_does_not_open_a_new_trade() {
        let mut boundary = candle(2, 101.0, 100.0);
        boundary.enter_long = Some(EntrySignal {
            tag: Some("boundary".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![boundary].into(),
            }],
        };

        let result = simulate(&input).expect("valid stop-boundary candle");

        assert!(result.trades.is_empty());
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn callback_context_rows_are_visible_but_never_executed() {
        let mut context = candle(1, 90.0, 90.0);
        context.enter_long = Some(EntrySignal {
            tag: Some("context-only".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut executable = candle(2, 100.0, 100.0);
        executable.enter_long = Some(EntrySignal {
            tag: Some("executable".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![context, executable, candle(3, 101.0, 101.0)].into(),
            }],
        };

        let result = simulate(&input).expect("context boundary is valid");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].open_timestamp_ms, 2);
        assert_eq!(result.trades[0].entry_tag.as_deref(), Some("executable"));
        assert_eq!(result.rejected_signals, 0);
    }

    #[test]
    fn execution_start_index_must_point_to_a_candle() {
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![candle(1, 100.0, 100.0)].into(),
            }],
        };

        assert_eq!(
            simulate(&input),
            Err(SimError::InvalidExecutionStart {
                pair: "AAA/USDT".to_owned(),
                index: 1,
                rows: 1,
            })
        );
    }

    #[test]
    fn pairwise_profit_sum_matches_numpy_reduction_order() {
        let profits = [
            13.433_598_31,
            5.716_389_78,
            8.516_438_52,
            1.152_679_260_000_020_2,
            2.817_485_03,
            2.228_106_82,
            0.982_624_96,
            0.735_159,
            2.030_196_569_999_998,
            2.782_651_25,
            2.093_312_4,
            0.941_256_3,
        ];

        assert_eq!(pairwise_sum(&profits), 43.429_898_200_000_025);
    }

    #[test]
    fn pairwise_profit_sum_matches_x7_annual_pandas_token() {
        let profits = [
            145.507_105_8,
            1_169.701_240_65,
            753.539_616,
            382.422_002_739_998_7,
            627.860_778,
            284.871_360_94,
            576.035_552,
            417.658_364_52,
            248.585_082_58,
            541.245_411_6,
            -4_831.775_913_230_002_5,
        ];

        assert_eq!(pairwise_sum(&profits), 315.650_601_599_995_75);
    }

    #[test]
    fn total_volume_uses_cpython_compensated_sum() {
        // Costs are the exact serialized values from the latest X7 tag-120
        // ZEC fixture. A naive Rust fold ends in ...00004; CPython/Freqtrade
        // exports ...9999.
        let costs = [
            32.994_561_599_999_99,
            24.689_464_799_999_996,
            39.540_500_999_999_99,
            40.349_809_499_999_99,
            39.569_630_1,
            32.969_036_1,
            33.636_302_699_999_995,
            40.446_706_299_999_995,
            39.507_467_999_999_99,
            40.566_726_199_999_99,
            39.462_122_7,
            40.322_482_199_999_996,
            12.908_996_1,
        ];

        assert_eq!(python_float_sum(costs), 456.963_807_299_999_9);
        assert_ne!(costs.into_iter().sum::<f64>(), 456.963_807_299_999_9);
    }

    #[test]
    fn nfi_top_coins_pure_decision_exits_with_the_original_entry_tag() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141 142".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_profit_program(0.01, "exit_long_tc_test")),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 103.0, 102.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("supported top-coins route");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].exit_reason, "exit_long_tc_test ( 141 142)");
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_short_rebuy_runs_the_short_program_order_with_leverage() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("562 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_full_short_manager(&mut manager);
        manager.programs.insert(
            "short_exit_dec".to_owned(),
            nfi_profit_program(0.01, "exit_short_rebuy_d_3_100"),
        );
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        manager_config.leverage = Some(3.0);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0)], BTreeMap::new());
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("bounded short-rebuy route");

        assert_eq!(result.trades.len(), 1);
        assert!(result.trades[0].is_short);
        assert_eq!(result.trades[0].leverage, 3.0);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_short_rebuy_d_3_100 ( 562 )"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_short_route_matching_preserves_each_upstream_tag_predicate() {
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.managed_short_routes.extend([
            nfi_managed_route(
                "short_quick",
                NfiManagedLongProfile::Quick,
                "short_quick",
                &["541", "542"],
            ),
            nfi_managed_route(
                "short_scalp",
                NfiManagedLongProfile::Scalp,
                "short_scalp",
                &["661"],
            ),
            nfi_managed_route(
                "short_top_coins_fallback",
                NfiManagedLongProfile::Normal,
                "short_normal",
                &["641", "642"],
            ),
        ]);
        let rebuy = manager
            .managed_short_routes
            .iter()
            .find(|route| route.key == "short_rebuy")
            .expect("test manager has short rebuy");
        let quick = manager
            .managed_short_routes
            .iter()
            .find(|route| route.key == "short_quick")
            .expect("test manager has short quick");
        let scalp = manager
            .managed_short_routes
            .iter()
            .find(|route| route.key == "short_scalp")
            .expect("test manager has short scalp");
        let top_coins = manager
            .managed_short_routes
            .iter()
            .find(|route| route.key == "short_top_coins_fallback")
            .expect("test manager has short top-coins fallback");

        // Quick uses any(...), rebuy uses all(...), scalp permits its explicit
        // rebuy compound, and top-coins reaches normal fallback only when no
        // earlier explicit short family is present.
        assert!(nfi_managed_short_route_supports_tags(
            &manager,
            quick,
            &["542", "141"],
        ));
        assert!(!nfi_managed_short_route_supports_tags(
            &manager,
            rebuy,
            &["562", "141"],
        ));
        assert!(nfi_managed_short_route_supports_tags(
            &manager,
            scalp,
            &["661", "562"],
        ));
        assert!(nfi_managed_short_route_supports_tags(
            &manager,
            top_coins,
            &["641"],
        ));
        assert!(!nfi_managed_short_route_supports_tags(
            &manager,
            top_coins,
            &["641", "542"],
        ));
    }

    #[test]
    fn nfi_short_quick_inline_exit_uses_mirrored_thresholds() {
        let route = nfi_managed_route(
            "short_quick",
            NfiManagedLongProfile::Quick,
            "short_quick",
            &["542"],
        );
        let pair = nfi_pair(
            vec![candle(1, 97.0, 97.0)],
            BTreeMap::from([
                ("RSI_14".to_owned(), vec![serde_json::json!(21.0)]),
                ("MFI_14".to_owned(), vec![serde_json::json!(50.0)]),
                ("WILLR_14".to_owned(), vec![serde_json::json!(-50.0)]),
                ("RSI_3".to_owned(), vec![serde_json::json!(50.0)]),
                ("RSI_3_15m".to_owned(), vec![serde_json::json!(50.0)]),
            ]),
        );
        let snapshot = NfiProfitSnapshot {
            stake: 1.0,
            ratio: 0.03,
            current_stake_ratio: 0.03,
            initial_stake_ratio: 0.03,
        };

        let decision = nfi_inline_profile_exit(&route, &pair, 0, snapshot, TradeSide::Short)
            .expect("short quick inputs are complete");

        assert_eq!(decision, (true, Some("exit_short_quick_q_1".to_owned())));
    }

    #[test]
    fn nfi_normal_skips_profit_programs_while_initial_stake_is_negative() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("1".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 99.0, 99.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            // The predicate would return true at -1%, so a custom exit here
            // would prove that the source's positive-profit guard was lost.
            nfi_top_coins_manager(nfi_profit_program(-1.0, "should_not_run")),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 99.0, 99.0), force_exit],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("normal positive-profit guard");

        assert_eq!(result.trades[0].exit_reason, "force_exit");
    }

    #[test]
    fn nfi_rebuy_terminal_exit_uses_source_compiled_policy() {
        const MINUTE: i64 = 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("65 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .managed_long_routes
            .iter_mut()
            .find(|route| route.profile == NfiManagedLongProfile::Rebuy)
            .expect("test manager has a rebuy route")
            .terminal_exit = Some(NfiManagedTerminalExit {
            entry_tags: vec!["65".to_owned()],
            minimum_age_ms: 90 * MINUTE,
            minimum_profit_ratio: 0.0125,
            reason: "exit_long_rebuy_signal65_early_recovery".to_owned(),
        });
        assert!(
            manager
                .managed_long_routes
                .iter()
                .all(valid_nfi_managed_long_route),
            "source-compiled managed routes must validate"
        );
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let neutral_values = vec![serde_json::json!(0.0); 4];
        let mut pair = nfi_pair(
            vec![
                entry,
                // Profit is sufficient, but the source age gate is not.
                candle(89 * MINUTE, 102.0, 102.0),
                // Age is sufficient, but the source profit gate is not.
                candle(90 * MINUTE, 101.0, 101.0),
                candle(91 * MINUTE, 102.0, 102.0),
            ],
            BTreeMap::from([
                ("RSI_14".to_owned(), vec![serde_json::json!(50.0); 4]),
                ("CMF_20".to_owned(), neutral_values.clone()),
                ("CMF_20_1h".to_owned(), neutral_values.clone()),
                ("CMF_20_4h".to_owned(), neutral_values.clone()),
                ("ROC_9_4h".to_owned(), neutral_values),
            ]),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("source-compiled rebuy terminal exit");

        assert_eq!(result.trades[0].close_timestamp_ms, 91 * MINUTE);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_rebuy_signal65_early_recovery ( 65 )"
        );
    }

    #[test]
    fn nfi_quick_runs_inline_exit_after_the_common_stop_check() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("41".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let features = BTreeMap::from([
            (
                "RSI_14".to_owned(),
                vec![serde_json::json!(79.0), serde_json::json!(50.0)],
            ),
            (
                "MFI_14".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
            (
                "WILLR_14".to_owned(),
                vec![serde_json::json!(-50.0), serde_json::json!(-50.0)],
            ),
            (
                "RSI_3".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![serde_json::json!(50.0), serde_json::json!(50.0)],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(vec![entry, candle(2, 103.0, 103.0)], features)],
        };

        let result = simulate(&input).expect("quick inline profile exit");

        assert_eq!(result.trades[0].exit_reason, "exit_long_quick_q_1 ( 41)");
    }

    #[test]
    fn nfi_high_profit_returns_a_doom_stop_without_waiting_for_target_replay() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("81".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.constants.system_v3_2_stops_enable = true;
        manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 94.0, 94.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("high-profit immediate stop policy");

        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_hp_stoploss_doom ( 81)"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
    }

    #[test]
    fn nfi_top_coins_profit_target_trails_on_the_next_candle() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let features = BTreeMap::from([
            (
                "RSI_14".to_owned(),
                vec![
                    serde_json::json!(55.0),
                    serde_json::json!(60.0),
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                ],
            ),
            (
                "CMF_20".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "CMF_20_1h".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "CMF_20_4h".to_owned(),
                vec![
                    serde_json::json!(0.1),
                    serde_json::json!(0.1),
                    serde_json::json!(-0.1),
                    serde_json::json!(-0.1),
                ],
            ),
            (
                "ROC_9_4h".to_owned(),
                vec![
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![
                    entry,
                    candle(2, 110.0, 109.0),
                    candle(3, 106.0, 105.0),
                    candle(4, 106.0, 105.0),
                ],
                features,
            )],
        };

        let result = simulate(&input).expect("exact top-coins trailing target");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_profit_long_tc_t_5_1_m ( 141)"
        );
        // Candle 3's indicator values are not visible until candle 4 opens.
        assert_eq!(result.trades[0].close_timestamp_ms, 4);
    }

    #[test]
    fn nfi_top_coins_doom_stop_is_reserved_then_exits_with_m_suffix() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("145".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.constants.system_v3_2_stops_enable = true;
        manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 94.0, 93.0), candle(3, 94.0, 93.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("two-phase NFI doom stop");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_tc_stoploss_doom_m ( 145)"
        );
        assert_eq!(result.trades[0].close_timestamp_ms, 3);
    }

    #[test]
    fn nfi_trade_manager_rejects_unsupported_entry_tags_before_simulation() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "120"
        ));
    }

    #[test]
    fn nfi_trade_manager_rejects_a_mixed_unknown_tag() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            // Rebuy is compiled, but one unknown word can still select an
            // unreviewed source branch after future strategy changes.
            tag: Some("61 999".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "61 999"
        ));
    }

    #[test]
    fn nfi_trade_manager_accepts_a_compiled_cross_side_compound_noop() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            // X7's shared enter_tag column can contain a simultaneous short
            // label in spot mode. Neither all-tags route matches this pair.
            tag: Some("101 562 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("compiled cross-side compound");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].entry_tag.as_deref(), Some("101 562 "));
        assert_eq!(result.trades[0].exit_reason, "force_exit");
    }

    #[test]
    fn nfi_cross_side_compound_keeps_source_callback_order() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            // The source evaluates long-normal's any-tag branch before the
            // short branches, regardless of the opened trade side.
            tag: Some("1 562".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_profit_program(0.01, "exit_long_normal_test")),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 90.0, 90.0)],
                BTreeMap::new(),
            )],
        };

        let result = simulate(&input).expect("source-ordered cross-side callbacks");

        assert!(result.trades[0].is_short);
        assert_eq!(
            result.trades[0].exit_reason,
            "exit_long_normal_test ( 1 562)"
        );
    }

    #[test]
    fn nfi_trade_manager_requires_a_tag_for_the_opened_side() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("562".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "562"
        ));
    }

    #[test]
    fn nfi_validation_rejects_an_unsupported_short_tag_in_the_main_pass() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("999".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(2, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
                if entry_tag == "999"
        ));
    }

    #[test]
    fn nfi_validation_keeps_general_candle_errors_ahead_of_short_tag_errors() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("999".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![nfi_pair(
                vec![entry, candle(1, 100.0, 100.0)],
                BTreeMap::new(),
            )],
        };

        assert!(matches!(
            simulate(&input),
            Err(SimError::CandleOrder { index: 1, .. })
        ));
    }

    #[test]
    fn nfi_rebuy_adds_the_first_source_ladder_entry() {
        let mut entry = candle(1, 100.0, 100.0);
        // OHLC columns are read from the candle, not duplicated feature
        // storage. This is the analyzed close visible to candle 2.
        entry.close = 90.0;
        entry.enter_long = Some(EntrySignal {
            tag: Some("61".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 100.0, 100.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        // Callback features are shifted by one row: the candle-2 callback
        // reads index 0, exactly as Freqtrade reads its last analyzed candle.
        let features = BTreeMap::from([
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                ],
            ),
            (
                "RSI_3".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "AROONU_14".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "AROONU_14_15m".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "close".to_owned(),
                vec![
                    serde_json::json!(90.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
            (
                "EMA_26".to_owned(),
                vec![
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0), force_exit], features);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed rebuy ladder entry");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("r"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(trade.orders[1].price, 90.0);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_rebuy_derisk_leaves_the_exchange_minimum_reserve() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("65".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(3, 40.0, 40.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        // A false protection gate disables the entry branch. The de-risk
        // branch is intentionally independent of the indicator predicate.
        let features = BTreeMap::from([
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                ],
            ),
            (
                "RSI_3".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "RSI_3_15m".to_owned(),
                vec![
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                    serde_json::json!(20.0),
                ],
            ),
            (
                "AROONU_14".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "AROONU_14_15m".to_owned(),
                vec![
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                    serde_json::json!(10.0),
                ],
            ),
            (
                "close".to_owned(),
                vec![
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                    serde_json::json!(40.0),
                ],
            ),
            (
                "EMA_26".to_owned(),
                vec![
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                    serde_json::json!(100.0),
                ],
            ),
        ]);
        let mut manager_config = config(1);
        enable_nfi_manager(
            &mut manager_config,
            nfi_top_coins_manager(nfi_false_program()),
        );
        let mut pair = nfi_pair(vec![entry, candle(2, 40.0, 40.0), force_exit], features);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed rebuy de-risk");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk_level_3"));
        assert!(!trade.orders[1].is_entry);
        assert!(trade.stake_amount < trade.max_stake_amount);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_rebuy_transfer_restores_the_source_slice_before_grind_sizing() {
        const STEP: i64 = 10 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("64".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let derisk = candle(STEP, 40.0, 40.0);
        let grind = candle(2 * STEP, 39.0, 39.0);
        let mut force_exit = candle(3 * STEP, 39.0, 39.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        let numeric_values = |value| {
            vec![
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
            ]
        };
        let features = BTreeMap::from([
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                ],
            ),
            ("RSI_3".to_owned(), numeric_values(20.0)),
            ("RSI_3_15m".to_owned(), numeric_values(20.0)),
            ("AROONU_14".to_owned(), numeric_values(10.0)),
            ("AROONU_14_15m".to_owned(), numeric_values(10.0)),
            ("RSI_14".to_owned(), numeric_values(20.0)),
            ("close".to_owned(), numeric_values(39.0)),
            ("EMA_20".to_owned(), numeric_values(100.0)),
            ("EMA_26".to_owned(), numeric_values(100.0)),
        ]);
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        let position_adjustment = manager
            .position_adjustment
            .as_mut()
            .expect("test manager has position adjustment");
        position_adjustment.enabled = true;
        position_adjustment.constants.grinds[3].enabled = true;
        position_adjustment.constants.grinds[3].stakes_futures = vec![0.05];
        position_adjustment.constants.grinds[3].stakes_spot = vec![0.05];
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, derisk, grind, force_exit], features);
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed rebuy-to-grind transfer");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 4);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk_level_3"));
        assert_eq!(trade.orders[2].tag.as_deref(), Some("grind_4_entry"));
        // X7 divides the initial rebuy entry by 0.25, then applies grind 4's
        // 0.05 first stake. Precision can floor the filled amount, so assert
        // the exact source-sized region instead of a pre-fill decimal.
        assert!(
            trade.orders[2].cost > trade.orders[0].cost * 0.19
                && trade.orders[2].cost < trade.orders[0].cost * 0.21,
            "initial cost {}, transferred grind cost {}",
            trade.orders[0].cost,
            trade.orders[2].cost
        );
    }

    #[test]
    fn nfi_short_rebuy_transfer_accepts_the_source_rebuy_tag() {
        const STEP: i64 = 10 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("562".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let first_rebuy = candle(STEP, 114.0, 114.0);
        let second_rebuy = candle(2 * STEP, 128.0, 128.0);
        let mut derisk = candle(3 * STEP, 145.0, 145.0);
        derisk.high = 145.0;
        let mut grind = candle(4 * STEP, 140.0, 140.0);
        grind.high = 140.0;
        let mut force_exit = candle(5 * STEP, 140.0, 140.0);
        force_exit.high = 140.0;
        force_exit.exit_short = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });
        let numeric_values = |value| {
            vec![
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
                serde_json::json!(value),
            ]
        };
        let features = BTreeMap::from([
            (
                "protections_short_global".to_owned(),
                vec![
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(true),
                ],
            ),
            (
                "protections_long_global".to_owned(),
                vec![
                    serde_json::json!(true),
                    serde_json::json!(true),
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                    serde_json::json!(false),
                ],
            ),
            ("RSI_3".to_owned(), numeric_values(20.0)),
            ("RSI_3_15m".to_owned(), numeric_values(20.0)),
            ("AROOND_14".to_owned(), numeric_values(10.0)),
            ("AROOND_14_15m".to_owned(), numeric_values(10.0)),
            ("close".to_owned(), numeric_values(100.0)),
            ("EMA_26".to_owned(), numeric_values(200.0)),
        ]);
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_full_short_manager(&mut manager);
        manager.programs.insert(
            "short_grind_entry_v3".to_owned(),
            nfi_boolean_true_program(),
        );
        let adjustment = manager
            .short_position_adjustment
            .as_mut()
            .expect("test manager has short position adjustment");
        adjustment.constants.grinds[3].enabled = true;
        adjustment.constants.grinds[3].stakes_futures = vec![0.05];

        let mut manager_config = config(1);
        manager_config.is_futures = true;
        manager_config.leverage = Some(2.0);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, first_rebuy, second_rebuy, derisk, grind, force_exit],
            features,
        );
        pair.minimum_cost = Some(5.0);
        pair.minimum_amount = Some(0.1);
        pair.amount_step = Some(0.1);
        pair.price_step = Some(0.001);
        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("source short rebuy-to-grind transfer");
        let trade = &result.trades[0];
        let derisk_index = trade
            .orders
            .iter()
            .position(|order| order.tag.as_deref() == Some("derisk_level_3"))
            .expect("short rebuy reaches its level-3 transfer");
        let remaining_amount = remaining_after_partial_exit(&trade.orders, derisk_index);

        let post_derisk_tag = trade.orders[derisk_index + 1].tag.as_deref();
        assert_eq!(post_derisk_tag, Some("grind_4_entry"));
        assert!(trade.orders[derisk_index + 1].is_entry);
        // Dividing the callback minimum by leverage leaves 0.2 contracts;
        // retaining the raw exchange minimum would incorrectly leave 0.4.
        assert!((remaining_amount - 0.2).abs() < 1e-12);
    }

    #[test]
    fn nfi_long_grind_recovers_the_first_entry_once_with_gm0() {
        let mut entry = candle(1, 3.957, 3.957);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let recovery = candle(2, 4.037, 4.037);
        let mut force_exit = candle(3, 4.178, 4.178);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "spot-grind-backtest-v1".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: None,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
            regular_decision_program: None,
            regular_constants: None,
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        manager_config.price_step = 0.001;
        manager_config.amount_step = 0.01;
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, recovery, force_exit], BTreeMap::new());
        pair.price_step = Some(0.001);
        pair.amount_step = Some(0.01);
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed long-grind recovery route");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("gm0"));
        assert_eq!(trade.orders[1].price, 4.037);
        assert!(!trade.orders[1].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_grind_dual_mode_scope_accepts_a_futures_trade() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: Some(3.0),
            liquidation_price: None,
        });
        let ordinary_callback = candle(2, 99.0, 99.0);
        let mut force_exit = candle(3, 99.0, 99.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.schema_version = "0.14.0".to_owned();
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "grind-backtest-v2".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: None,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
            regular_decision_program: None,
            regular_constants: None,
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, ordinary_callback, force_exit], BTreeMap::new());
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed tag-120 futures callback route");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].entry_tag.as_deref(), Some("120 "));
        assert_eq!(result.trades[0].exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_grind_uses_the_source_bound_futures_drawdown_fallback() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: Some(3.0),
            liquidation_price: None,
        });
        // -22% from the last order is beyond -0.65 / 3. The ordinary grind
        // predicate remains false and the candle is far younger than every
        // age gate, so only the source's futures fallback can add this order.
        let fallback = candle(2, 78.0, 78.0);
        let mut force_exit = candle(3, 78.0, 78.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.schema_version = "0.14.0".to_owned();
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "grind-backtest-v2".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: Some(-0.65),
            derisk_use_grind_stops: false,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
            regular_decision_program: None,
            regular_constants: None,
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, fallback, force_exit], BTreeMap::new());
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("source-bound futures drawdown fallback");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
        assert_eq!(trade.orders[1].filled_timestamp_ms, 2);
        assert!(trade.orders[1].is_entry);
    }

    #[test]
    fn nfi_long_grind_wallet_rejection_stops_before_smaller_later_clusters() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let oversized_first_cluster = candle(25 * HOUR, 90.0, 90.0);
        let mut force_exit = candle(26 * HOUR, 90.0, 90.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut constants = nfi_legacy_grind_constants();
        // The first matching cluster asks for more than the remaining wallet,
        // while gd6 would fit. NFI returns None at the first wallet guard and
        // never lets the smaller later cluster bypass source order.
        constants.clusters[0].stakes_spot = vec![1.0];
        constants.clusters[0].thresholds_spot = vec![-0.12];
        constants.clusters[5].stakes_spot = vec![0.1];
        constants.clusters[5].thresholds_spot = vec![-0.03];

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "spot-grind-backtest-v1".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: None,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants,
            regular_decision_program: None,
            regular_constants: None,
        });
        manager.route_order.insert(6, "long_grind".to_owned());

        let mut manager_config = config(1);
        manager_config.starting_balance = 175.0;
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, oversized_first_cluster, force_exit],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("wallet rejection is an ordinary callback no-op");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 2);
        assert_eq!(trade.orders[0].tag.as_deref(), Some("120 "));
        assert_eq!(trade.orders[1].tag.as_deref(), Some("force_exit"));
    }

    #[test]
    fn nfi_long_grind_opens_and_closes_a_gd1_cluster_in_source_order() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("120 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let grind_entry = candle(25 * HOUR, 90.0, 90.0);
        let grind_exit = candle(26 * HOUR, 93.0, 93.0);
        let mut force_exit = candle(27 * HOUR, 93.0, 93.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        manager.long_grind = Some(NfiLongGrindRoute {
            mode_name: "long_grind".to_owned(),
            entry_tags: vec!["120".to_owned()],
            exit_profit_threshold: 0.25,
            adjustment_scope: "spot-grind-backtest-v1".to_owned(),
            grind_mode: true,
            decision_program: "long_grind_entry_v3".to_owned(),
            first_entry_profit_threshold_spot: 0.018,
            first_entry_stop_threshold_spot: -0.2,
            futures_fallback_loss_threshold: None,
            derisk_use_grind_stops: true,
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: nfi_legacy_grind_constants(),
            regular_decision_program: None,
            regular_constants: None,
        });
        manager.route_order.insert(6, "long_grind".to_owned());
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, grind_entry, grind_exit, force_exit],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);
        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed legacy grind cluster");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 4);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(
            trade.orders[2].tag.as_deref(),
            Some(format!("gd1 {}", trade.orders[1].id).as_str())
        );
        assert!(!trade.orders[2].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_btc_uses_its_source_ordered_profit_exit() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_long_btc(
            &mut manager,
            nfi_regular_adjustment_constants(),
            nfi_false_program(),
        );
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, candle(2, 126.0, 126.0), candle(3, 126.0, 126.0)],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        };

        let result = simulate(&input).expect("reviewed long-btc route");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].close_timestamp_ms, 2);
        assert_eq!(result.trades[0].exit_reason, "exit_long_btc_g ( 121)");
    }

    #[test]
    fn nfi_long_btc_regular_mode_opens_and_closes_g1_in_source_order() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(5 * HOUR, 93.0, 93.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut constants = nfi_regular_adjustment_constants();
        // This case isolates g1. Rebuy and de-risk remain structurally valid
        // but cannot match the selected prices.
        constants.rebuy_thresholds_spot.fill(-1.0);
        constants.derisk_threshold_spot = -1.0;
        constants.derisk_level_1_threshold_spot = -1.0;
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![
                entry,
                candle(3 * HOUR, 90.0, 90.0),
                candle(4 * HOUR, 93.0, 93.0),
                force_exit,
            ],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed tag-121 regular adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 4);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("g1"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(
            trade.orders[2].tag.as_deref(),
            Some(format!("g1 {}", trade.orders[1].id).as_str())
        );
        assert!(!trade.orders[2].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_btc_futures_selects_the_futures_regular_branch() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(14 * HOUR, 90.0, 90.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut constants = nfi_regular_adjustment_constants();
        // Only the futures rebuy threshold can match. This proves mode
        // selection without relying on the two branches sharing today's X7
        // values.
        constants.rebuy_thresholds_futures.fill(-0.01);
        constants.rebuy_thresholds_spot.fill(-1.0);
        constants.derisk_threshold_futures = -1.0;
        constants.derisk_level_1_threshold_futures = -1.0;
        for grind in &mut constants.grinds {
            grind.thresholds_futures.fill(-1.0);
            grind.thresholds_spot.fill(-1.0);
        }
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
        let mut manager_config = config(1);
        manager_config.is_futures = true;
        manager_config.leverage = Some(3.0);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, candle(13 * HOUR, 90.0, 90.0), force_exit],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed tag-121 futures adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.leverage, 3.0);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("r"));
        assert!(trade.orders[1].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn nfi_long_btc_futures_funding_precedes_regular_adjustment() {
        const MINUTE: i64 = 60 * 1_000;
        let mut entry = candle(0, 21_084.0, 21_084.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut funding = candle(435 * MINUTE, 20_913.0, 20_913.0);
        funding.funding_rate = Some(0.000_458_47);
        funding.funding_mark_price = Some(20_913.0);
        let adjustment = candle(515 * MINUTE, 20_476.4, 20_476.4);
        let mut force_exit = candle(520 * MINUTE, 20_476.4, 20_476.4);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut constants = nfi_regular_adjustment_constants();
        constants.rebuy_thresholds_futures.fill(-1.0);
        constants.derisk_threshold_futures = -1.0;
        constants.derisk_level_1_threshold_futures = -1.0;
        for (grind, threshold) in constants
            .grinds
            .iter_mut()
            .zip([-0.06, -0.04, -0.03, -0.03, -0.03, -0.025])
        {
            grind.thresholds_futures.fill(threshold);
        }
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
        let mut manager_config = config(1);
        manager_config.starting_balance = 10_000.0;
        manager_config.stake_amount = 1_977.994_266_67;
        manager_config.fee_rate = 0.0005;
        manager_config.fee_open_rate = Some(0.0005);
        manager_config.fee_close_rate = Some(0.0005);
        manager_config.is_futures = true;
        manager_config.leverage = Some(3.0);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![entry, funding, adjustment, force_exit],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(50.0);
        pair.amount_step = Some(0.001);
        pair.price_step = Some(0.1);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("funding-aware tag-121 futures adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[1].tag.as_deref(), Some("g3"));
        assert!(trade.orders[1].is_entry);
    }

    #[test]
    fn nfi_long_btc_derisk_transfers_to_the_legacy_continuation() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("121".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut force_exit = candle(15 * HOUR, 80.0, 80.0);
        force_exit.exit_long = Some(ExitSignal {
            reason: "force_exit".to_owned(),
        });

        let mut constants = nfi_regular_adjustment_constants();
        constants.rebuy_thresholds_spot.fill(-1.0);
        for grind in &mut constants.grinds {
            grind.thresholds_spot.fill(-1.0);
        }
        constants.derisk_threshold_spot = -0.05;
        constants.derisk_level_1_threshold_spot = -1.0;
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager
            .programs
            .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
        enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(
            vec![
                entry,
                candle(13 * HOUR, 90.0, 90.0),
                candle(14 * HOUR, 80.0, 80.0),
                force_exit,
            ],
            BTreeMap::new(),
        );
        pair.minimum_cost = Some(5.0);

        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("reviewed tag-121 legacy continuation");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[1].tag.as_deref(), Some("d"));
        assert!(!trade.orders[1].is_entry);
        // A regular `d` (not `d1`) enables the first ordinary legacy grind.
        // The two `dl*` post-level-1 clusters remain source-ordered but require
        // an actual level-1 de-risk tag.
        assert_eq!(trade.orders[2].tag.as_deref(), Some("gd1"));
        assert!(trade.orders[2].is_entry);
        assert_eq!(trade.exit_reason, "force_exit");
    }

    #[test]
    fn entry_adjustment_stop_and_fees_are_accounted_in_order() {
        let mut entry = candle(1, 100.0, 99.5);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut adjustment = candle(2, 99.5, 99.2);
        adjustment.adjustment = Some(AdjustmentSignal {
            stake_amount: 50.0,
            tag: "rebuy".to_owned(),
        });
        let stop = candle(3, 99.0, 98.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, adjustment, stop].into(),
            }],
        };

        let result = simulate(&input).expect("valid simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.exit_reason, "stop_loss");
        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("rebuy"));
        assert!(trade.profit_abs < 0.0);
        assert!((trade.close_rate - 99.0).abs() < f64::EPSILON);
    }

    #[test]
    fn futures_entry_adjustment_replays_funding_at_the_fill_timestamp() {
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(2.0);
        portfolio.fee_rate = 0.0;
        portfolio.fee_open_rate = Some(0.0);
        portfolio.fee_close_rate = Some(0.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;

        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut adjustment = candle(2, 100.0, 100.0);
        adjustment.funding_rate = Some(0.001);
        adjustment.funding_mark_price = Some(100.0);
        adjustment.adjustment = Some(AdjustmentSignal {
            stake_amount: 100.0,
            tag: "rebuy".to_owned(),
        });
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.01),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid same-timestamp funding adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[0].amount, 2.0);
        assert_eq!(trade.orders[1].amount, 2.0);
        assert_eq!(trade.orders[1].funding_fee, -0.2);
        // Pinned Freqtrade first moves the pre-fill funding to the adjustment
        // order, then force-recalculates the inclusive funding range with the
        // new position amount: -(0.001 * 100 * 2) - (0.001 * 100 * 4).
        assert_eq!(trade.orders[2].funding_fee, -0.4);
        assert_eq!(trade.funding_fees, -0.600_000_000_000_000_1);
        assert_eq!(trade.profit_abs, -0.6);
    }

    #[test]
    fn futures_initial_entry_seeds_funding_at_the_fill_timestamp() {
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(2.0);
        portfolio.fee_rate = 0.0;
        portfolio.fee_open_rate = Some(0.0);
        portfolio.fee_close_rate = Some(0.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;

        let mut entry = candle(1, 100.0, 100.0);
        entry.funding_rate = Some(0.001);
        entry.funding_mark_price = Some(100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.01),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid same-timestamp entry funding");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[0].amount, 2.0);
        assert_eq!(trade.orders[1].funding_fee, -0.2);
        assert_eq!(trade.funding_fees, -0.2);
        assert_eq!(trade.profit_abs, -0.2);
    }

    #[test]
    fn futures_partial_exit_rebases_inclusive_funding_on_the_next_tick() {
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(2.0);
        portfolio.stake_amount = 200.0;
        portfolio.fee_rate = 0.0;
        portfolio.fee_open_rate = Some(0.0);
        portfolio.fee_close_rate = Some(0.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;

        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut partial_exit = candle(2, 100.0, 100.0);
        partial_exit.funding_rate = Some(0.001);
        partial_exit.funding_mark_price = Some(100.0);
        partial_exit.adjustment = Some(AdjustmentSignal {
            stake_amount: -100.0,
            tag: "derisk".to_owned(),
        });
        let mut next_funding = candle(3, 100.0, 100.0);
        next_funding.funding_rate = Some(0.002);
        next_funding.funding_mark_price = Some(100.0);
        let mut exit = candle(4, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.01),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, partial_exit, next_funding, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid partial-exit funding rebase");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[0].amount, 4.0);
        assert_eq!(trade.orders[1].amount, 2.0);
        assert_eq!(trade.orders[1].funding_fee, -0.4);
        // The next segment is recalculated with the reduced amount and remains
        // inclusive of the partial-fill row: -0.2 + -0.4.
        assert_eq!(trade.orders[2].funding_fee, -0.600_000_000_000_000_1);
        assert_eq!(trade.funding_fees, -1.0);
        assert_eq!(trade.profit_abs, -1.0);
    }

    #[test]
    fn futures_partial_exit_rebases_on_a_refresh_without_a_new_funding_event() {
        const HOUR: i64 = 60 * 60 * 1_000;
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.funding_fee_interval_ms = Some(HOUR);
        portfolio.leverage = Some(2.0);
        portfolio.stake_amount = 200.0;
        portfolio.fee_rate = 0.0;
        portfolio.fee_open_rate = Some(0.0);
        portfolio.fee_close_rate = Some(0.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;

        let mut entry = candle(7 * HOUR, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut partial_exit = candle(8 * HOUR, 100.0, 100.0);
        partial_exit.funding_rate = Some(0.001);
        partial_exit.funding_mark_price = Some(100.0);
        partial_exit.adjustment = Some(AdjustmentSignal {
            stake_amount: -100.0,
            tag: "derisk".to_owned(),
        });
        // Freqtrade refreshes its inclusive segment at this scheduled hour,
        // even though the sparse funding dataframe has no new event.
        let refresh = candle(9 * HOUR, 100.0, 100.0);
        let mut exit = candle(10 * HOUR, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.01),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, partial_exit, refresh, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid scheduled funding rebase");
        let trade = &result.trades[0];

        assert_eq!(trade.orders[1].funding_fee, -0.4);
        assert_eq!(trade.orders[2].funding_fee, -0.2);
        assert_eq!(trade.funding_fees, -0.600_000_000_000_000_1);
        assert_eq!(trade.profit_abs, -0.600_000_000_000_000_1);
    }

    #[test]
    fn futures_profit_uses_python_eight_decimal_ties_to_even() {
        let pair_name = "BAND/USDT:USDT";
        let mut portfolio = config(1);
        portfolio.starting_balance = 1_000.0;
        portfolio.stake_amount = 216.343_926_67;
        portfolio.fee_rate = 0.0005;
        portfolio.fee_open_rate = Some(0.0005);
        portfolio.fee_close_rate = Some(0.0005);
        portfolio.leverage = Some(3.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 0.1;
        portfolio.price_step = 0.0001;
        portfolio.is_futures = true;

        let mut entry = candle(1, 1.8094, 1.8033);
        entry.enter_long = Some(EntrySignal {
            tag: Some("104 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 1.8951, 1.89);
        exit.exit_long = Some(ExitSignal {
            reason: "exit_long_rapid_d_4_71 ( 104 )".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: Some(0.1),
                price_step: Some(0.0001),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: Some(5.0),
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid decimal tie boundary trade");
        let trade = &result.trades[0];

        assert_eq!(trade.amount, 358.7);
        assert_eq!(trade.profit_abs, 30.076_187_92);
    }

    #[test]
    fn negative_adjustment_realizes_a_partial_exit() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("entry".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut derisk = candle(2, 110.0, 109.0);
        derisk.adjustment = Some(AdjustmentSignal {
            stake_amount: -40.0,
            tag: "derisk".to_owned(),
        });
        let mut exit = candle(3, 120.0, 119.0);
        exit.exit_long = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, derisk, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid partial exit simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert!(!trade.orders[1].is_entry);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk"));
        assert!(trade.stake_amount < trade.max_stake_amount);
        assert!(trade.profit_abs > 0.0);
    }

    #[test]
    fn explicit_exit_is_filled_at_candle_open() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 105.0, 104.0);
        exit.exit_long = Some(ExitSignal {
            reason: "custom_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid simulation");

        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
        assert_eq!(result.trades[0].exit_reason, "custom_exit");
        assert!(result.final_balance > result.starting_balance);
    }

    #[test]
    fn overlapping_trades_are_exported_in_freqtrade_closure_order() {
        let mut first_entry = candle(1, 100.0, 100.0);
        first_entry.enter_long = Some(EntrySignal {
            tag: Some("first".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut first_exit = candle(4, 103.0, 103.0);
        first_exit.exit_long = Some(ExitSignal {
            reason: "late_exit".to_owned(),
        });

        let mut second_entry = candle(2, 100.0, 100.0);
        second_entry.enter_long = Some(EntrySignal {
            tag: Some("second".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut second_exit = candle(3, 102.0, 102.0);
        second_exit.exit_long = Some(ExitSignal {
            reason: "early_exit".to_owned(),
        });

        let pair = |name: &str, candles: Vec<Candle>| PairSeries {
            pair: name.to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: candles.into(),
        };
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(2),
            pairs: vec![
                pair(
                    "AAA/USDT",
                    vec![
                        first_entry,
                        candle(2, 101.0, 101.0),
                        candle(3, 102.0, 102.0),
                        first_exit,
                    ],
                ),
                pair(
                    "BBB/USDT",
                    vec![
                        candle(1, 100.0, 100.0),
                        second_entry,
                        second_exit,
                        candle(4, 103.0, 103.0),
                    ],
                ),
            ],
        };

        let result = simulate(&input).expect("valid overlapping trade simulation");

        assert_eq!(result.trades.len(), 2);
        assert_eq!(result.trades[0].pair, "BBB/USDT");
        assert_eq!(result.trades[0].sequence, 0);
        assert_eq!(result.trades[1].pair, "AAA/USDT");
        assert_eq!(result.trades[1].sequence, 1);
    }

    #[test]
    fn strategy_exit_precedes_stoploss_on_the_same_freqtrade_candle() {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 105.0, 90.0);
        exit.exit_long = Some(ExitSignal {
            reason: "custom_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: config(1),
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid collision simulation");

        assert_eq!(result.trades[0].exit_reason, "custom_exit");
        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    }

    #[test]
    fn compiled_custom_exit_bundle_runs_inside_the_native_trade_loop() {
        let mut config = config(1);
        config.custom_exit_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "custom_exit",
                "programs": {
                    "custom_exit": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "pair",
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit"
                        ],
                        "expressions": [
                            ["variable", "current_profit"],
                            ["literal", 0.01],
                            ["compare", 0, [["greater", 1]]],
                            ["literal", "native_custom_exit"],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 3]], []],
                            ["return", 4]
                        ]
                    }
                }
            }))
            .expect("valid custom exit bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let exit = candle(2, 105.0, 104.0);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid compiled custom exit");

        assert_eq!(result.trades[0].exit_reason, "native_custom_exit");
        assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    }

    #[test]
    fn compiled_position_adjustment_bundle_adds_a_tagged_entry() {
        let mut portfolio = config(1);
        portfolio.stoploss_ratio = -0.99;
        portfolio.adjust_trade_position_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "adjust_trade_position",
                "programs": {
                    "adjust_trade_position": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit",
                            "min_stake",
                            "max_stake",
                            "current_entry_rate",
                            "current_exit_rate",
                            "current_entry_profit",
                            "current_exit_profit"
                        ],
                        "expressions": [
                            ["variable", "current_profit"],
                            ["literal", -0.01],
                            ["compare", 0, [["less", 1]]],
                            ["literal", 50.0],
                            ["literal", "compiled_rebuy"],
                            ["tuple", [3, 4]],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 5]], []],
                            ["return", 6]
                        ]
                    }
                }
            }))
            .expect("valid adjustment bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment = candle(2, 90.0, 90.0);
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid compiled adjustment");
        let trade = &result.trades[0];

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.orders[1].tag.as_deref(), Some("compiled_rebuy"));
        assert!(trade.orders[1].is_entry);
    }

    #[test]
    fn position_adjustment_receives_tradable_balance_limited_max_stake() {
        let mut portfolio = config(1);
        portfolio.starting_balance = 1_000.0;
        portfolio.stake_amount = 100.0;
        portfolio.tradable_balance_ratio = 0.99;
        portfolio.stoploss_ratio = -0.99;
        portfolio.adjust_trade_position_program = Some(
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.0.0",
                "entry": "adjust_trade_position",
                "programs": {
                    "adjust_trade_position": {
                        "schema_version": "1.1.0",
                        "opcode": "scalar-decision-program-v1",
                        "parameters": [
                            "trade",
                            "current_time",
                            "current_rate",
                            "current_profit",
                            "min_stake",
                            "max_stake",
                            "current_entry_rate",
                            "current_exit_rate",
                            "current_entry_profit",
                            "current_exit_profit"
                        ],
                        "expressions": [
                            ["variable", "max_stake"],
                            ["literal", 895.0],
                            ["compare", 0, [["greater", 1]]],
                            ["literal", 50.0],
                            ["literal", "must_not_run"],
                            ["tuple", [3, 4]],
                            ["literal", null]
                        ],
                        "statements": [
                            ["if", 2, [["return", 5]], []],
                            ["return", 6]
                        ]
                    }
                }
            }))
            .expect("valid adjustment bundle"),
        );
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("test".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let adjustment = candle(2, 99.0, 99.0);
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid tradable-balance adjustment");

        assert_eq!(result.trades[0].orders.len(), 2);
        assert!(result.trades[0]
            .orders
            .iter()
            .all(|order| order.tag.as_deref() != Some("must_not_run")));
    }

    #[test]
    fn leveraged_short_uses_side_specific_orders_and_funding() {
        let mut entry = candle(1, 100.0, 99.0);
        entry.enter_short = Some(EntrySignal {
            tag: Some("short".to_owned()),
            leverage: Some(3.0),
            liquidation_price: Some(130.0),
        });
        let mut exit = candle(2, 90.0, 89.0);
        exit.high = 91.0;
        exit.funding_rate = Some(0.001);
        exit.funding_mark_price = Some(90.0);
        exit.exit_short = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let mut futures_config = config(1);
        futures_config.is_futures = true;
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: futures_config,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid short simulation");
        let trade = &result.trades[0];

        assert!(trade.is_short);
        assert!((trade.leverage - 3.0).abs() < f64::EPSILON);
        assert_eq!(trade.orders[0].side, OrderSide::Sell);
        assert_eq!(trade.orders[1].side, OrderSide::Buy);
        assert!(trade.funding_fees > 0.0);
        assert!(trade.profit_abs > 0.0);
    }

    #[test]
    fn nfi_entry_leverage_preserves_rule_order_and_exchange_cap() {
        let program = NfiLeverageProgram {
            default: 4.0,
            ordered_tag_overrides: vec![
                NfiLeverageOverride {
                    entry_tags: vec!["61".to_owned(), "62".to_owned()],
                    leverage: 3.0,
                },
                NfiLeverageOverride {
                    entry_tags: vec!["120".to_owned(), "121".to_owned()],
                    leverage: 2.0,
                },
            ],
        };
        assert!((evaluate_nfi_leverage(&program, Some("61 62")) - 3.0).abs() < f64::EPSILON);
        assert!((evaluate_nfi_leverage(&program, Some("120")) - 2.0).abs() < f64::EPSILON);
        assert!((evaluate_nfi_leverage(&program, Some("61 120")) - 4.0).abs() < f64::EPSILON);

        let mut portfolio = config(1);
        portfolio.nfi_leverage_program = Some(program);
        portfolio
            .maximum_leverage_by_pair
            .insert("AAA/USDT:USDT".to_owned(), 2.5);
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("61".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid tag-dependent leverage");

        assert!((result.trades[0].leverage - 2.5).abs() < f64::EPSILON);
    }

    #[test]
    fn dynamic_leverage_cap_uses_proposed_stake_tier() {
        let pair_name = "AAA/USDT:USDT";
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(8.0);
        portfolio
            .maximum_leverage_by_pair
            .insert(pair_name.to_owned(), 20.0);
        portfolio.liquidation_model = Some(isolated_model(
            pair_name,
            vec![
                leverage_tier(0.0, Some(500.0), 10.0, 0.005, 0.0),
                leverage_tier(500.0, Some(5_000.0), 5.0, 0.01, 5.0),
            ],
        ));
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid tier-capped leverage");

        assert_eq!(result.trades[0].leverage, 5.0);
    }

    #[test]
    fn futures_partial_exit_keeps_freqtrade_initial_stake_float_boundary() {
        let (amount, stake, _, _) =
            entry_sizing(9_900.0, 20.881, 0.0005, 1.0, 5.0).expect("valid entry sizing");
        assert_eq!(amount, 2_370.0);
        assert_eq!(stake, 9_897.594_000_000_001);

        // This is the arithmetic order used by X7's derisk callback before
        // Freqtrade applies FtPrecise and exchange amount precision.
        let exit_rate = 18.792;
        let sell_amount = amount * 0.2 * exit_rate / 5.0;
        let requested_stake = sell_amount * 5.0 * (stake / amount) / exit_rate;
        let raw_amount =
            precise_product_quotient(requested_stake, amount, stake).expect("valid partial exit");
        assert_eq!(floor_step(raw_amount, 1.0), 474.0);
    }

    #[test]
    fn futures_profit_rounding_matches_python_format_boundary() {
        let open_value = precise_product(&[26_791.1, 0.7768, 1.0005]).expect("valid open value");
        let close_value = precise_product(&[26_791.1, 0.8043, 0.9995]).expect("valid close value");
        let profit = close_value - open_value;
        let amount = exact_rational(26_791.1).expect("valid amount");
        let stake = &amount * exact_rational(0.7768).expect("valid price");
        let average = ft_precise_division(&stake, &amount)
            .and_then(|value| value.to_f64())
            .expect("valid average");

        assert_eq!(open_value, 20_821.732_143_24);
        assert_eq!(close_value, 21_537.307_689_135);
        assert_eq!(profit, 715.575_545_895_000_7);
        assert_eq!(average, 0.7768);
        assert_eq!(round_eight(profit), 715.575_545_9);
    }

    #[test]
    fn variable_leverage_trade_keeps_eight_decimal_profit_boundary() {
        let pair_name = "ALGO/USDT:USDT";
        let mut portfolio = config(1);
        portfolio.starting_balance = 20_000.0;
        portfolio.stake_amount = 10_405.663_24;
        portfolio.unlimited_stake = false;
        portfolio.is_futures = true;
        portfolio.leverage = Some(2.0);
        portfolio.amount_step = 0.1;
        portfolio.price_step = 0.0001;
        portfolio.fee_rate = 0.0005;
        portfolio.fee_open_rate = Some(0.0005);
        portfolio.fee_close_rate = Some(0.0005);

        let mut entry = candle(1, 0.7768, 0.77);
        entry.enter_long = Some(EntrySignal {
            tag: Some("145 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(2, 0.8043, 0.80);
        exit.exit_long = Some(ExitSignal {
            reason: "exit_long_tc_d_3_42 ( 145 )".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: Some(0.1),
                price_step: Some(0.0001),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid variable-leverage trade");
        let trade = &result.trades[0];

        assert_eq!(trade.amount, 26_791.1);
        assert_eq!(trade.profit_abs, 715.575_545_9);
    }

    #[test]
    fn computed_isolated_liquidation_matches_binance_long_and_short_formula() {
        let pair_name = "AAA/USDT:USDT";
        for side in [TradeSide::Long, TradeSide::Short] {
            let mut portfolio = config(1);
            portfolio.is_futures = true;
            portfolio.leverage = Some(3.0);
            portfolio.stoploss_ratio = -0.99;
            portfolio.liquidation_model = Some(isolated_model(
                pair_name,
                vec![leverage_tier(0.0, None, 20.0, 0.005, 0.0)],
            ));
            let mut entry = candle(1, 100.0, 100.0);
            let signal = EntrySignal {
                tag: None,
                leverage: None,
                liquidation_price: None,
            };
            if side == TradeSide::Short {
                entry.enter_short = Some(signal);
            } else {
                entry.enter_long = Some(signal);
            }
            // Keep this a liquidation-only candle. The stop-loss collision
            // ordering is covered separately because Freqtrade returns the
            // stop-loss candidate first when both boundaries are crossed.
            let mut liquidated = candle(2, 100.0, 100.0);
            if side == TradeSide::Short {
                liquidated.high = 132.0;
            } else {
                liquidated.low = 68.0;
            }
            let input = SimulationInput {
                schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
                config: portfolio,
                pairs: vec![PairSeries {
                    pair: pair_name.to_owned(),
                    execution_start_index: 0,
                    amount_step: None,
                    price_step: None,
                    price_steps: Vec::new(),
                    minimum_stake: None,
                    minimum_amount: None,
                    minimum_cost: None,
                    feature_columns: BTreeMap::new(),
                    candles: vec![entry, liquidated].into(),
                }],
            };

            let result = simulate(&input).expect("valid computed liquidation");
            let trade = &result.trades[0];
            let expected = buffered_liquidation_price(
                side,
                trade.stake_amount,
                trade.amount,
                trade.open_rate,
                0.005,
                0.0,
                0.05,
            );

            assert_eq!(trade.exit_reason, "liquidation");
            // The calculated liquidation boundary remains exact on the trade,
            // while the synthetic liquidation order is filled at the price
            // precision frozen when the position opened.
            assert_eq!(trade.close_rate, round_step(expected, 0.01));
            assert_eq!(trade.liquidation_price, Some(expected));
        }
    }

    #[test]
    fn isolated_liquidation_recalculates_after_position_adjustment() {
        let pair_name = "AAA/USDT:USDT";
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(3.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.adjustment_rule = Some(AdjustmentRule {
            profit_below: -0.1,
            stake_ratio: 1.0,
            max_adjustments: 1,
            tag: "rebuy".to_owned(),
        });
        portfolio.liquidation_model = Some(isolated_model(
            pair_name,
            vec![leverage_tier(0.0, None, 20.0, 0.005, 0.0)],
        ));
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let adjustment = candle(2, 80.0, 75.0);
        let mut exit = candle(3, 100.0, 100.0);
        exit.exit_long = Some(ExitSignal {
            reason: "done".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, adjustment, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid adjusted liquidation");
        let trade = &result.trades[0];
        let expected = buffered_liquidation_price(
            TradeSide::Long,
            trade.stake_amount,
            trade.amount,
            trade.open_rate,
            0.005,
            0.0,
            0.05,
        );
        let first_order = &trade.orders[0];
        let initial = buffered_liquidation_price(
            TradeSide::Long,
            first_order.cost / trade.leverage,
            first_order.amount,
            first_order.price,
            0.005,
            0.0,
            0.05,
        );

        assert_eq!(trade.orders.len(), 3);
        assert_eq!(trade.liquidation_price, Some(expected));
        assert!((expected - initial).abs() > 1e-6);
    }

    #[test]
    fn partial_exit_liquidation_refresh_precedes_trade_replay() {
        let pair_name = "APE/USDT:USDT";
        let mut portfolio = config(1);
        portfolio.starting_balance = 20_000.0;
        portfolio.stake_amount = 11_588.348_4;
        portfolio.unlimited_stake = false;
        portfolio.is_futures = true;
        portfolio.leverage = Some(5.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;
        portfolio.price_step = 0.001;
        portfolio.liquidation_model = Some(isolated_model(
            pair_name,
            vec![leverage_tier(0.0, None, 5.0, 0.02, 25.0)],
        ));

        let mut entry = candle(1, 21.799, 21.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some("141 142 ".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut first_derisk = candle(2, 20.044, 19.5);
        first_derisk.adjustment = Some(AdjustmentSignal {
            stake_amount: -2_317.669_68,
            tag: "derisk_level_1".to_owned(),
        });
        let mut second_derisk = candle(3, 18.89, 18.5);
        second_derisk.adjustment = Some(AdjustmentSignal {
            stake_amount: -3_476.504_52,
            tag: "derisk_level_2".to_owned(),
        });
        let liquidation = candle(4, 18.0, 17.95);
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.001),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, first_derisk, second_derisk, liquidation].into(),
            }],
        };

        let result = simulate(&input).expect("valid partial-exit liquidation");
        let trade = &result.trades[0];
        let expected = buffered_liquidation_price(
            TradeSide::Long,
            9_273.294_6,
            2_127.0,
            21.799,
            0.02,
            25.0,
            0.05,
        );

        assert_eq!(trade.exit_reason, "liquidation");
        assert_eq!(trade.orders[1].amount, 531.0);
        assert_eq!(trade.orders[2].amount, 797.0);
        assert_eq!(trade.close_rate, round_step(expected, 0.001));
        assert_eq!(trade.close_rate, 17.984);
    }

    #[test]
    fn ape_short_funding_and_profit_match_freqtrade_2026_5_1() {
        let mut portfolio = config(1);
        portfolio.starting_balance = 10_000.0;
        portfolio.stake_amount = 3_236.574;
        portfolio.fee_rate = 0.0005;
        portfolio.fee_open_rate = Some(0.0005);
        portfolio.fee_close_rate = Some(0.0005);
        portfolio.leverage = Some(3.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.amount_step = 1.0;
        portfolio.price_step = 0.001;
        portfolio.is_futures = true;

        let mut entry = candle(1_654_801_500_000, 5.742, 5.74);
        entry.high = 5.758;
        entry.enter_short = Some(EntrySignal {
            tag: Some("562 ".to_owned()),
            leverage: Some(3.0),
            liquidation_price: None,
        });
        let mut funding = candle(1_654_819_200_000, 5.721, 5.525_74);
        funding.high = 5.738_839;
        funding.funding_rate = Some(0.000_020_67);
        funding.funding_mark_price = Some(5.721);
        let mut exit = candle(1_654_820_400_000, 5.568, 5.5);
        exit.high = 5.58;
        exit.exit_short = Some(ExitSignal {
            reason: "exit_short_rebuy_d_3_100 ( 562 )".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "APE/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: Some(1.0),
                price_step: Some(0.001),
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: Some(5.0),
                feature_columns: BTreeMap::new(),
                candles: vec![entry, funding, exit].into(),
            }],
        };

        let result = simulate(&input).expect("valid APE short simulation");
        let trade = &result.trades[0];

        assert_eq!(trade.amount, 1_691.0);
        assert_eq!(trade.funding_fees, 0.199_965_941_37);
        assert!((trade.profit_abs - 284.871_360_94).abs() < 1e-10);
        assert!((trade.profit_ratio - 0.088_060_358_846_711_66).abs() < 1e-14);
    }

    #[test]
    fn rejected_stop_loss_collision_does_not_fall_through_to_liquidation() {
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.exit_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [{
                    "op": "return",
                    "value": {"op": "literal", "value": false}
                }],
                "functions": {}
            }))
            .expect("valid rejecting confirmation program"),
        );
        let mut entry = candle(1, 100.0, 99.0);
        entry.enter_short = Some(EntrySignal {
            tag: None,
            leverage: Some(5.0),
            liquidation_price: Some(105.0),
        });
        let mut liquidated = candle(2, 104.0, 103.0);
        liquidated.high = 110.0;
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT:USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, liquidated].into(),
            }],
        };

        let result = simulate(&input).expect("valid liquidation simulation");

        assert_eq!(result.trades[0].exit_reason, "force_exit");
        assert!((result.trades[0].close_rate - 104.0).abs() < f64::EPSILON);
    }

    #[test]
    fn trailing_stop_uses_candle_open_after_a_gap_beyond_the_retained_stop() {
        let portfolio = config(1);
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut trigger = candle(2, 99.0, 98.0);
        trigger.high = 100.0;
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, trigger].into(),
        };
        let entry_candle = pair.candles.get(0).expect("entry candle");
        let signal = entry_candle.enter_long.as_ref().expect("long signal");
        let mut trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &portfolio,
        )
        .expect("valid entry")
        .expect("sized entry");
        trade.initial_stop_loss = 90.0;
        trade.stop_loss = 105.0;
        let trigger_candle = pair.candles.get(1).expect("trigger candle");

        let exit = exit_decision(
            &trade,
            &pair,
            1,
            &trigger_candle,
            &portfolio,
            &mut BTreeMap::new(),
        )
        .expect("valid exit evaluation")
        .expect("stop reached");

        assert_eq!(exit.reason, "trailing_stop_loss");
        assert_eq!(exit.rate, 99.0);
    }

    #[test]
    fn cooldown_pair_lock_uses_strict_candle_rounding_and_expiry() {
        let program = ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![ProtectionHandler::CooldownPeriod {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            }],
        };
        let closed = vec![protection_trade(
            1,
            "AAA/USDT",
            300_000,
            -0.01,
            "exit_signal",
            TradeSide::Long,
        )];
        let mut state = ProtectionState::default();

        state.after_trade_close(&program, &closed[0], &closed, 1_000.0);

        assert_eq!(
            state.locks(),
            &[PairLockState {
                pair: "AAA/USDT".to_owned(),
                lock_timestamp_ms: 300_000,
                // Requested end 900_000 is already a boundary. CCXT
                // ROUND_UP advances it to the following 5-minute boundary.
                lock_end_timestamp_ms: 1_200_000,
                reason: "Cooldown period for for 10 minutes.".to_owned(),
                side: "*".to_owned(),
                active: true,
            }]
        );
        assert!(state.is_pair_locked("AAA/USDT", 1_199_999, TradeSide::Long));
        assert!(!state.is_pair_locked("AAA/USDT", 1_200_000, TradeSide::Long));
        assert!(!state.is_pair_locked("BBB/USDT", 600_000, TradeSide::Long));
    }

    #[test]
    fn simulator_skips_locked_entry_without_counting_a_rejection() {
        let mut portfolio = config(1);
        portfolio.stoploss_ratio = -0.99;
        portfolio.protection_program = Some(ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![ProtectionHandler::CooldownPeriod {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            }],
        });
        let mut entry = candle(0, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let mut exit = candle(300_000, 101.0, 101.0);
        exit.exit_long = Some(ExitSignal {
            reason: "exit_signal".to_owned(),
        });
        let mut locked_signal = candle(600_000, 102.0, 102.0);
        locked_signal.enter_long = Some(EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, exit, locked_signal, candle(900_000, 102.0, 102.0)].into(),
            }],
        };

        let result = simulate(&input).expect("valid cooldown simulation");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.rejected_signals, 0);
        assert_eq!(result.locks.len(), 1);
        assert_eq!(result.locks[0].lock_end_timestamp_ms, 1_200_000);
    }

    #[test]
    fn stoploss_guard_global_lock_respects_trade_side() {
        let program = ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![ProtectionHandler::StoplossGuard {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                trade_limit: 2,
                only_per_pair: false,
                only_per_side: true,
                required_profit: 0.0,
            }],
        };
        let closed = vec![
            protection_trade(1, "AAA/USDT", 300_000, -0.1, "stop_loss", TradeSide::Long),
            protection_trade(2, "BBB/USDT", 600_000, -0.1, "liquidation", TradeSide::Long),
        ];
        let mut state = ProtectionState::default();

        state.after_trade_close(&program, &closed[0], &closed[..1], 1_000.0);
        state.after_trade_close(&program, &closed[1], &closed, 1_000.0);

        assert_eq!(state.locks().len(), 1);
        assert_eq!(state.locks()[0].pair, "*");
        assert_eq!(state.locks()[0].side, "long");
        assert!(state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Long));
        assert!(!state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Short));
    }

    #[test]
    fn low_profit_pairs_and_max_drawdown_create_local_and_global_locks() {
        let program = ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![
                ProtectionHandler::LowProfitPairs {
                    timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                    trade_limit: 1,
                    only_per_side: false,
                    required_profit: -0.02,
                },
                ProtectionHandler::MaxDrawdown {
                    timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                    trade_limit: 2,
                    maximum_allowed_drawdown: 0.2,
                    calculation_mode: DrawdownMode::Ratios,
                },
            ],
        };
        let closed = vec![
            protection_trade(1, "AAA/USDT", 300_000, 0.1, "exit_signal", TradeSide::Long),
            protection_trade(2, "AAA/USDT", 600_000, -0.3, "exit_signal", TradeSide::Long),
        ];
        let mut state = ProtectionState::default();

        state.after_trade_close(&program, &closed[0], &closed[..1], 1_000.0);
        state.after_trade_close(&program, &closed[1], &closed, 1_000.0);

        assert_eq!(
            state
                .locks()
                .iter()
                .map(|lock| lock.pair.as_str())
                .collect::<Vec<_>>(),
            vec!["AAA/USDT", "*"]
        );
        assert!(state.locks()[0]
            .reason
            .starts_with("-0.19999999999999998 < -0.02"));
        assert!(state.locks()[1].reason.starts_with("0.3 passed 0.2"));
    }

    #[test]
    fn order_filled_program_updates_compiled_trade_custom_state() {
        let mut config = config(1);
        config.callback_program = Some(CallbackProgram {
            order_filled: Some(OrderFilledProgram {
                initial_successful_entry_writes: vec![CustomDataWrite {
                    key: "system_version".to_owned(),
                    value: Value::String("system_v3_2".to_owned()),
                }],
                order_tag_actions: BTreeMap::from([(
                    "grind_1_exit".to_owned(),
                    vec![
                        CustomDataWrite {
                            key: "grind_1_cluster_max_profit_stake".to_owned(),
                            value: serde_json::json!(0.0),
                        },
                        CustomDataWrite {
                            key: "grind_1_cluster_max_profit_rate".to_owned(),
                            value: serde_json::json!(0.0),
                        },
                    ],
                )]),
            }),
        });
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 99.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("grind_1_exit detail".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");

        let trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");

        assert_eq!(
            trade.custom_data.get("system_version"),
            Some(&Value::String("system_v3_2".to_owned()))
        );
        assert_eq!(
            trade.custom_data.get("grind_1_cluster_max_profit_stake"),
            Some(&serde_json::json!(0.0))
        );
    }

    #[test]
    fn bounded_stake_vm_applies_tag_rule_and_exchange_minimum() {
        let program: StakeProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "enter_tags",
                    "value": {
                        "op": "split_words",
                        "value": {"op": "variable", "name": "entry_tag"}
                    }
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "all_in",
                        "items": {"op": "variable", "name": "enter_tags"},
                        "container": {"op": "literal", "value": ["61", "62"]}
                    },
                    "then": [{
                        "op": "return",
                        "value": {
                            "op": "stake_clamp_min",
                            "multiplier": {"op": "literal", "value": 0.25}
                        }
                    }],
                    "otherwise": []
                },
                {
                    "op": "return",
                    "value": {"op": "variable", "name": "proposed_stake"}
                }
            ]
        }))
        .expect("valid stake program");

        let stake = evaluate_stake_program(
            &program,
            &StakeInputs {
                proposed_stake: 100.0,
                minimum_stake: 30.0,
                maximum_stake: 1_000.0,
                current_rate: 100.0,
                leverage: 1.0,
                entry_tag: Some("61"),
                side: TradeSide::Long,
            },
        )
        .expect("stake result");

        assert!((stake - 30.0).abs() < f64::EPSILON);
    }

    #[test]
    fn entry_confirmation_vm_evaluates_tag_and_slippage_gates() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "entry_tags",
                    "value": {
                        "op": "split_words",
                        "value": {"op": "variable", "name": "entry_tag"}
                    }
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "all_in",
                        "items": {"op": "variable", "name": "entry_tags"},
                        "container": {"op": "literal", "value": ["120"]}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "if",
                    "condition": {
                        "op": "greater",
                        "left": {"op": "variable", "name": "rate"},
                        "right": {"op": "literal", "value": 102.0}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "return",
                    "value": {"op": "literal", "value": true}
                }
            ],
            "functions": {}
        }))
        .expect("valid confirmation program");
        let open_trades = Vec::new();
        let base = ConfirmInputs {
            pair: "BTC/USDT",
            timestamp_ms: 1,
            amount: 0.99,
            rate: 101.0,
            entry_tag: Some("61"),
            side: TradeSide::Long,
            previous_close: Some(100.0),
            open_trades: &open_trades,
            max_open_trades: 6,
            is_futures: false,
        };

        assert_eq!(evaluate_confirm_program(&program, base), Some(true));
        assert_eq!(
            evaluate_confirm_program(
                &program,
                ConfirmInputs {
                    entry_tag: Some("120"),
                    ..base
                },
            ),
            Some(false)
        );
        assert_eq!(
            evaluate_confirm_program(
                &program,
                ConfirmInputs {
                    rate: 103.0,
                    ..base
                },
            ),
            Some(false)
        );
    }

    #[test]
    fn entry_confirmation_vm_accepts_a_computed_negative_dataframe_index() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "let",
                    "name": "df",
                    "value": {"op": "analyzed_frame"}
                },
                {
                    "op": "let",
                    "name": "last_candle",
                    "value": {
                        "op": "index",
                        "value": {"op": "variable", "name": "df"},
                        "index": {
                            "op": "negative",
                            "value": {"op": "literal", "value": 1}
                        }
                    }
                },
                {
                    "op": "return",
                    "value": {
                        "op": "less",
                        "left": {
                            "op": "field",
                            "value": {"op": "variable", "name": "last_candle"},
                            "name": "close"
                        },
                        "right": {"op": "variable", "name": "rate"}
                    }
                }
            ],
            "functions": {}
        }))
        .expect("valid analyzed-frame confirmation program");
        let open_trades = Vec::new();
        let inputs = ConfirmInputs {
            pair: "APE/USDT",
            timestamp_ms: 1,
            amount: 1.0,
            rate: 101.0,
            entry_tag: Some("62"),
            side: TradeSide::Long,
            previous_close: Some(100.0),
            open_trades: &open_trades,
            max_open_trades: 6,
            is_futures: false,
        };

        assert_eq!(evaluate_confirm_program(&program, inputs), Some(true));
    }

    #[test]
    fn exit_confirmation_vm_rejects_spot_stop_and_emits_clear_effect() {
        let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [
                {
                    "op": "if",
                    "condition": {
                        "op": "contains",
                        "container": {
                            "op": "literal",
                            "value": ["stop_loss", "trailing_stop_loss"]
                        },
                        "value": {"op": "variable", "name": "exit_reason"}
                    },
                    "then": [{
                        "op": "return",
                        "value": {"op": "literal", "value": false}
                    }],
                    "otherwise": []
                },
                {
                    "op": "clear_profit_target",
                    "pair": {"op": "variable", "name": "pair"}
                },
                {
                    "op": "return",
                    "value": {"op": "literal", "value": true}
                }
            ],
            "functions": {}
        }))
        .expect("valid exit confirmation program");
        let config = config(1);
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 99.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("61".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");
        let trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");

        assert_eq!(
            evaluate_exit_confirm_program(&program, &trade, 1, 99.0, "stop_loss", &config),
            Some((false, false))
        );
        assert_eq!(
            evaluate_exit_confirm_program(&program, &trade, 2, 101.0, "custom_exit", &config),
            Some((true, true))
        );

        let mode_program: ConfirmProgram = serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {"op": "config_value", "name": "is_futures"}
            }],
            "functions": {}
        }))
        .expect("valid runtime-mode confirmation program");
        assert_eq!(
            evaluate_exit_confirm_program(&mode_program, &trade, 3, 101.0, "custom_exit", &config,),
            Some((false, false))
        );
        let mut futures = config;
        futures.is_futures = true;
        assert_eq!(
            evaluate_exit_confirm_program(&mode_program, &trade, 4, 101.0, "custom_exit", &futures,),
            Some((true, false))
        );
    }

    #[test]
    fn scalar_decision_vm_evaluates_chained_comparison_and_formatted_reason() {
        let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["mode", "current_profit", "last_candle"],
            "expressions": [
                ["literal", "RSI_14"],
                ["variable", "last_candle"],
                ["index", 1, 0],
                ["variable", "current_profit"],
                ["literal", 0.01],
                ["literal", 0.001],
                ["compare", 4, [["greater", 3], ["greater-equal", 5]]],
                ["is-instance", 2, "np.float64"],
                ["literal", 80.0],
                ["compare", 2, [["greater", 8]]],
                ["and", [6, 7, 9]],
                ["literal", true],
                ["variable", "mode"],
                ["format", [["text", "exit_"], ["value", 12], ["text", "_0_1"]]],
                ["tuple", [11, 13]],
                ["literal", false],
                ["literal", null],
                ["tuple", [15, 16]]
            ],
            "statements": [
                ["set", "last_rsi", 2],
                ["if", 10, [["return", 14]], []],
                ["return", 17]
            ]
        }))
        .expect("valid scalar decision program");
        let inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.005)),
            (
                "last_candle".to_owned(),
                serde_json::json!({"RSI_14": 85.0}),
            ),
        ]);

        assert_eq!(
            evaluate_scalar_decision_program(&program, &inputs),
            Some(serde_json::json!([true, "exit_normal_0_1"]))
        );
        let nan_inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.005)),
            (
                "last_candle".to_owned(),
                serde_json::json!({"RSI_14": {"$float": "nan"}}),
            ),
        ]);
        assert_eq!(
            evaluate_scalar_decision_program(&program, &nan_inputs),
            Some(serde_json::json!([false, null]))
        );
    }

    #[test]
    fn scalar_program_overlays_do_not_leak_writes_between_callbacks() {
        let writer: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["value"],
            "expressions": [
                ["literal", 2],
                ["variable", "value"]
            ],
            "statements": [
                ["set", "value", 0],
                ["return", 1]
            ]
        }))
        .expect("valid scalar writer");
        let reader: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["value"],
            "expressions": [["variable", "value"]],
            "statements": [["return", 0]]
        }))
        .expect("valid scalar reader");
        let programs =
            BTreeMap::from([("writer".to_owned(), writer), ("reader".to_owned(), reader)]);
        let base = BTreeMap::from([("value".to_owned(), serde_json::json!(1))]);

        assert_eq!(
            evaluate_scalar_program_bundle_from_base(&programs, "writer", &base),
            Some(serde_json::json!(2))
        );
        assert_eq!(
            evaluate_scalar_program_bundle_from_base(&programs, "reader", &base),
            Some(serde_json::json!(1))
        );
    }

    #[test]
    fn scalar_decision_vm_resolves_transitive_program_calls_fail_closed() {
        let entry_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.1.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["mode", "current_profit"],
            "expressions": [
                ["variable", "mode"],
                ["variable", "current_profit"],
                ["call-program", "decide", [0, 1]]
            ],
            "statements": [["return", 2]]
        }))
        .expect("valid caller");
        let decision_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["mode", "current_profit"],
        "expressions": [
            ["variable", "current_profit"],
            ["literal", 0.1],
            ["compare", 0, [["greater", 1]]],
            ["literal", true],
            ["variable", "mode"],
            ["format", [["text", "exit_"], ["value", 4]]],
            ["tuple", [3, 5]],
            ["literal", false],
            ["literal", null],
            ["tuple", [7, 8]]
        ],
        "statements": [
            ["if", 2, [["return", 6]], []],
            ["return", 9]
        ]
        }))
        .expect("valid decision program");
        let programs = BTreeMap::from([
            ("custom_exit".to_owned(), entry_program.clone()),
            ("decide".to_owned(), decision_program),
        ]);
        let inputs = BTreeMap::from([
            ("mode".to_owned(), Value::String("normal".to_owned())),
            ("current_profit".to_owned(), serde_json::json!(0.2)),
        ]);

        assert_eq!(
            evaluate_scalar_program_bundle(&programs, "custom_exit", &inputs),
            Some(serde_json::json!([true, "exit_normal"]))
        );
        assert_eq!(
            evaluate_scalar_decision_program(&entry_program, &inputs),
            None
        );
        assert_eq!(
            evaluate_scalar_program_bundle(&programs, "missing", &BTreeMap::new()),
            None
        );
    }

    #[test]
    fn scalar_decision_vm_preserves_first_match_for_flat_elif_chains() {
        let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["score"],
            "expressions": [
                ["variable", "score"],
                ["literal", 1.0],
                ["compare", 0, [["less", 1]]],
                ["literal", "first"],
                ["literal", 3.0],
                ["compare", 0, [["less", 4]]],
                ["literal", "second"],
                ["literal", "fallback"]
            ],
            "statements": [
                ["if-chain", [
                    [2, [["return", 3]]],
                    [5, [["return", 6]]]
                ], [["return", 7]]]
            ]
        }))
        .expect("valid flat elif program");

        assert_eq!(
            evaluate_scalar_decision_program(
                &program,
                &BTreeMap::from([("score".to_owned(), serde_json::json!(2.0))]),
            ),
            Some(Value::String("second".to_owned()))
        );
    }

    #[test]
    fn callback_feature_index_selects_the_last_closed_analyzed_row() {
        assert_eq!(callback_feature_index(0), None);
        assert_eq!(callback_feature_index(1), Some(0));
        assert_eq!(callback_feature_index(42), Some(41));
    }

    #[test]
    fn exchange_step_quantization_uses_decimal_ticks() {
        assert_eq!(floor_step(8.45, 0.01), 8.45);
        assert_eq!(floor_step(0.459_999_999_999_999_1, 0.01), 0.45);
        assert_eq!(ceil_step(0.044_361, 0.0001), 0.0444);
        assert_eq!(round_step(20.562_49, 0.0001), 20.5625);
    }

    #[test]
    fn pair_price_step_selects_the_latest_historical_change() {
        let mut pair = nfi_pair(vec![candle(10, 5.0, 4.0)], BTreeMap::new());
        pair.price_step = Some(0.0001);
        pair.price_steps = vec![
            PriceStepChange {
                timestamp_ms: 1,
                step: 0.0001,
            },
            PriceStepChange {
                timestamp_ms: 9,
                step: 0.001,
            },
        ];

        assert_eq!(
            pair_price_step(&pair, &pair.candles.get(0).expect("fixture candle"), 0.01),
            0.001
        );
        assert_eq!(pair_price_step(&pair, &candle(5, 5.0, 4.0), 0.01), 0.0001);
    }

    #[test]
    fn columnar_features_reconstruct_the_exact_selected_and_previous_rows() {
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::from([
                (
                    "RSI_14".to_owned(),
                    FeatureColumn::numbers(vec![41.0, f64::NAN]),
                ),
                (
                    "protections_long_global".to_owned(),
                    FeatureColumn::booleans(vec![false, true]),
                ),
            ]),
            candles: vec![candle(1, 100.0, 100.0), candle(2, 101.0, 101.0)].into(),
        };
        let mut variables = BTreeMap::new();

        insert_feature_window(&mut variables, &pair, 1).expect("aligned feature window");

        assert_eq!(
            variables["last_candle"],
            serde_json::json!({
                "open": 101.0,
                "high": 111.0,
                "low": 101.0,
                "close": 101.0,
                "volume": 1.0,
                "RSI_14": {"$float": "nan"},
                "protections_long_global": true
            })
        );
        assert_eq!(variables["previous_candle"]["RSI_14"], 41.0);
        assert_eq!(variables["previous_candle_1"], variables["previous_candle"]);
        assert_eq!(variables["previous_candle_2"], Value::Null);
    }

    #[test]
    fn nfi_profit_snapshot_uses_filled_order_cashflows_and_first_entry_basis() {
        let config = config(1);
        let pair = PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 100.0)].into(),
        };
        let signal = EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        };
        let entry_candle = pair.candles.get(0).expect("fixture candle");
        let mut trade = enter_trade(
            EntryRequest {
                pair_index: 0,
                pair: &pair,
                candle: &entry_candle,
                side: TradeSide::Long,
                signal: &signal,
                stake: EntryStake {
                    proposed: 100.0,
                    maximum: 1_000.0,
                },
                open_trades: &[],
                id: 1,
                order_id: 1,
            },
            &config,
        )
        .expect("valid entry")
        .expect("sized entry");
        let first = trade.orders[0].clone();
        let exit_amount = first.amount * 0.25;
        trade.orders.push(FilledOrder {
            id: 2,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Sell,
            is_entry: false,
            filled_timestamp_ms: 2,
            amount: exit_amount,
            price: 110.0,
            cost: exit_amount * 110.0,
            tag: Some("d1".to_owned()),
        });

        let snapshot =
            nfi_profit_snapshot(&trade, 105.0, fee_open(&config), fee_close(&config), false)
                .expect("open amount remains");
        let entry_stake = first.amount * first.price * (1.0 + fee_open(&config));
        let exit_stake = exit_amount * 110.0 * (1.0 - fee_close(&config));
        let current_stake = (first.amount - exit_amount) * 105.0 * (1.0 - fee_close(&config));
        let expected = -entry_stake + exit_stake + current_stake;

        assert!((snapshot.stake - expected).abs() < 1e-12);
        assert!((snapshot.ratio - expected / entry_stake).abs() < 1e-12);
        assert!((snapshot.current_stake_ratio - expected / current_stake).abs() < 1e-12);
        assert!(
            (snapshot.initial_stake_ratio - expected / (first.amount * first.price)).abs() < 1e-12
        );
    }

    #[test]
    fn entry_confirmation_receives_post_precision_amount() {
        let mut config = config(1);
        config.entry_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [{
                    "op": "return",
                    "value": {
                        "op": "greater",
                        "left": {"op": "variable", "name": "amount"},
                        "right": {"op": "literal", "value": 0.9}
                    }
                }],
                "functions": {}
            }))
            .expect("valid confirmation program"),
        );
        let mut first = candle(1, 100.0, 100.0);
        first.enter_long = Some(EntrySignal {
            tag: Some("141".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut second = candle(2, 101.0, 101.0);
        second.exit_long = Some(ExitSignal {
            reason: "signal_exit".to_owned(),
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![first, second].into(),
            }],
        };

        let result = simulate(&input).expect("simulation succeeds");

        assert_eq!(result.trades.len(), 1);
    }

    #[test]
    fn rejected_entry_confirmation_consumes_order_id_but_not_rejected_signal_count() {
        let mut config = config(1);
        config.entry_confirmation_program = Some(
            serde_json::from_value(serde_json::json!({
                "statements": [{
                    "op": "return",
                    "value": {
                        "op": "greater",
                        "left": {"op": "variable", "name": "amount"},
                        "right": {"op": "literal", "value": 0.9}
                    }
                }],
                "functions": {}
            }))
            .expect("valid confirmation program"),
        );
        let mut rejected = candle(1, 200.0, 200.0);
        rejected.enter_long = Some(EntrySignal {
            tag: Some("rejected".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut accepted = candle(2, 100.0, 100.0);
        accepted.enter_long = Some(EntrySignal {
            tag: Some("accepted".to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
            pairs: vec![PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![rejected, accepted, candle(3, 100.0, 100.0)].into(),
            }],
        };

        let result = simulate(&input).expect("simulation succeeds");

        assert_eq!(result.trades.len(), 1);
        assert_eq!(result.trades[0].orders[0].id, 2);
        assert_eq!(result.rejected_signals, 0);
    }
}
