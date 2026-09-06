//! Exit arbitration, stop handling, trade close, and closed-profit replay.

use std::collections::BTreeMap;

use crate::calculations::{
    ceil_step, checked_float_product, exit_order_side, fee_close, fee_open, floor_step,
    precise_product, precise_sum, round_eight, round_step,
};
use crate::callbacks::evaluate_custom_exit_bundle;
use crate::domain::{
    AdjustmentSignal, CallbackInvocation, CallbackOutcome, CallbackPhase, CallbackReturnClass,
    Candle, ClosedTrade, ExecutableCallbackError, FilledOrder, OrderType, PairSeries,
    PortfolioConfig, SimError,
};
use crate::futures::{recalculate_order_funding_total, take_running_funding};
use crate::nfi::{evaluate_nfi_exit, CustomExitDecision, ProfitTarget};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scheduler::callback_feature_index;

use super::callback_trace::{record_trade_current as trace_trade_callback, ExecutableCallbacks};
use super::entry::executable_order_filled;
use super::position::{
    freqtrade_total_entry_value, is_unleveraged_spot, replay_leveraged_profit, replay_spot_profit,
};
use super::state_machine::evaluate_state_machine_exit;

pub(crate) fn rule_adjustment(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<AdjustmentSignal> {
    let rule = config.adjustment_rule.as_ref()?;
    if trade.adjustment_count >= rule.max_adjustments {
        return None;
    }
    let current_profit = current_profit_ratio(trade, candle.open, fee_close(config));
    (current_profit < rule.profit_below).then(|| AdjustmentSignal {
        stake_amount: trade.first_entry_cost_with_fees * rule.stake_ratio,
        tag: rule.tag.clone(),
    })
}

pub(crate) struct ExitDecision {
    pub(crate) rate: f64,
    pub(crate) reason: String,
    pub(crate) requires_confirmation: bool,
}

fn executable_trade_invocation(
    callback: &str,
    trade: &OpenTrade,
    candle: &Candle,
    inputs: BTreeMap<String, serde_json::Value>,
) -> CallbackInvocation {
    let mut invocation = CallbackInvocation::new(callback, candle.timestamp_ms, inputs);
    invocation.trade = BTreeMap::from([
        ("id".to_owned(), serde_json::Value::from(trade.id)),
        ("amount".to_owned(), serde_json::Value::from(trade.amount)),
        (
            "stake_amount".to_owned(),
            serde_json::Value::from(trade.stake_amount),
        ),
        (
            "open_rate".to_owned(),
            serde_json::Value::from(trade.open_rate),
        ),
        (
            "stop_loss".to_owned(),
            serde_json::Value::from(trade.stop_loss),
        ),
        (
            "leverage".to_owned(),
            serde_json::Value::from(trade.leverage),
        ),
        (
            "order_count".to_owned(),
            serde_json::Value::from(trade.orders.len()),
        ),
        (
            "nr_of_successful_entries".to_owned(),
            serde_json::Value::from(trade.orders.iter().filter(|order| order.is_entry).count()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            serde_json::Value::from(trade.orders.iter().filter(|order| !order.is_entry).count()),
        ),
        (
            "orders".to_owned(),
            serde_json::Value::Array(
                trade
                    .orders
                    .iter()
                    .map(|order| serde_json::Value::from(order.id))
                    .collect(),
            ),
        ),
        (
            "entry_tag".to_owned(),
            trade
                .entry_tag
                .as_ref()
                .map_or(serde_json::Value::Null, |tag| {
                    serde_json::Value::String(tag.clone())
                }),
        ),
    ]);
    invocation
}

pub(crate) fn executable_custom_stoploss(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    trade: &mut OpenTrade,
    candle: &Candle,
    current_profit: f64,
    after_fill: bool,
) -> Result<Option<f64>, ExecutableCallbackError> {
    let current_rate = match trade.side {
        TradeSide::Long => candle.high,
        TradeSide::Short => candle.low,
    };
    let inputs = BTreeMap::from([
        (
            "pair".to_owned(),
            serde_json::Value::String(trade.pair.clone()),
        ),
        (
            "current_time".to_owned(),
            serde_json::Value::from(candle.timestamp_ms),
        ),
        (
            "current_rate".to_owned(),
            serde_json::Value::from(current_rate),
        ),
        (
            "current_profit".to_owned(),
            serde_json::Value::from(current_profit),
        ),
        ("after_fill".to_owned(), serde_json::Value::from(after_fill)),
    ]);
    let invocation = executable_trade_invocation("custom_stoploss", trade, candle, inputs);
    let event = callbacks.invoke(&invocation, &mut trade.custom_data)?;
    Ok(event
        .return_value
        .as_ref()
        .and_then(serde_json::Value::as_f64))
}

pub(crate) fn executable_custom_exit(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    trade: &mut OpenTrade,
    candle: &Candle,
    current_profit: f64,
) -> Result<Option<String>, ExecutableCallbackError> {
    let inputs = BTreeMap::from([
        (
            "pair".to_owned(),
            serde_json::Value::String(trade.pair.clone()),
        ),
        (
            "current_time".to_owned(),
            serde_json::Value::from(candle.timestamp_ms),
        ),
        (
            "current_rate".to_owned(),
            serde_json::Value::from(candle.open),
        ),
        (
            "current_profit".to_owned(),
            serde_json::Value::from(current_profit),
        ),
    ]);
    let invocation = executable_trade_invocation("custom_exit", trade, candle, inputs);
    let event = callbacks.invoke(&invocation, &mut trade.custom_data)?;
    if event.return_class == CallbackReturnClass::Boolean {
        return Ok((event
            .return_value
            .as_ref()
            .and_then(serde_json::Value::as_bool)
            == Some(true))
        .then(|| "custom_exit".to_owned()));
    }
    Ok(event
        .return_value
        .as_ref()
        .and_then(serde_json::Value::as_str)
        .filter(|reason| !reason.is_empty())
        .map(ToOwned::to_owned))
}

pub(crate) fn executable_exit_confirmation(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    trade: &mut OpenTrade,
    candle: &Candle,
    decision: &ExitDecision,
    is_futures: bool,
    order_type: crate::OrderType,
) -> Result<bool, ExecutableCallbackError> {
    let inputs = BTreeMap::from([
        (
            "pair".to_owned(),
            serde_json::Value::String(trade.pair.clone()),
        ),
        (
            "current_time".to_owned(),
            serde_json::Value::from(candle.timestamp_ms),
        ),
        ("amount".to_owned(), serde_json::Value::from(trade.amount)),
        ("rate".to_owned(), serde_json::Value::from(decision.rate)),
        (
            "order_type".to_owned(),
            serde_json::Value::String(order_type.as_str().to_owned()),
        ),
        (
            "exit_reason".to_owned(),
            serde_json::Value::String(decision.reason.clone()),
        ),
        (
            "config.trading_mode".to_owned(),
            serde_json::Value::String(if is_futures { "futures" } else { "spot" }.to_owned()),
        ),
    ]);
    let invocation = executable_trade_invocation("confirm_trade_exit", trade, candle, inputs);
    let event = callbacks.invoke(&invocation, &mut trade.custom_data)?;
    Ok(event
        .return_value
        .as_ref()
        .and_then(serde_json::Value::as_bool)
        != Some(false))
}

#[allow(clippy::too_many_lines)]
pub(crate) fn exit_decisions(
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Result<Vec<ExitDecision>, SimError> {
    // This order mirrors Freqtrade 2026.5.1 `IStrategy.should_exit`.
    // Strategy exits precede liquidation and stop-loss candidates, so a
    // same-candle collision keeps the strategy reason and candle-open rate.
    let signal = match trade.side {
        TradeSide::Long => &candle.exit_long,
        TradeSide::Short => &candle.exit_short,
    };
    if let Some(signal) = signal {
        return ordered_risk_candidates(
            trade,
            candle,
            config,
            Some(ExitDecision {
                rate: strategy_exit_rate(trade, candle, config),
                reason: signal.reason.clone(),
                requires_confirmation: true,
            }),
        );
    }
    if let Some(program) = &config.state_machine_program {
        if program.entrypoints.contains_key("custom_exit") {
            let feature_index =
                callback_feature_index(candle_index).ok_or(SimError::InvalidStateMachineProgram)?;
            let before = trade.clone();
            if let Ok(reason) =
                evaluate_state_machine_exit(program, trade, pair, feature_index, candle, config)
            {
                trace_trade_callback(
                    CallbackPhase::CustomExit,
                    reason
                        .as_ref()
                        .map_or(CallbackOutcome::None, |_| CallbackOutcome::Value),
                    trade,
                )?;
                if let Some(reason) = reason {
                    return ordered_risk_candidates(
                        trade,
                        candle,
                        config,
                        Some(ExitDecision {
                            rate: strategy_exit_rate(trade, candle, config),
                            reason,
                            requires_confirmation: true,
                        }),
                    );
                }
            } else {
                *trade = before;
                super::callback_trace::record_trade_transaction_current(
                    CallbackPhase::CustomExit,
                    CallbackOutcome::Exception,
                    crate::CallbackTransaction::RolledBack,
                    trade,
                    Some("custom_exit callback failed".to_owned()),
                )?;
            }
        }
    }
    if let Some(manager) = &config.nfi_x7_trade_manager {
        let runtime_error = |diagnostic: String| SimError::InvalidNfiExitRuntime {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
            diagnostic,
        };
        let feature_index = callback_feature_index(candle_index)
            .ok_or_else(|| runtime_error("callback feature index is unavailable".to_owned()))?;
        let decision = evaluate_nfi_exit(
            manager,
            trade,
            pair,
            feature_index,
            candle,
            config,
            profit_targets,
        )
        .map_err(|diagnostic| runtime_error(diagnostic.to_string()))?;
        trace_trade_callback(
            CallbackPhase::CustomExit,
            if matches!(decision, CustomExitDecision::Exit(_)) {
                CallbackOutcome::Value
            } else {
                CallbackOutcome::None
            },
            trade,
        )?;
        if let CustomExitDecision::Exit(reason) = decision {
            return ordered_risk_candidates(
                trade,
                candle,
                config,
                Some(ExitDecision {
                    rate: strategy_exit_rate(trade, candle, config),
                    reason,
                    requires_confirmation: true,
                }),
            );
        }
    }
    if let Some(bundle) = &config.custom_exit_program {
        let feature_index =
            callback_feature_index(candle_index).ok_or_else(|| SimError::InvalidCustomExit {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?;
        let decision =
            evaluate_custom_exit_bundle(bundle, trade, pair, feature_index, candle, config)
                .ok_or_else(|| SimError::InvalidCustomExit {
                    pair: trade.pair.clone(),
                    timestamp_ms: candle.timestamp_ms,
                })?;
        trace_trade_callback(
            CallbackPhase::CustomExit,
            if matches!(decision, CustomExitDecision::Exit(_)) {
                CallbackOutcome::Value
            } else {
                CallbackOutcome::None
            },
            trade,
        )?;
        if let CustomExitDecision::Exit(reason) = decision {
            return ordered_risk_candidates(
                trade,
                candle,
                config,
                Some(ExitDecision {
                    rate: strategy_exit_rate(trade, candle, config),
                    reason,
                    requires_confirmation: true,
                }),
            );
        }
    }
    if config
        .custom_exit_after_ms
        .is_some_and(|duration| candle.timestamp_ms - trade.open_timestamp_ms >= duration)
    {
        return ordered_risk_candidates(trade, candle, config, None);
    }
    ordered_risk_candidates(trade, candle, config, None)
}

fn strategy_exit_rate(trade: &OpenTrade, candle: &Candle, config: &PortfolioConfig) -> f64 {
    if config.exit_order_type == OrderType::Market {
        return candle.open;
    }
    config
        .exit_rates_by_pair
        .get(&trade.pair)
        .and_then(|rates| rates.get(&candle.timestamp_ms))
        .copied()
        .unwrap_or(candle.open)
}

pub(crate) fn ordered_risk_candidates(
    trade: &mut OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
    strategy: Option<ExitDecision>,
) -> Result<Vec<ExitDecision>, SimError> {
    update_trailing_stop(trade, candle, config);
    let mut candidates = strategy.into_iter().collect::<Vec<_>>();
    // Freqtrade calculates stop-loss and liquidation collisions inside
    // `IStrategy.ft_stoploss_reached()`. A regular stop-loss wins that
    // collision and is the only candidate returned to the backtester. This is
    // observable when `confirm_trade_exit()` rejects the stop-loss: Freqtrade
    // does not then fall through to a same-candle liquidation candidate.
    let stopped = match trade.side {
        TradeSide::Long => candle.low <= trade.stop_loss,
        TradeSide::Short => candle.high >= trade.stop_loss,
    };
    if stopped {
        let trailing = match trade.side {
            TradeSide::Long => trade.stop_loss > trade.initial_stop_loss,
            TradeSide::Short => trade.stop_loss < trade.initial_stop_loss,
        };
        if !trailing {
            candidates.push(ExitDecision {
                rate: stop_or_liquidation_exit_rate(trade, candle, trade.stop_loss),
                reason: "stop_loss".to_owned(),
                requires_confirmation: true,
            });
        }
    }
    if !stopped {
        if let Some(liquidation_price) = trade.liquidation_price {
            if liquidation_reached(trade, candle) {
                candidates.push(ExitDecision {
                    rate: stop_or_liquidation_exit_rate(trade, candle, liquidation_price),
                    reason: "liquidation".to_owned(),
                    requires_confirmation: false,
                });
            }
        }
    }
    if let Some(roi) = roi_candidate(trade, candle, config)? {
        candidates.push(roi);
    }
    if config
        .custom_exit_after_ms
        .is_some_and(|duration| candle.timestamp_ms - trade.open_timestamp_ms >= duration)
    {
        candidates.push(ExitDecision {
            rate: candle.open,
            reason: "contract_timed_exit".to_owned(),
            requires_confirmation: true,
        });
    }
    let trailing = stopped
        && match trade.side {
            TradeSide::Long => trade.stop_loss > trade.initial_stop_loss,
            TradeSide::Short => trade.stop_loss < trade.initial_stop_loss,
        };
    if trailing {
        candidates.push(ExitDecision {
            rate: stop_or_liquidation_exit_rate(trade, candle, trade.stop_loss),
            reason: "trailing_stop_loss".to_owned(),
            requires_confirmation: true,
        });
    }
    Ok(candidates)
}

pub(crate) fn liquidation_reached(trade: &OpenTrade, candle: &Candle) -> bool {
    trade.liquidation_price.is_some_and(|liquidation_price| {
        // Freqtrade guards this comparison with Python float truthiness.
        liquidation_price != 0.0
            && match trade.side {
                TradeSide::Long => candle.low <= liquidation_price,
                TradeSide::Short => candle.high >= liquidation_price,
            }
    })
}
fn update_trailing_stop(trade: &mut OpenTrade, candle: &Candle, config: &PortfolioConfig) {
    if !config.trailing_stop {
        return;
    }
    let initial_stop_reached = match trade.side {
        TradeSide::Long => candle.low <= trade.stop_loss,
        TradeSide::Short => candle.high >= trade.stop_loss,
    };
    if initial_stop_reached {
        return;
    }
    let positive = config
        .trailing_stop_positive
        .unwrap_or(config.stoploss_ratio.abs());
    let offset = config.trailing_stop_positive_offset.unwrap_or(0.0);
    let offset_reached = match trade.side {
        TradeSide::Long => candle.high >= trade.open_rate * (1.0 + offset),
        TradeSide::Short => candle.low <= trade.open_rate * (1.0 - offset),
    };
    if config.trailing_only_offset_is_reached && !offset_reached {
        return;
    }
    let adjusted = match trade.side {
        TradeSide::Long => ceil_step(candle.high * (1.0 - positive), trade.price_step),
        TradeSide::Short => floor_step(candle.low * (1.0 + positive), trade.price_step),
    };
    if let Ok(adjusted) = adjusted {
        match trade.side {
            TradeSide::Long if adjusted > trade.stop_loss => trade.stop_loss = adjusted,
            TradeSide::Short if adjusted < trade.stop_loss => trade.stop_loss = adjusted,
            TradeSide::Long | TradeSide::Short => {}
        }
    }
}

fn roi_candidate(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Result<Option<ExitDecision>, SimError> {
    let elapsed_minutes = u64::try_from(
        (candle.timestamp_ms - trade.open_timestamp_ms).max(0) / 60_000,
    )
    .map_err(|_| SimError::ExactArithmetic {
        operation: "roi-duration",
    })?;
    let Some((roi_entry, ratio)) = config.minimal_roi.range(..=elapsed_minutes).next_back() else {
        return Ok(None);
    };
    let rate = if ratio.total_cmp(&-1.0).is_eq() {
        candle.open
    } else {
        let open_base = precise_product(&[trade.amount, trade.open_rate])?;
        let open_fee = precise_product(&[open_base, fee_open(config)])?;
        let open_value = precise_sum(&[
            open_base,
            if trade.side == TradeSide::Long {
                open_fee
            } else {
                -open_fee
            },
        ])?;
        let close_base = precise_product(&[trade.amount, 1.0])?;
        let close_fee = precise_product(&[close_base, fee_close(config)])?;
        let alpha = precise_sum(&[
            close_base,
            if trade.side == TradeSide::Long {
                -close_fee
            } else {
                close_fee
            },
        ])?;
        let beta = if config.is_futures {
            if trade.side == TradeSide::Long {
                trade.funding_fees
            } else {
                -trade.funding_fees
            }
        } else {
            0.0
        };
        let direction = if trade.side == TradeSide::Long {
            1.0
        } else {
            -1.0
        };
        ((1.0 + ratio / direction) * open_value - beta) / alpha
    };
    let reached = match trade.side {
        TradeSide::Long => candle.high >= rate,
        TradeSide::Short => candle.low <= rate,
    };
    Ok(reached.then(|| {
        let new_roi_gap = elapsed_minutes > 0
            && elapsed_minutes == *roi_entry
            && match trade.side {
                TradeSide::Long => candle.open > rate,
                TradeSide::Short => candle.open < rate,
            };
        ExitDecision {
            rate: if new_roi_gap {
                candle.open
            } else {
                rate.clamp(candle.low, candle.high)
            },
            reason: "roi".to_owned(),
            requires_confirmation: true,
        }
    }))
}

#[cfg(test)]
pub(crate) fn exit_decision(
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Result<Option<ExitDecision>, SimError> {
    exit_decisions(trade, pair, candle_index, candle, config, profit_targets)
        .map(|mut candidates| candidates.drain(..).next())
}

pub(crate) fn stop_or_liquidation_exit_rate(
    trade: &OpenTrade,
    candle: &Candle,
    threshold: f64,
) -> f64 {
    // Freqtrade exits at the candle open when a previously retained stop or
    // liquidation threshold lies beyond the complete candle range. This is
    // observable after confirm_trade_exit rejected earlier stop candidates.
    let crossed_before_open = match trade.side {
        TradeSide::Long => threshold > candle.high,
        TradeSide::Short => threshold < candle.low,
    };
    if crossed_before_open {
        candle.open
    } else {
        threshold
    }
}

pub(crate) fn current_profit_ratio(trade: &OpenTrade, rate: f64, close_fee_rate: f64) -> f64 {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let hypothetical_proceeds = trade.amount * rate * (1.0 - close_fee_rate);
        return (hypothetical_proceeds - trade.entry_cost_with_fees + trade.funding_fees_total)
            / trade.entry_cost_with_fees;
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit = gross_profit - entry_fees - close_fees + trade.funding_fees_total;
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - (entry_fees / (trade.amount * trade.open_rate))
    } else {
        1.0 + (entry_fees / (trade.amount * trade.open_rate))
    };
    profit / (trade.stake_amount * open_fee_multiplier)
}

pub(crate) struct CloseTradeContext<'callbacks, 'program, 'runtime, 'events> {
    pub(crate) sequence: usize,
    pub(crate) order_id: u64,
    pub(crate) executable_callbacks:
        Option<&'callbacks mut ExecutableCallbacks<'program, 'runtime, 'events>>,
    pub(crate) wallet_available_before: f64,
}

pub(crate) fn close_trade(
    mut trade: OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    reason: String,
    config: &PortfolioConfig,
    mut context: CloseTradeContext<'_, '_, '_, '_>,
) -> Result<(ClosedTrade, f64), SimError> {
    // Backtest exit orders use the price precision captured when the trade
    // opened, not a later market-snapshot precision.
    let rate = round_step(rate, trade.price_step)?;
    let gross_proceeds = checked_float_product(&[trade.amount, rate], "exit-order-gross-proceeds")?;
    let open_fee_rate = fee_open(config);
    let close_fee_rate = fee_close(config);
    let (fallback_remaining_profit, fallback_remaining_profit_ratio) =
        fallback_close_profit(&trade, rate, open_fee_rate, close_fee_rate)?;
    let funding_fee = take_running_funding(&mut trade)?;
    trade.push_filled_order(FilledOrder {
        id: context.order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: timestamp_ms,
        amount: trade.amount,
        price: rate,
        cost: checked_float_product(&[gross_proceeds, 1.0 + close_fee_rate], "exit-order-cost")?,
        tag: Some(reason.clone()),
    })?;
    recalculate_order_funding_total(&mut trade)?;
    let (profit_abs, _fallback_profit_ratio) = replay_closed_profit(
        &trade,
        config,
        open_fee_rate,
        fallback_remaining_profit,
        fallback_remaining_profit_ratio,
    )?;
    let total_stake = freqtrade_total_entry_value(&trade, config)?;
    let profit_ratio = if total_stake == 0.0 {
        0.0
    } else {
        exact_ratio(
            profit_abs,
            total_stake,
            trade.leverage,
            "close-profit-ratio",
        )?
    };
    let wallet_proceeds = precise_sum(&[trade.stake_amount, profit_abs])?;
    if let Some(callbacks) = context.executable_callbacks.as_mut() {
        executable_order_filled(
            callbacks,
            &mut trade,
            precise_sum(&[context.wallet_available_before, wallet_proceeds])?,
        )?;
    }
    Ok((
        ClosedTrade {
            sequence: context.sequence,
            id: trade.id,
            pair: trade.pair,
            is_short: trade.side == TradeSide::Short,
            leverage: trade.leverage,
            open_timestamp_ms: trade.open_timestamp_ms,
            close_timestamp_ms: timestamp_ms,
            open_rate: trade.open_rate,
            close_rate: rate,
            amount: trade.amount,
            stake_amount: trade.stake_amount,
            max_stake_amount: trade.max_stake_amount,
            entry_tag: trade.entry_tag,
            exit_reason: reason,
            fee_open: open_fee_rate,
            fee_close: close_fee_rate,
            funding_fees: trade.funding_fees_total,
            liquidation_price: trade.liquidation_price,
            profit_abs,
            profit_ratio,
            initial_stop_loss: trade.initial_stop_loss,
            stop_loss: trade.stop_loss,
            custom_stop_loss_ratio: trade.custom_stop_loss_ratio,
            minimum_rate: trade.minimum_rate,
            maximum_rate: trade.maximum_rate,
            orders: trade.orders,
        },
        wallet_proceeds,
    ))
}

pub(crate) fn fallback_close_profit(
    trade: &OpenTrade,
    rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
) -> Result<(f64, f64), SimError> {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let proceeds = precise_product(&[trade.amount, rate, 1.0 - close_fee_rate])?;
        let profit_abs = round_eight(precise_sum(&[
            proceeds,
            -trade.entry_cost_with_fees,
            trade.funding_fees,
        ])?)?;
        let profit_ratio = exact_ratio(
            profit_abs,
            trade.entry_cost_with_fees,
            1.0,
            "fallback-spot-profit-ratio",
        )?;
        return Ok((profit_abs, profit_ratio));
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let rate_change = precise_sum(&[rate, -trade.open_rate])?;
    let gross_profit = precise_product(&[trade.amount, rate_change, direction])?;
    let entry_fees = precise_sum(&[trade.entry_cost_with_fees, -trade.stake_amount])?;
    let close_fees = precise_product(&[trade.amount, rate, close_fee_rate])?;
    let profit_abs = round_eight(precise_sum(&[
        gross_profit,
        -entry_fees,
        -close_fees,
        trade.funding_fees,
    ])?)?;
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - open_fee_rate
    } else {
        1.0 + open_fee_rate
    };
    let denominator = precise_product(&[trade.stake_amount, open_fee_multiplier])?;
    Ok((
        profit_abs,
        exact_ratio(profit_abs, denominator, 1.0, "fallback-profit-ratio")?,
    ))
}

pub(crate) fn replay_closed_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
    open_fee_rate: f64,
    fallback_remaining_profit: f64,
    fallback_remaining_profit_ratio: f64,
) -> Result<(f64, f64), SimError> {
    if is_unleveraged_spot(trade, config) {
        let replay = replay_spot_profit(trade, config)?;
        let ratio = if replay.total_entry_value == 0.0 {
            0.0
        } else {
            exact_ratio(
                replay.profit_abs,
                replay.total_entry_value,
                1.0,
                "spot-replay-profit-ratio",
            )?
        };
        return Ok((replay.profit_abs, ratio));
    }
    if config.is_futures || trade.adjustment_count > 0 {
        // Futures uses LocalTrade.calculate_profit even for a single-entry
        // position. That path converts FtPrecise open/close values back to
        // floats before applying Python's eight-decimal formatting. The
        // algebraically equivalent gross-profit shortcut can miss a half-way
        // decimal by one unit at variable leverage.
        let profit_abs = replay_leveraged_profit(trade, config)?;
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        let denominator = precise_product(&[trade.max_stake_amount, open_fee_multiplier])?;
        return Ok((
            profit_abs,
            exact_ratio(
                profit_abs,
                denominator,
                1.0,
                "leveraged-replay-profit-ratio",
            )?,
        ));
    }
    let profit_abs = round_eight(trade.realized_partial_profit + fallback_remaining_profit)?;
    let profit_ratio = if trade.realized_partial_profit == 0.0 {
        fallback_remaining_profit_ratio
    } else {
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        let denominator = precise_product(&[trade.max_stake_amount, open_fee_multiplier])?;
        exact_ratio(profit_abs, denominator, 1.0, "remaining-profit-ratio")?
    };
    Ok((profit_abs, profit_ratio))
}

fn exact_ratio(
    numerator: f64,
    denominator: f64,
    multiplier: f64,
    operation: &'static str,
) -> Result<f64, SimError> {
    if denominator == 0.0 {
        return Err(SimError::ExactArithmetic { operation });
    }
    let value = (numerator / denominator) * multiplier;
    if value.is_finite() {
        Ok(value)
    } else {
        Err(SimError::ExactArithmetic { operation })
    }
}
