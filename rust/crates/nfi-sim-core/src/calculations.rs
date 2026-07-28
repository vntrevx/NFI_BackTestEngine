//! Side-effect-free scheduling, sizing, precision, and aggregation calculations.

use std::str::FromStr;
use std::time::Duration;

use num_bigint::BigInt;
use num_rational::BigRational;
use num_traits::{One, ToPrimitive, Zero};
use rust_decimal::Decimal;

use super::{OrderSide, PairSeries, PortfolioConfig, TradeSide};

pub(super) fn duration_ns(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

pub(super) fn scheduled_cursor(
    pair: &PairSeries,
    start: usize,
    has_open_trade: bool,
    sparse_execution: bool,
) -> usize {
    if sparse_execution && !has_open_trade {
        pair.candles
            .next_entry_index(start)
            .unwrap_or(pair.candles.len())
    } else {
        start
    }
}

pub(super) fn logical_pair_event_count(pairs: &[PairSeries]) -> u64 {
    pairs.iter().fold(0_u64, |total, pair| {
        total.saturating_add(
            u64::try_from(
                pair.candles
                    .len()
                    .saturating_sub(pair.execution_start_index),
            )
            .unwrap_or(u64::MAX),
        )
    })
}

pub(super) fn available_stake_amount(free: f64, tied_up_stake: f64, ratio: f64) -> f64 {
    let total_stake_amount = (tied_up_stake + free) * ratio;
    (total_stake_amount - tied_up_stake).min(free).max(0.0)
}

pub(super) fn entry_sizing(
    requested: f64,
    rate: f64,
    fee_rate: f64,
    amount_step: f64,
    leverage: f64,
) -> Option<(f64, f64, f64, f64)> {
    // Freqtrade treats the callback's stake as collateral/notional and derives
    // amount before accounting for fees (`stake / rate * leverage`). Fees
    // affect profit and wallet proceeds, but do not shrink the requested base
    // amount at entry.
    let raw_amount = requested * leverage / rate;
    let amount = floor_step(raw_amount, amount_step);
    if amount <= 0.0 {
        return None;
    }
    let notional = precise_product(&[amount, rate])?;
    let stake = if (leverage - 1.0).abs() < f64::EPSILON {
        notional
    } else {
        // LocalTrade first stores the ordinary Python-float result of
        // `amount * rate / leverage`. Its later partial-exit callback feeds
        // that float into FtPrecise. Replacing the initial operation with
        // exact decimal arithmetic can move an integer-contract exit one
        // whole step at values such as 474 - a visible parity difference.
        (amount * rate) / leverage
    };
    let precise_cost = if (leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[amount, rate, 1.0 + fee_rate])?
    } else {
        let entry_fee = precise_product(&[notional, fee_rate])?;
        precise_sum(&[stake, entry_fee])?
    };
    let order_cost = (amount * rate) * (1.0 + fee_rate);
    Some((amount, stake, precise_cost, order_cost))
}

pub(super) fn fee_open(config: &PortfolioConfig) -> f64 {
    config.fee_open_rate.unwrap_or(config.fee_rate)
}

pub(super) fn fee_close(config: &PortfolioConfig) -> f64 {
    config.fee_close_rate.unwrap_or(config.fee_rate)
}

pub(super) const fn entry_order_side(side: TradeSide) -> OrderSide {
    match side {
        TradeSide::Long => OrderSide::Buy,
        TradeSide::Short => OrderSide::Sell,
    }
}

pub(super) const fn exit_order_side(side: TradeSide) -> OrderSide {
    match side {
        TradeSide::Long => OrderSide::Sell,
        TradeSide::Short => OrderSide::Buy,
    }
}

pub(super) fn floor_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Floor).unwrap_or_else(|| {
        let units = (value / step).floor();
        units * step
    })
}

pub(super) fn ceil_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Ceil)
        .unwrap_or_else(|| (value / step).ceil() * step)
}

pub(super) fn round_step(value: f64, step: f64) -> f64 {
    exact_step_quantize(value, step, StepQuantize::Round)
        .unwrap_or_else(|| (value / step).round() * step)
}

#[derive(Clone, Copy)]
enum StepQuantize {
    Floor,
    Ceil,
    Round,
}

/// Apply exchange tick precision without dividing binary floats.
///
/// Values such as `8.45 / 0.01` can become `844.999...` in f64 and lose a
/// full market step. CCXT precision works on decimal text, so the simulator
/// must choose the integer number of ticks in that same domain.
fn exact_step_quantize(value: f64, step: f64, mode: StepQuantize) -> Option<f64> {
    let value = exact_rational(value)?;
    let step = exact_rational(step)?;
    if value < BigRational::zero() || step <= BigRational::zero() {
        return None;
    }
    let quotient = &value / &step;
    let floor = quotient.to_integer();
    let remainder = quotient - BigRational::from_integer(floor.clone());
    let units = match mode {
        StepQuantize::Floor => floor,
        StepQuantize::Ceil => {
            if remainder.is_zero() {
                floor
            } else {
                floor + BigInt::from(1_u8)
            }
        }
        StepQuantize::Round => {
            if remainder * BigRational::from_integer(BigInt::from(2_u8)) >= BigRational::one() {
                floor + BigInt::from(1_u8)
            } else {
                floor
            }
        }
    };
    (step * BigRational::from_integer(units)).to_f64()
}

pub(super) fn precise_product(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(BigRational::one(), |product, value| {
            exact_rational(*value).map(|number| product * number)
        })?
        .to_f64()
}

pub(super) fn precise_sum(values: &[f64]) -> Option<f64> {
    values
        .iter()
        .try_fold(BigRational::zero(), |sum, value| {
            exact_rational(*value).map(|number| sum + number)
        })?
        .to_f64()
}

pub(super) fn precise_product_quotient(left: f64, right: f64, denominator: f64) -> Option<f64> {
    let denominator = exact_rational(denominator)?;
    if denominator.is_zero() {
        return None;
    }
    ft_precise_division(
        &(exact_rational(left)? * exact_rational(right)?),
        &denominator,
    )?
    .to_f64()
}

/// Reproduce CCXT `Precise.div(..., precision=18)`.
///
/// `FtPrecise` truncates every division toward zero to eighteen decimal
/// places. Keeping divisions as unlimited rationals looks more accurate, but
/// diverges from Freqtrade after a long sequence of weighted-basis updates.
pub(super) fn ft_precise_division(
    numerator: &BigRational,
    denominator: &BigRational,
) -> Option<BigRational> {
    if denominator.is_zero() {
        return None;
    }
    let value = numerator / denominator;
    let scale = BigInt::from(10_u8).pow(18);
    let scaled_integer = (value.numer() * &scale) / value.denom();
    Some(BigRational::new(scaled_integer, scale))
}

/// Convert the shortest round-trippable float text into an exact rational.
///
/// CCXT's `Precise`, and therefore Freqtrade's `FtPrecise`, performs decimal
/// string arithmetic. `rust_decimal` is used only to parse one f64 string
/// (at most 17 significant digits); multiplication and addition stay exact,
/// while `ft_precise_division` applies CCXT's explicit division boundary.
pub(super) fn exact_rational(value: f64) -> Option<BigRational> {
    if !value.is_finite() {
        return None;
    }
    let encoded = value.to_string();
    let decimal = Decimal::from_str(&encoded)
        .or_else(|_| Decimal::from_scientific(&encoded))
        .ok()?;
    let numerator = BigInt::from(decimal.mantissa());
    let denominator = BigInt::from(10_u8).pow(decimal.scale());
    Some(BigRational::new(numerator, denominator))
}

pub(super) fn round_eight(value: f64) -> f64 {
    // Freqtrade intentionally crosses a text boundary with
    // `float(f"{value:.8f}")`. Rust's numeric `round()` resolves exact halves
    // away from zero, while Python formatting uses ties-to-even. Formatting
    // here mirrors the pinned contract and also avoids a second rounding from
    // multiplying a large binary float by 1e8 first.
    format!("{value:.8}")
        .parse::<f64>()
        .expect("finite simulator profit formats as a finite decimal")
}

pub(super) fn pairwise_sum(values: &[f64]) -> f64 {
    const NUMPY_BLOCK_SIZE: usize = 128;
    if values.len() < 8 {
        return values.iter().fold(-0.0, |sum, value| sum + value);
    }
    if values.len() <= NUMPY_BLOCK_SIZE {
        // NumPy seeds eight independent lanes from the first eight values,
        // accumulates complete eight-value blocks lane by lane, and then
        // combines those lanes as a balanced tree. The grouping is observable
        // in Pandas' exported profit_total_abs.
        let mut accumulators = [
            values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7],
        ];
        let mut index = 8;
        while index + 7 < values.len() {
            for lane in 0..8 {
                accumulators[lane] += values[index + lane];
            }
            index += 8;
        }
        let mut result = ((accumulators[0] + accumulators[1])
            + (accumulators[2] + accumulators[3]))
            + ((accumulators[4] + accumulators[5]) + (accumulators[6] + accumulators[7]));
        while index < values.len() {
            result += values[index];
            index += 1;
        }
        return result;
    }
    let mut middle = values.len() / 2;
    middle -= middle % 8;
    pairwise_sum(&values[..middle]) + pairwise_sum(&values[middle..])
}

pub(super) fn python_float_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    // This is CPython 3.14's Neumaier compensated fast path for built-in
    // sum(float_iterable). Freqtrade 2026.5.1 runs on that interpreter, and
    // total_volume is exported without decimal rounding. Keeping this small
    // implementation local makes the parity rule explicit and testable.
    let mut high = 0.0;
    let mut low = 0.0;
    for value in values {
        let next = high + value;
        if high.abs() >= value.abs() {
            low += (high - next) + value;
        } else {
            low += (value - next) + high;
        }
        high = next;
    }
    if low != 0.0 && low.is_finite() {
        high + low
    } else {
        high
    }
}
