//! Exit arbitration, stop handling, trade close, and closed-profit replay.

use std::collections::BTreeMap;

use crate::calculations::{
    exit_order_side, fee_close, fee_open, precise_product, round_eight, round_step,
};
use crate::callbacks::{callback_feature_index, evaluate_custom_exit_bundle};
use crate::domain::{
    AdjustmentSignal, Candle, ClosedTrade, FilledOrder, PairSeries, PortfolioConfig, SimError,
};
use crate::futures::{recalculate_order_funding_total, take_running_funding};
use crate::nfi::{evaluate_nfi_exit, CustomExitDecision, ProfitTarget};
use crate::portfolio::{OpenTrade, TradeSide};

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

#[allow(clippy::too_many_lines)]
pub(crate) fn exit_decision(
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Result<Option<ExitDecision>, SimError> {
    // This order mirrors Freqtrade 2026.5.1 `IStrategy.should_exit`.
    // Strategy exits precede liquidation and stop-loss candidates, so a
    // same-candle collision keeps the strategy reason and candle-open rate.
    let signal = match trade.side {
        TradeSide::Long => &candle.exit_long,
        TradeSide::Short => &candle.exit_short,
    };
    if let Some(signal) = signal {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: signal.reason.clone(),
            requires_confirmation: true,
        }));
    }
    if let Some(program) = &config.state_machine_program {
        if program.entrypoints.contains_key("custom_exit") {
            let feature_index =
                callback_feature_index(candle_index).ok_or(SimError::InvalidStateMachineProgram)?;
            if let Some(reason) =
                evaluate_state_machine_exit(program, trade, pair, feature_index, candle, config)?
            {
                return Ok(Some(ExitDecision {
                    rate: candle.open,
                    reason,
                    requires_confirmation: true,
                }));
            }
        }
    }
    if let Some(manager) = &config.nfi_x7_trade_manager {
        let feature_index =
            callback_feature_index(candle_index).ok_or(SimError::InvalidNfiTradeManager)?;
        let decision = evaluate_nfi_exit(
            manager,
            trade,
            pair,
            feature_index,
            candle,
            config,
            profit_targets,
        )
        .ok_or(SimError::InvalidNfiTradeManager)?;
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
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
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
        }
    }
    if config
        .custom_exit_after_ms
        .is_some_and(|duration| candle.timestamp_ms - trade.open_timestamp_ms >= duration)
    {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: "contract_timed_exit".to_owned(),
            requires_confirmation: true,
        }));
    }
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
        return Ok(Some(ExitDecision {
            rate: stop_or_liquidation_exit_rate(trade, candle, trade.stop_loss),
            reason: if trailing {
                "trailing_stop_loss".to_owned()
            } else {
                "stop_loss".to_owned()
            },
            requires_confirmation: true,
        }));
    }
    if let Some(liquidation_price) = trade.liquidation_price {
        let liquidated = match trade.side {
            TradeSide::Long => candle.low <= liquidation_price,
            TradeSide::Short => candle.high >= liquidation_price,
        };
        if liquidated {
            return Ok(Some(ExitDecision {
                rate: stop_or_liquidation_exit_rate(trade, candle, liquidation_price),
                reason: "liquidation".to_owned(),
                requires_confirmation: false,
            }));
        }
    }
    Ok(None)
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

pub(crate) fn close_trade(
    mut trade: OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    reason: String,
    config: &PortfolioConfig,
    sequence: usize,
    order_id: u64,
) -> (ClosedTrade, f64) {
    // Backtest exit orders use the price precision captured when the trade
    // opened, not a later market-snapshot precision.
    let rate = round_step(rate, trade.price_step);
    let gross_proceeds = trade.amount * rate;
    let open_fee_rate = fee_open(config);
    let close_fee_rate = fee_close(config);
    let (fallback_remaining_profit, fallback_remaining_profit_ratio) =
        fallback_close_profit(&trade, rate, open_fee_rate, close_fee_rate, gross_proceeds);
    let funding_fee = take_running_funding(&mut trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: timestamp_ms,
        amount: trade.amount,
        price: rate,
        cost: gross_proceeds * (1.0 + close_fee_rate),
        tag: Some(reason.clone()),
    });
    recalculate_order_funding_total(&mut trade);
    let (profit_abs, fallback_profit_ratio) = replay_closed_profit(
        &trade,
        config,
        open_fee_rate,
        fallback_remaining_profit,
        fallback_remaining_profit_ratio,
    );
    let profit_ratio =
        freqtrade_total_entry_value(&trade, config).map_or(fallback_profit_ratio, |total_stake| {
            if total_stake == 0.0 {
                0.0
            } else {
                (profit_abs / total_stake) * trade.leverage
            }
        });
    let wallet_proceeds = trade.stake_amount + profit_abs;
    (
        ClosedTrade {
            sequence,
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
            minimum_rate: trade.minimum_rate,
            maximum_rate: trade.maximum_rate,
            orders: trade.orders,
        },
        wallet_proceeds,
    )
}

pub(crate) fn fallback_close_profit(
    trade: &OpenTrade,
    rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    gross_proceeds: f64,
) -> (f64, f64) {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let proceeds =
            precise_product(&[trade.amount, rate, 1.0 - close_fee_rate]).unwrap_or(gross_proceeds);
        let profit_abs = round_eight(proceeds - trade.entry_cost_with_fees + trade.funding_fees);
        return (profit_abs, profit_abs / trade.entry_cost_with_fees);
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit_abs = round_eight(gross_profit - entry_fees - close_fees + trade.funding_fees);
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - open_fee_rate
    } else {
        1.0 + open_fee_rate
    };
    (
        profit_abs,
        profit_abs / (trade.stake_amount * open_fee_multiplier),
    )
}

pub(crate) fn replay_closed_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
    open_fee_rate: f64,
    fallback_remaining_profit: f64,
    fallback_remaining_profit_ratio: f64,
) -> (f64, f64) {
    if is_unleveraged_spot(trade, config) {
        return replay_spot_profit(trade, config).map_or_else(
            || {
                let profit_abs =
                    round_eight(trade.realized_partial_profit + fallback_remaining_profit);
                (profit_abs, fallback_remaining_profit_ratio)
            },
            |replay| {
                let ratio = if replay.total_entry_value == 0.0 {
                    0.0
                } else {
                    replay.profit_abs / replay.total_entry_value
                };
                (replay.profit_abs, ratio)
            },
        );
    }
    if config.is_futures || trade.adjustment_count > 0 {
        // Futures uses LocalTrade.calculate_profit even for a single-entry
        // position. That path converts FtPrecise open/close values back to
        // floats before applying Python's eight-decimal formatting. The
        // algebraically equivalent gross-profit shortcut can miss a half-way
        // decimal by one unit at variable leverage.
        let profit_abs = replay_leveraged_profit(trade, config).unwrap_or_else(|| {
            round_eight(trade.realized_partial_profit + fallback_remaining_profit)
        });
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        return (
            profit_abs,
            profit_abs / (trade.max_stake_amount * open_fee_multiplier),
        );
    }
    let profit_abs = round_eight(trade.realized_partial_profit + fallback_remaining_profit);
    let profit_ratio = if trade.realized_partial_profit == 0.0 {
        fallback_remaining_profit_ratio
    } else {
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        profit_abs / (trade.max_stake_amount * open_fee_multiplier)
    };
    (profit_abs, profit_ratio)
}
