//! Position adjustment and filled-order replay.

use std::collections::BTreeMap;

use num_rational::BigRational;
use num_traits::{ToPrimitive, Zero};
use serde_json::Value;

use crate::calculations::{
    ceil_step, checked_float_product, entry_order_side, entry_sizing, exact_rational,
    exit_order_side, fee_close, fee_open, floor_step, ft_precise_division, precise_product,
    precise_product_quotient, precise_sum, round_eight, round_step,
};
use crate::domain::{
    AdjustmentSignal, CallbackInvocation, CallbackReturnClass, Candle, ExecutableCallbackError,
    FilledOrder, PairSeries, PortfolioConfig, SimError,
};
use crate::futures::{
    preserve_partial_exit_funding_refresh, reapply_inclusive_funding_after_entry_fill,
    recalculate_order_funding_total, take_running_funding, update_isolated_liquidation_price,
};
use crate::portfolio::{OpenTrade, TradeSide};

use super::callback_trace::ExecutableCallbacks;
use super::entry::{
    adjustment_minimum_pair_stake, apply_order_filled, minimum_pair_stake, validate_stake_amount,
};

pub(crate) fn update_extrema(trade: &mut OpenTrade, candle: &Candle) {
    trade.minimum_rate = trade.minimum_rate.min(candle.low);
    trade.maximum_rate = trade.maximum_rate.max(candle.high);
}

pub(crate) fn executable_position_adjustment(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle: &Candle,
    minimum_stake: f64,
    maximum_stake: f64,
    current_profit: f64,
) -> Result<Option<AdjustmentSignal>, ExecutableCallbackError> {
    let invocation = position_adjustment_invocation(
        trade,
        pair,
        candle,
        minimum_stake,
        maximum_stake,
        current_profit,
    );
    let event = callbacks.invoke(&invocation, &mut trade.custom_data)?;
    if event.return_class == CallbackReturnClass::None {
        return Ok(None);
    }
    Ok(position_adjustment_signal(event.return_value.as_ref()))
}

fn position_adjustment_invocation(
    trade: &OpenTrade,
    pair: &PairSeries,
    candle: &Candle,
    minimum_stake: f64,
    maximum_stake: f64,
    current_profit: f64,
) -> CallbackInvocation {
    let inputs = BTreeMap::from([
        ("pair".to_owned(), Value::String(pair.pair.clone())),
        ("current_time".to_owned(), Value::from(candle.timestamp_ms)),
        ("current_rate".to_owned(), Value::from(candle.open)),
        ("current_profit".to_owned(), Value::from(current_profit)),
        ("min_stake".to_owned(), Value::from(minimum_stake)),
        ("max_stake".to_owned(), Value::from(maximum_stake)),
    ]);
    let mut invocation =
        CallbackInvocation::new("adjust_trade_position", candle.timestamp_ms, inputs);
    invocation.trade = position_trade_values(trade);
    invocation.candle = BTreeMap::from([
        ("open".to_owned(), Value::from(candle.open)),
        ("high".to_owned(), Value::from(candle.high)),
        ("low".to_owned(), Value::from(candle.low)),
        ("close".to_owned(), Value::from(candle.close)),
    ]);
    invocation.wallet = BTreeMap::from([("available".to_owned(), Value::from(maximum_stake))]);
    invocation
}

fn position_trade_values(trade: &OpenTrade) -> BTreeMap<String, Value> {
    BTreeMap::from([
        ("id".to_owned(), Value::from(trade.id)),
        ("pair".to_owned(), Value::from(trade.pair.clone())),
        ("amount".to_owned(), Value::from(trade.amount)),
        ("stake_amount".to_owned(), Value::from(trade.stake_amount)),
        ("open_rate".to_owned(), Value::from(trade.open_rate)),
        ("leverage".to_owned(), Value::from(trade.leverage)),
        (
            "adjustment_count".to_owned(),
            Value::from(trade.adjustment_count),
        ),
        ("order_count".to_owned(), Value::from(trade.orders.len())),
        (
            "nr_of_successful_entries".to_owned(),
            Value::from(trade.orders.iter().filter(|order| order.is_entry).count()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            Value::from(trade.orders.iter().filter(|order| !order.is_entry).count()),
        ),
        (
            "orders".to_owned(),
            Value::Array(
                trade
                    .orders
                    .iter()
                    .map(|order| Value::from(order.id))
                    .collect(),
            ),
        ),
    ])
}

fn position_adjustment_signal(value: Option<&Value>) -> Option<AdjustmentSignal> {
    let (stake_amount, tag) = value.map_or((None, None), |value| {
        if let Some(items) = value.as_array() {
            (
                items.first().and_then(Value::as_f64),
                items.get(1).and_then(Value::as_str).map(ToOwned::to_owned),
            )
        } else {
            (value.as_f64(), None)
        }
    });
    stake_amount
        .filter(|value| *value != 0.0)
        .map(|stake_amount| AdjustmentSignal {
            stake_amount,
            tag: tag.unwrap_or_default(),
        })
}

pub(crate) fn apply_adjustment(
    trade: &mut OpenTrade,
    pair: &PairSeries,
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
        return apply_partial_exit(trade, pair, candle, adjustment, config, order_id);
    }
    let minimum = minimum_pair_stake(
        pair,
        candle.open,
        0.0,
        trade.leverage,
        config.amount_reserve_percent,
    );
    let Some(requested) =
        validate_stake_amount(adjustment.stake_amount, minimum, available_balance)
    else {
        return Ok(());
    };
    let Some((amount, _, _, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        trade.amount_step,
        trade.leverage,
    )?
    else {
        return Ok(());
    };
    let funding_fee = take_running_funding(trade)?;
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
    })?;
    recalculate_order_funding_total(trade)?;
    // Freqtrade does not update these fields incrementally. Its
    // `LocalTrade.recalc_trade_from_orders()` replays every filled order after
    // each adjustment. Replaying here preserves weighted-basis exits and the
    // all-time entry stake even after a cluster has been sold.
    recalculate_open_trade_from_orders(trade, config)?;
    reapply_inclusive_funding_after_entry_fill(trade, candle, config.funding_fee_interval_ms)?;
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config)?;
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    Ok(())
}

pub(crate) fn apply_partial_exit(
    trade: &mut OpenTrade,
    pair: &PairSeries,
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
    let raw_amount = precise_product_quotient(requested_stake, trade.amount, trade.stake_amount)?;
    let amount = floor_step(raw_amount, trade.amount_step)?;
    if amount <= 0.0 || amount >= trade.amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    let remaining_stake = (trade.amount - amount) * candle.open;
    let minimum = adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent);
    if remaining_stake != 0.0 && remaining_stake < minimum {
        return Ok(());
    }
    // Freqtrade freezes price precision on the trade when it opens and runs
    // every later exit through price_to_precision. This remains observable
    // after an exchange changes the pair tick size during a long-lived NFI
    // position: callback arithmetic uses the raw candle open, while the
    // resulting partial-exit order is filled at the frozen rounded price.
    let exit_rate = round_step(candle.open, trade.price_step)?;
    let funding_fee = take_running_funding(trade)?;
    trade.push_filled_order(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: exit_rate,
        cost: checked_float_product(
            &[amount, exit_rate, 1.0 + fee_close(config)],
            "partial-exit-order-cost",
        )?,
        tag: Some(adjustment.tag.clone()),
    })?;
    recalculate_order_funding_total(trade)?;
    // Pinned Freqtrade refreshes isolated liquidation inside
    // `_try_close_open_order()`, before `_process_exit_order()` replays the
    // partial exit into LocalTrade. The resulting one-adjustment lag is
    // observable when a second derisk changes the Binance maintenance tier.
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    recalculate_open_trade_from_orders(trade, config)?;
    preserve_partial_exit_funding_refresh(trade, candle, amount_before_fill)?;
    trade.realized_partial_profit = if is_unleveraged_spot(trade, config) {
        replay_spot_profit(trade, config)?.profit_abs
    } else {
        replay_leveraged_profit(trade, config)?
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
) -> Result<(), SimError> {
    let failure = || SimError::ExactArithmetic {
        operation: "open-trade-order-replay",
    };
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut maximum_stake = BigRational::zero();
    let mut average_price = BigRational::zero();

    for order in &trade.orders {
        let amount = exact_rational(order.amount).ok_or_else(failure)?;
        let price = exact_rational(order.price).ok_or_else(failure)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return Err(failure());
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &price * &amount;
            maximum_stake += &price * &amount;
            average_price =
                ft_precise_division(&current_stake, &current_amount).ok_or_else(failure)?;
        } else {
            current_amount -= &amount;
            current_stake -= &average_price * &amount;
        }
    }
    if current_amount <= BigRational::zero() || current_stake <= BigRational::zero() {
        return Err(failure());
    }

    let finite = |value: &BigRational| value.to_f64().filter(|number| number.is_finite());
    let raw_amount = finite(&current_amount).ok_or_else(failure)?;
    let raw_stake = finite(&current_stake).ok_or_else(failure)?;
    trade.amount = floor_step(raw_amount, trade.amount_step)?;
    trade.stake_amount = finite_division(raw_stake, trade.leverage, "open-trade-stake")?;
    trade.max_stake_amount = finite_division(
        finite(&maximum_stake).ok_or_else(failure)?,
        trade.leverage,
        "open-trade-maximum-stake",
    )?;
    trade.open_rate = round_step(
        finite(&(&current_stake / &current_amount)).ok_or_else(failure)?,
        trade.price_step,
    )?;
    let leveraged_stoploss = config.stoploss_ratio / trade.leverage;
    let adjusted_stop = match trade.side {
        TradeSide::Long => ceil_step(
            trade.open_rate * (1.0 + leveraged_stoploss),
            trade.price_step,
        )?,
        TradeSide::Short => floor_step(
            trade.open_rate * (1.0 - leveraged_stoploss),
            trade.price_step,
        )?,
    };
    trade.stop_loss = match trade.side {
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
    Ok(())
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
) -> Result<ProfitReplay, SimError> {
    let failure = || SimError::ExactArithmetic {
        operation: "spot-profit-replay",
    };
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut total_entry_value = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        let amount = exact_rational(order.amount).ok_or_else(failure)?;
        let price = exact_rational(order.price).ok_or_else(failure)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return Err(failure());
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price =
                ft_precise_division(&current_stake, &current_amount).ok_or_else(failure)?;
            let entry_value =
                precise_product(&[order.amount, order.price, 1.0 + fee_open(config)])?;
            total_entry_value =
                checked_sum(total_entry_value, entry_value, "spot-total-entry-value")?;
            continue;
        }

        if amount > current_amount {
            return Err(failure());
        }
        let open_value = precise_product(&[
            order.amount,
            average_price
                .to_f64()
                .filter(|value| value.is_finite())
                .ok_or_else(failure)?,
            1.0 + fee_open(config),
        ])?;
        let close_value = precise_product(&[order.amount, order.price, 1.0 - fee_close(config)])?;
        let exit_profit = if trade.side == TradeSide::Long {
            close_value - open_value
        } else {
            open_value - close_value
        };
        let exit_profit = checked_finite(exit_profit, "spot-exit-profit")?;
        profit_abs = checked_sum(profit_abs, round_eight(exit_profit)?, "spot-profit-total")?;
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }

    Ok(ProfitReplay {
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
pub(crate) fn replay_leveraged_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    let failure = || SimError::ExactArithmetic {
        operation: "leveraged-profit-replay",
    };
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut current_funding = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        current_funding = checked_sum(
            current_funding,
            order.funding_fee,
            "leveraged-funding-total",
        )?;
        let amount = exact_rational(order.amount).ok_or_else(failure)?;
        let price = exact_rational(order.price).ok_or_else(failure)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return Err(failure());
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price =
                ft_precise_division(&current_stake, &current_amount).ok_or_else(failure)?;
            continue;
        }
        if amount > current_amount {
            return Err(failure());
        }

        let average = average_price
            .to_f64()
            .filter(|value| value.is_finite())
            .ok_or_else(failure)?;
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
        let exit_profit = checked_finite(exit_profit, "leveraged-exit-profit")?;
        profit_abs = checked_sum(
            profit_abs,
            round_eight(exit_profit)?,
            "leveraged-profit-total",
        )?;
        current_funding = 0.0;
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }
    Ok(profit_abs)
}

pub(crate) fn freqtrade_total_entry_value(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
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
            let entry_value = precise_product(&[order.amount, order.price, open_multiplier])?;
            checked_sum(total, entry_value, "total-entry-value")
        })
}

fn checked_sum(left: f64, right: f64, operation: &'static str) -> Result<f64, SimError> {
    checked_finite(left + right, operation)
}

fn checked_finite(value: f64, operation: &'static str) -> Result<f64, SimError> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(SimError::ExactArithmetic { operation })
    }
}

fn finite_division(
    numerator: f64,
    denominator: f64,
    operation: &'static str,
) -> Result<f64, SimError> {
    let value = numerator / denominator;
    if value.is_finite() {
        Ok(value)
    } else {
        Err(SimError::ExactArithmetic { operation })
    }
}
