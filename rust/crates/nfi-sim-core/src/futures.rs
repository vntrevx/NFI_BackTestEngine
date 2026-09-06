use crate::calculations::{
    checked_finite, checked_float_product, checked_float_sum, checked_python_float_sum,
};
use crate::domain::{
    Candle, EntrySignal, LeverageTier, NfiLeverageProgram, PairSeries, PortfolioConfig, SimError,
};
use crate::portfolio::{OpenTrade, TradeSide};

pub(super) fn entry_leverage(
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

pub(super) fn update_isolated_liquidation_price(
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
    };
    // Reject non-finite arithmetic before `f64::max`, which would otherwise
    // turn a NaN into the finite clamp operand and conceal the invalid value.
    if !buffered.is_finite() {
        return Err(SimError::InvalidLiquidationPrice {
            pair: trade.pair.clone(),
            timestamp_ms,
        });
    }
    trade.liquidation_price = Some(buffered.max(0.0));
    Ok(())
}

pub(super) fn evaluate_nfi_leverage(program: &NfiLeverageProgram, entry_tag: Option<&str>) -> f64 {
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

pub(super) fn apply_funding(
    trade: &mut OpenTrade,
    candle: &Candle,
    funding_fee_interval_ms: Option<i64>,
) -> Result<(), SimError> {
    let scheduled_refresh =
        funding_fee_interval_ms.is_some_and(|interval| candle.timestamp_ms % interval == 0);
    let mut changed = false;
    if scheduled_refresh {
        if let Some(seed) = trade.funding_rebase_seed.take() {
            reset_running_funding(trade, seed)?;
            changed = true;
        }
    }

    if let Some(signed) = funding_fee_at_candle(trade.side, trade.amount, candle)? {
        // Inputs created before the refresh cadence became explicit still
        // rebase on the next sparse event. Exact X7 manifests always carry the
        // cadence and take the scheduled branch above.
        if funding_fee_interval_ms.is_none() {
            if let Some(seed) = trade.funding_rebase_seed.take() {
                reset_running_funding(trade, seed)?;
            }
        }
        add_running_funding(trade, signed)?;
        changed = true;
    }

    if changed {
        // `Trade.set_funding_fees()` separately performs Python `sum()` over
        // the already-filled orders, then adds the current running segment.
        let prior_funding = checked_python_float_sum(
            trade.orders.iter().map(|order| order.funding_fee),
            "funding-prior-total",
        )?;
        trade.funding_fees_total = checked_float_sum(
            &[prior_funding, trade.funding_fees],
            "funding-visible-total",
        )?;
    }
    Ok(())
}

fn funding_fee_at_candle(
    side: TradeSide,
    amount: f64,
    candle: &Candle,
) -> Result<Option<f64>, SimError> {
    let (Some(rate), Some(mark_price)) = (candle.funding_rate, candle.funding_mark_price) else {
        return Ok(None);
    };
    // Pandas evaluates Freqtrade's expression left-to-right as
    // `(open_fund * open_mark) * amount`. Multiplying amount first is
    // mathematically equivalent but changes exported float tokens.
    let fee = checked_float_product(&[rate, mark_price, amount], "funding-fee-product")?;
    // Freqtrade's persisted convention is positive when the trade receives
    // funding and negative when it pays. A positive market funding rate is
    // therefore income for shorts and a cost for longs.
    Ok(Some(match side {
        TradeSide::Long => -fee,
        TradeSide::Short => fee,
    }))
}

fn add_running_funding(trade: &mut OpenTrade, signed: f64) -> Result<(), SimError> {
    // `Exchange.calculate_funding_fees()` uses Python `sum()` over all
    // funding rows since the most recent filled order. CPython 3.14 uses a
    // Neumaier correction for float iterables, so a plain `+=` can differ by
    // an exported ulp on long-running adjustment trades.
    let next = checked_float_sum(&[trade.funding_sum_high, signed], "funding-running-high")?;
    let correction = if trade.funding_sum_high.abs() >= signed.abs() {
        checked_float_sum(
            &[trade.funding_sum_high - next, signed],
            "funding-running-correction",
        )?
    } else {
        checked_float_sum(
            &[signed - next, trade.funding_sum_high],
            "funding-running-correction",
        )?
    };
    trade.funding_sum_low =
        checked_float_sum(&[trade.funding_sum_low, correction], "funding-running-low")?;
    trade.funding_sum_high = next;
    trade.funding_fees = compensated_sum_result(
        trade.funding_sum_high,
        trade.funding_sum_low,
        "funding-running-total",
    )?;
    Ok(())
}

fn reset_running_funding(trade: &mut OpenTrade, value: f64) -> Result<(), SimError> {
    let value = checked_finite(value, "funding-running-reset")?;
    trade.funding_sum_high = value;
    trade.funding_sum_low = 0.0;
    trade.funding_fees = value;
    Ok(())
}

/// Reproduce Freqtrade's forced funding refresh after an additional entry.
///
/// Backtesting first calculates funding before `adjust_trade_position`, moves
/// that running segment onto the newly filled order, and then calls
/// `_run_funding_fees(..., force=True)`. The exchange filter is inclusive at
/// both ends, so a fill exactly on a funding timestamp sees that row again
/// using the post-entry amount. A later exit attaches this refreshed running
/// segment to its order. Candles without funding data remain a no-op.
pub(super) fn reapply_inclusive_funding_after_entry_fill(
    trade: &mut OpenTrade,
    candle: &Candle,
    funding_fee_interval_ms: Option<i64>,
) -> Result<(), SimError> {
    apply_funding(trade, candle, funding_fee_interval_ms)
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
pub(super) fn preserve_partial_exit_funding_refresh(
    trade: &mut OpenTrade,
    candle: &Candle,
    amount_before_fill: f64,
) -> Result<(), SimError> {
    let Some(pre_exit_fee) = funding_fee_at_candle(trade.side, amount_before_fill, candle)? else {
        return Ok(());
    };
    let post_exit_fee = funding_fee_at_candle(trade.side, trade.amount, candle)?.ok_or(
        SimError::ExactArithmetic {
            operation: "funding-partial-exit-refresh",
        },
    )?;
    reset_running_funding(trade, pre_exit_fee)?;
    trade.funding_rebase_seed = Some(post_exit_fee);
    // `recalc_trade_from_orders()` runs after the forced refresh and resets
    // `funding_fees` to filled-order funding without clearing the separate
    // running value.
    recalculate_order_funding_total(trade)?;
    Ok(())
}

fn compensated_sum_result(high: f64, low: f64, operation: &'static str) -> Result<f64, SimError> {
    if low == 0.0 {
        checked_finite(high, operation)
    } else {
        checked_float_sum(&[high, low], operation)
    }
}

/// Move the current funding segment to a newly filled order.
///
/// Freqtrade resets `funding_fee_running` after every non-stoploss fill. The
/// compensated state must be reset at the same boundary or later segments
/// would retain an invisible correction from an earlier order.
pub(super) fn take_running_funding(trade: &mut OpenTrade) -> Result<f64, SimError> {
    trade.funding_sum_high = 0.0;
    trade.funding_sum_low = 0.0;
    trade.funding_rebase_seed = None;
    checked_finite(
        std::mem::take(&mut trade.funding_fees),
        "funding-take-running",
    )
}

/// Mirror the ordinary left-to-right accumulation in
/// `LocalTrade.recalc_trade_from_orders()`.
///
/// This intentionally does not use `python_float_sum`: Freqtrade's order
/// replay is an explicit `+=` loop, which has different rounding behavior.
pub(super) fn recalculate_order_funding_total(trade: &mut OpenTrade) -> Result<(), SimError> {
    trade.funding_fees_total = trade.orders.iter().try_fold(0.0, |total, order| {
        checked_float_sum(&[total, order.funding_fee], "funding-order-replay-total")
    })?;
    Ok(())
}
