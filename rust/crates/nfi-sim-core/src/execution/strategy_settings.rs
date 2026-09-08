//! Freqtrade strategy settings on the source-qualified execution contract.

use crate::calculations::{
    ceil_step, fee_close, fee_open, floor_step, precise_trade_value, round_eight,
};
use crate::domain::{Candle, PortfolioConfig, SimError};
use crate::portfolio::{OpenTrade, TradeSide};

pub(crate) fn callback_profit_ratio(
    trade: &OpenTrade,
    rate: f64,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    if config.strategy_exit_policy.is_some() {
        source_current_profit_ratio(trade, rate, config)
    } else {
        Ok(super::exit::current_profit_ratio(
            trade,
            rate,
            fee_close(config),
        ))
    }
}

pub(crate) fn source_custom_exit_allowed(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> bool {
    let Some(policy) = config.strategy_exit_policy.as_ref() else {
        return true;
    };
    let (enter, exit) = match trade.side {
        TradeSide::Long => (candle.enter_long.is_some(), candle.exit_long.is_some()),
        TradeSide::Short => (candle.enter_short.is_some(), candle.exit_short.is_some()),
    };
    policy.use_exit_signal && (!exit || enter)
}

pub(crate) fn source_current_profit_ratio(
    trade: &OpenTrade,
    rate: f64,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    let short = trade.side == TradeSide::Short;
    let open = precise_trade_value(trade.amount, trade.open_rate, fee_open(config), !short)?;
    if open == 0.0 {
        return Ok(0.0);
    }
    let mut close = precise_trade_value(trade.amount, rate, fee_close(config), short)?;
    if config.is_futures {
        close += if short {
            -trade.funding_fees_total
        } else {
            trade.funding_fees_total
        };
    }
    let ratio = if short {
        1.0 - close / open
    } else {
        close / open - 1.0
    };
    round_eight(ratio * trade.leverage)
}

pub(crate) fn update_source_trailing_stop(
    trade: &mut OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Result<(), SimError> {
    let inherited = inherited_custom_stoploss(config);
    if !config.trailing_stop && !inherited {
        return Ok(());
    }
    let short = trade.side == TradeSide::Short;
    let direction_correct = if short {
        trade.stop_loss > candle.high
    } else {
        trade.stop_loss < candle.low
    };
    if !direction_correct {
        return Ok(());
    }
    let bound = if short { candle.low } else { candle.high };
    if inherited {
        adjust_source_stop(trade, bound, config.stoploss_ratio, false)?;
    }
    if !config.trailing_stop {
        return Ok(());
    }
    let profit = source_current_profit_ratio(trade, bound, config)?;
    let offset = config.trailing_stop_positive_offset.unwrap_or(0.0);
    if config.trailing_only_offset_is_reached && profit < offset {
        return Ok(());
    }
    let ratio = if profit > offset {
        config
            .trailing_stop_positive
            .unwrap_or(config.stoploss_ratio)
    } else {
        config.stoploss_ratio
    };
    adjust_source_stop(trade, bound, ratio, false)
}

pub(crate) fn inherited_custom_stoploss(config: &PortfolioConfig) -> bool {
    config
        .strategy_exit_policy
        .as_ref()
        .is_some_and(|policy| policy.inherited_custom_stoploss)
}

pub(crate) fn adjust_source_stop(
    trade: &mut OpenTrade,
    bound: f64,
    ratio: f64,
    allow_refresh: bool,
) -> Result<(), SimError> {
    let short = trade.side == TradeSide::Short;
    let distance = (ratio / trade.leverage).abs();
    let adjusted = if short {
        floor_step(bound * (1.0 + distance), trade.price_step)?
    } else {
        ceil_step(bound * (1.0 - distance), trade.price_step)?
    };
    if allow_refresh
        || (short && adjusted < trade.stop_loss)
        || (!short && adjusted > trade.stop_loss)
    {
        if !allow_refresh {
            trade.is_stop_loss_trailing = true;
        }
        trade.stop_loss = adjusted;
        trade.custom_stop_loss_ratio = Some(-ratio.abs());
    }
    Ok(())
}

pub(crate) fn refresh_inherited_stop_after_fill(
    trade: &mut OpenTrade,
    rate: f64,
    config: &PortfolioConfig,
) -> Result<(), SimError> {
    let direction_correct = match trade.side {
        TradeSide::Long => trade.stop_loss < rate,
        TradeSide::Short => trade.stop_loss > rate,
    };
    if inherited_custom_stoploss(config) && direction_correct {
        adjust_source_stop(trade, rate, config.stoploss_ratio, true)?;
        // The inherited callback opts into after_fill. Freqtrade then runs
        // ordinary trailing adjustment using the pre-refresh direction gate.
        if config.trailing_stop {
            let profit = source_current_profit_ratio(trade, rate, config)?;
            let offset = config.trailing_stop_positive_offset.unwrap_or(0.0);
            if !config.trailing_only_offset_is_reached || profit >= offset {
                let ratio = if profit > offset {
                    config
                        .trailing_stop_positive
                        .unwrap_or(config.stoploss_ratio)
                } else {
                    config.stoploss_ratio
                };
                adjust_source_stop(trade, rate, ratio, false)?;
            }
        }
    }
    Ok(())
}

pub(crate) fn source_trailing_exit_rate(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> f64 {
    let short = trade.side == TradeSide::Short;
    if (short && trade.stop_loss < candle.low) || (!short && trade.stop_loss > candle.high) {
        return candle.open;
    }
    if (candle.timestamp_ms - trade.open_timestamp_ms) / 60_000 != 0 {
        return trade.stop_loss;
    }
    let direction = if short { -1.0 } else { 1.0 };
    let rate = if !inherited_custom_stoploss(config)
        && config.trailing_stop
        && config.trailing_only_offset_is_reached
        && config.trailing_stop_positive_offset.is_some()
        && config
            .trailing_stop_positive
            .is_some_and(|value| value != 0.0)
    {
        candle.open
            * (1.0 + direction * config.trailing_stop_positive_offset.unwrap().abs()
                - direction * (config.trailing_stop_positive.unwrap() / trade.leverage).abs())
    } else {
        candle.open
            * (1.0
                - direction
                    * (trade
                        .custom_stop_loss_ratio
                        .unwrap_or(config.stoploss_ratio)
                        / trade.leverage)
                        .abs())
    };
    if short {
        candle.high.min(rate)
    } else {
        candle.low.max(rate)
    }
}

pub(crate) fn source_roi_exit_rate(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
    elapsed_minutes: u64,
    roi_entry: u64,
    ratio: f64,
) -> Result<Option<f64>, SimError> {
    let short = trade.side == TradeSide::Short;
    let bound = if short { candle.low } else { candle.high };
    // min_roi_reached compares the rounded leveraged profit strictly.
    if source_current_profit_ratio(trade, bound, config)? <= ratio {
        return Ok(None);
    }
    let timeframe = config
        .strategy_exit_policy
        .as_ref()
        .ok_or(SimError::InvalidPositiveConfig("strategy_exit_policy"))?
        .timeframe_minutes;
    let on_boundary = roi_entry % timeframe == 0;
    if ratio.total_cmp(&-1.0).is_eq() && on_boundary {
        return Ok(Some(candle.open));
    }
    let open_value = precise_trade_value(trade.amount, trade.open_rate, fee_open(config), !short)?;
    let beta = if config.is_futures {
        if short {
            -trade.funding_fees_total
        } else {
            trade.funding_fees_total
        }
    } else {
        0.0
    };
    // Preserve Freqtrade's floating point subtraction of its zero-rate and
    // unit-rate probes, including cancellation from funding.
    let value_at_one = precise_trade_value(trade.amount, 1.0, fee_close(config), short)? + beta;
    let alpha = value_at_one - beta;
    let direction = if short { -1.0 } else { 1.0 };
    let rate = ((1.0 + (ratio / trade.leverage) / direction) * open_value - beta) / alpha;
    let new_gap = if short {
        candle.open < rate
    } else {
        candle.open > rate
    };
    if elapsed_minutes > 0 && elapsed_minutes == roi_entry && on_boundary && new_gap {
        return Ok(Some(candle.open));
    }
    let invalid_opening_exit = if short {
        candle.open < candle.close && trade.open_rate > candle.open && rate < candle.close
    } else {
        candle.open > candle.close && trade.open_rate < candle.open && rate > candle.close
    };
    if elapsed_minutes == 0 && invalid_opening_exit {
        return Ok(None);
    }
    Ok(Some(rate.clamp(candle.low, candle.high)))
}
