//! Position adjustment and filled-order replay.

use num_rational::BigRational;
use num_traits::{ToPrimitive, Zero};

use crate::calculations::{
    ceil_step, entry_order_side, entry_sizing, exact_rational, exit_order_side, fee_close,
    fee_open, floor_step, ft_precise_division, precise_product, precise_product_quotient,
    precise_sum, round_eight, round_step,
};
use crate::domain::{AdjustmentSignal, Candle, FilledOrder, PortfolioConfig, SimError};
use crate::futures::{
    preserve_partial_exit_funding_refresh, reapply_inclusive_funding_after_entry_fill,
    recalculate_order_funding_total, take_running_funding, update_isolated_liquidation_price,
};
use crate::portfolio::{OpenTrade, TradeSide};

use super::entry::apply_order_filled;

pub(crate) fn update_extrema(trade: &mut OpenTrade, candle: &Candle) {
    trade.minimum_rate = trade.minimum_rate.min(candle.low);
    trade.maximum_rate = trade.maximum_rate.max(candle.high);
}

pub(crate) fn apply_adjustment(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    available_balance: f64,
    order_id: u64,
) -> Result<(), SimError> {
    if !adjustment.stake_amount.is_finite() || adjustment.stake_amount == 0.0 {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    if adjustment.stake_amount < 0.0 {
        return apply_partial_exit(trade, candle, adjustment, config, order_id);
    }
    let requested = adjustment.stake_amount.min(available_balance);
    let Some((amount, _, _, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        trade.amount_step,
        trade.leverage,
    ) else {
        return Ok(());
    };
    let funding_fee = take_running_funding(trade);
    trade.push_filled_order(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: entry_order_side(trade.side),
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    // Freqtrade does not update these fields incrementally. Its
    // `LocalTrade.recalc_trade_from_orders()` replays every filled order after
    // each adjustment. Replaying here preserves weighted-basis exits and the
    // all-time entry stake even after a cluster has been sold.
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    reapply_inclusive_funding_after_entry_fill(trade, candle, config.funding_fee_interval_ms);
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config)?;
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    Ok(())
}

pub(crate) fn apply_partial_exit(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    order_id: u64,
) -> Result<(), SimError> {
    let amount_before_fill = trade.amount;
    let requested_stake = -adjustment.stake_amount;
    if requested_stake >= trade.stake_amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    // Freqtrade performs this multiplication with `FtPrecise` before amount
    // precision is applied. A mathematically exact 0.46 can therefore become
    // 0.459999... and correctly truncate to 0.45 on a 0.01 market step.
    let raw_amount = precise_product_quotient(requested_stake, trade.amount, trade.stake_amount)
        .ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?;
    let amount = floor_step(raw_amount, trade.amount_step);
    if amount <= 0.0 || amount >= trade.amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    // Freqtrade freezes price precision on the trade when it opens and runs
    // every later exit through price_to_precision. This remains observable
    // after an exchange changes the pair tick size during a long-lived NFI
    // position: callback arithmetic uses the raw candle open, while the
    // resulting partial-exit order is filled at the frozen rounded price.
    let exit_rate = round_step(candle.open, trade.price_step);
    let funding_fee = take_running_funding(trade);
    trade.push_filled_order(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: exit_rate,
        cost: amount * exit_rate * (1.0 + fee_close(config)),
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    // Pinned Freqtrade refreshes isolated liquidation inside
    // `_try_close_open_order()`, before `_process_exit_order()` replays the
    // partial exit into LocalTrade. The resulting one-adjustment lag is
    // observable when a second derisk changes the Binance maintenance tier.
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    preserve_partial_exit_funding_refresh(trade, candle, amount_before_fill);
    trade.realized_partial_profit = if is_unleveraged_spot(trade, config) {
        replay_spot_profit(trade, config)
            .map(|replay| replay.profit_abs)
            .ok_or_else(|| SimError::InvalidAdjustment {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?
    } else {
        replay_leveraged_profit(trade, config).ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?
    };
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config)?;
    Ok(())
}

/// Rebuild Freqtrade's order-derived open-position fields.
///
/// Exit orders remove stake at the weighted entry price, not at their fill
/// price. `max_stake_amount` is the sum of every successful entry and never
/// shrinks after partial exits. Decimal replay also prevents accumulated
/// binary-float drift across the hundreds of X7 grind orders.
pub(crate) fn recalculate_open_trade_from_orders(
    trade: &mut OpenTrade,
    config: &PortfolioConfig,
) -> Option<()> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut maximum_stake = BigRational::zero();
    let mut average_price = BigRational::zero();

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &price * &amount;
            maximum_stake += &price * &amount;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
        } else {
            current_amount -= &amount;
            current_stake -= &average_price * &amount;
        }
    }
    if current_amount <= BigRational::zero() || current_stake <= BigRational::zero() {
        return None;
    }

    let raw_amount = current_amount.to_f64()?;
    let raw_stake = current_stake.to_f64()?;
    trade.amount = floor_step(raw_amount, trade.amount_step);
    trade.stake_amount = raw_stake / trade.leverage;
    trade.max_stake_amount = maximum_stake.to_f64()? / trade.leverage;
    trade.open_rate = round_step(
        (&current_stake / &current_amount).to_f64()?,
        trade.price_step,
    );
    let leveraged_stoploss = config.stoploss_ratio / trade.leverage;
    let adjusted_stop = match trade.side {
        TradeSide::Long => ceil_step(
            trade.open_rate * (1.0 + leveraged_stoploss),
            trade.price_step,
        ),
        TradeSide::Short => floor_step(
            trade.open_rate * (1.0 - leveraged_stoploss),
            trade.price_step,
        ),
    };
    trade.stop_loss = match trade.side {
        // `adjust_stop_loss()` is monotonic: position adjustment may protect
        // more profit, but it must never loosen an already established stop.
        TradeSide::Long => trade.stop_loss.max(adjusted_stop),
        TradeSide::Short => trade.stop_loss.min(adjusted_stop),
    };

    let notional = precise_product(&[trade.amount, trade.open_rate])?;
    trade.entry_cost_with_fees = if (trade.leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[trade.amount, trade.open_rate, 1.0 + fee_open(config)])?
    } else {
        let entry_fee = precise_product(&[notional, fee_open(config)])?;
        precise_sum(&[trade.stake_amount, entry_fee])?
    };
    Some(())
}

pub(crate) struct ProfitReplay {
    pub(crate) profit_abs: f64,
    pub(crate) total_entry_value: f64,
}

pub(crate) fn is_unleveraged_spot(trade: &OpenTrade, config: &PortfolioConfig) -> bool {
    !config.is_futures && (trade.leverage - 1.0).abs() < f64::EPSILON
}

/// Replay Freqtrade's spot `recalc_trade_from_orders()` profit path.
///
/// Each partial exit is valued against the weighted entry price at that point
/// and rounded to eight decimals before it is added to cumulative profit.
/// The denominator includes entry fees for every buy, matching
/// `LocalTrade.close_profit` rather than the fee-free `max_stake_amount`.
pub(crate) fn replay_spot_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<ProfitReplay> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut total_entry_value = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            total_entry_value +=
                precise_product(&[order.amount, order.price, 1.0 + fee_open(config)])?;
            continue;
        }

        if amount > current_amount {
            return None;
        }
        let open_value = precise_product(&[
            order.amount,
            average_price.to_f64()?,
            1.0 + fee_open(config),
        ])?;
        let close_value = precise_product(&[order.amount, order.price, 1.0 - fee_close(config)])?;
        let exit_profit = if trade.side == TradeSide::Long {
            close_value - open_value
        } else {
            open_value - close_value
        };
        profit_abs += round_eight(exit_profit);
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }

    Some(ProfitReplay {
        profit_abs,
        total_entry_value,
    })
}

/// Replay Freqtrade's leveraged/futures realized-profit calculation.
///
/// Freqtrade stores the full running funding amount on the next filled order.
/// During order replay it accumulates those values until an exit, includes the
/// accumulated funding in that exit's profit, rounds the exit profit to eight
/// decimals, and then resets the funding accumulator. This differs materially
/// from prorating funding by the partial-exit amount.
pub(crate) fn replay_leveraged_profit(trade: &OpenTrade, config: &PortfolioConfig) -> Option<f64> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut current_funding = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        current_funding += order.funding_fee;
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            continue;
        }
        if amount > current_amount {
            return None;
        }

        let average = average_price.to_f64()?;
        let open_multiplier = if trade.side == TradeSide::Short {
            1.0 - fee_open(config)
        } else {
            1.0 + fee_open(config)
        };
        let close_multiplier = if trade.side == TradeSide::Short {
            1.0 + fee_close(config)
        } else {
            1.0 - fee_close(config)
        };
        let open_value = precise_product(&[order.amount, average, open_multiplier])?;
        let close_value = precise_product(&[order.amount, order.price, close_multiplier])?;
        let exit_profit = if trade.side == TradeSide::Short {
            open_value - close_value + current_funding
        } else {
            close_value - open_value + current_funding
        };
        profit_abs += round_eight(exit_profit);
        current_funding = 0.0;
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }
    Some(profit_abs)
}

pub(crate) fn freqtrade_total_entry_value(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<f64> {
    let open_multiplier = if trade.side == TradeSide::Short {
        1.0 - fee_open(config)
    } else {
        1.0 + fee_open(config)
    };
    trade
        .orders
        .iter()
        .filter(|order| order.is_entry)
        .try_fold(0.0, |total, order| {
            precise_product(&[order.amount, order.price, open_multiplier])
                .map(|entry_value| total + entry_value)
        })
}
