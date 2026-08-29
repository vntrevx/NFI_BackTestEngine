//! Side-effect-free scheduling, sizing, precision, and aggregation calculations.

use std::time::Duration;

use crate::domain::{OrderSide, PairSeries, PortfolioConfig, SimError};
use crate::portfolio::TradeSide;
use num_bigint::BigInt;
use num_rational::BigRational;
use num_traits::{One, ToPrimitive, Zero};

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

pub(super) fn available_stake_amount(
    free: f64,
    tied_up_stake: f64,
    ratio: f64,
) -> Result<f64, SimError> {
    let wallet_total = checked_float_sum(&[tied_up_stake, free], "available-stake-total")?;
    let total_stake_amount =
        checked_float_product(&[wallet_total, ratio], "available-stake-ratio")?;
    let available = checked_float_sum(
        &[total_stake_amount, -tied_up_stake],
        "available-stake-free",
    )?;
    Ok(available.min(free).max(0.0))
}

pub(super) fn entry_sizing(
    requested: f64,
    rate: f64,
    fee_rate: f64,
    amount_step: f64,
    leverage: f64,
) -> Result<Option<(f64, f64, f64, f64)>, SimError> {
    // Freqtrade treats the callback's stake as collateral/notional and derives
    // amount before accounting for fees (`stake / rate * leverage`). Fees
    // affect profit and wallet proceeds, but do not shrink the requested base
    // amount at entry.
    let raw_amount = requested * leverage / rate;
    let amount = floor_step(raw_amount, amount_step)?;
    if amount <= 0.0 {
        return Ok(None);
    }
    let notional = precise_product(&[amount, rate])?;
    // Freqtrade's precision layer represents the amount/rate product through
    // decimal text before storing collateral. Preserve that boundary here:
    // direct binary multiplication moves quote-free by one ULP for valid
    // Binance Futures entries such as 0.186 * 39858.3 / 3.
    let stake = if (leverage - 1.0).abs() < f64::EPSILON {
        notional
    } else {
        notional / leverage
    };
    let precise_cost = if (leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[amount, rate, 1.0 + fee_rate])?
    } else {
        let entry_fee = precise_product(&[notional, fee_rate])?;
        precise_sum(&[stake, entry_fee])?
    };
    let order_cost = checked_float_product(&[amount, rate, 1.0 + fee_rate], "entry-order-cost")?;
    if !stake.is_finite() {
        return Err(SimError::ExactArithmetic {
            operation: "entry-sizing",
        });
    }
    Ok(Some((amount, stake, precise_cost, order_cost)))
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

pub(super) fn floor_step(value: f64, step: f64) -> Result<f64, SimError> {
    exact_step_quantize(value, step, StepQuantize::Floor).ok_or(SimError::ExactArithmetic {
        operation: "floor-step",
    })
}

pub(super) fn ceil_step(value: f64, step: f64) -> Result<f64, SimError> {
    exact_step_quantize(value, step, StepQuantize::Ceil).ok_or(SimError::ExactArithmetic {
        operation: "ceil-step",
    })
}

pub(super) fn round_step(value: f64, step: f64) -> Result<f64, SimError> {
    exact_step_quantize(value, step, StepQuantize::Round).ok_or(SimError::ExactArithmetic {
        operation: "round-step",
    })
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
    finite_rational_to_f64(&(step * BigRational::from_integer(units)))
}

pub(super) fn checked_float_product(
    values: &[f64],
    operation: &'static str,
) -> Result<f64, SimError> {
    values.iter().try_fold(1.0, |product, value| {
        checked_finite(product * value, operation)
    })
}

pub(super) fn checked_float_sum(values: &[f64], operation: &'static str) -> Result<f64, SimError> {
    values
        .iter()
        .try_fold(0.0, |sum, value| checked_finite(sum + value, operation))
}

pub(super) fn checked_finite(value: f64, operation: &'static str) -> Result<f64, SimError> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(SimError::ExactArithmetic { operation })
    }
}

pub(super) fn precise_product(values: &[f64]) -> Result<f64, SimError> {
    let product = values
        .iter()
        .try_fold(BigRational::one(), |product, value| {
            exact_rational(*value).map(|number| product * number)
        })
        .ok_or(SimError::ExactArithmetic {
            operation: "precise-product",
        })?;
    finite_rational_to_f64(&product).ok_or(SimError::ExactArithmetic {
        operation: "precise-product",
    })
}

pub(super) fn precise_sum(values: &[f64]) -> Result<f64, SimError> {
    let sum = values
        .iter()
        .try_fold(BigRational::zero(), |sum, value| {
            exact_rational(*value).map(|number| sum + number)
        })
        .ok_or(SimError::ExactArithmetic {
            operation: "precise-sum",
        })?;
    finite_rational_to_f64(&sum).ok_or(SimError::ExactArithmetic {
        operation: "precise-sum",
    })
}

pub(super) fn precise_product_quotient(
    left: f64,
    right: f64,
    denominator: f64,
) -> Result<f64, SimError> {
    let failure = || SimError::ExactArithmetic {
        operation: "precise-product-quotient",
    };
    let denominator = exact_rational(denominator).ok_or_else(failure)?;
    if denominator.is_zero() {
        return Err(failure());
    }
    let quotient = ft_precise_division(
        &(exact_rational(left).ok_or_else(failure)? * exact_rational(right).ok_or_else(failure)?),
        &denominator,
    )
    .ok_or_else(failure)?;
    finite_rational_to_f64(&quotient).ok_or_else(failure)
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
/// string arithmetic. The shortest f64 text is parsed directly into an
/// unbounded integer ratio, including exponents outside fixed-decimal ranges;
/// `ft_precise_division` then applies CCXT's explicit division boundary.
pub(super) fn exact_rational(value: f64) -> Option<BigRational> {
    if !value.is_finite() {
        return None;
    }
    let encoded = value.to_string();
    let (mantissa, exponent) = if let Some((mantissa, exponent)) = encoded.split_once(['e', 'E']) {
        (mantissa, exponent.parse::<i32>().ok()?)
    } else {
        (encoded.as_str(), 0_i32)
    };
    let negative = mantissa.starts_with('-');
    let unsigned = mantissa.trim_start_matches(['-', '+']);
    let (whole, fraction) = if let Some(parts) = unsigned.split_once('.') {
        parts
    } else {
        (unsigned, "")
    };
    let digits = format!("{whole}{fraction}");
    let mut numerator = BigInt::parse_bytes(digits.as_bytes(), 10)?;
    if negative {
        numerator = -numerator;
    }
    let decimal_exponent = exponent.checked_sub(i32::try_from(fraction.len()).ok()?)?;
    if decimal_exponent >= 0 {
        numerator *= BigInt::from(10_u8).pow(u32::try_from(decimal_exponent).ok()?);
        Some(BigRational::from_integer(numerator))
    } else {
        let denominator = BigInt::from(10_u8).pow(decimal_exponent.unsigned_abs());
        Some(BigRational::new(numerator, denominator))
    }
}

fn finite_rational_to_f64(value: &BigRational) -> Option<f64> {
    value.to_f64().filter(|number| number.is_finite())
}

pub(super) fn round_eight(value: f64) -> Result<f64, SimError> {
    // Freqtrade intentionally crosses a text boundary with
    // `float(f"{value:.8f}")`. Rust's numeric `round()` resolves exact halves
    // away from zero, while Python formatting uses ties-to-even. Formatting
    // here mirrors the pinned contract and also avoids a second rounding from
    // multiplying a large binary float by 1e8 first.
    if !value.is_finite() {
        return Err(SimError::ExactArithmetic {
            operation: "round-eight",
        });
    }
    format!("{value:.8}")
        .parse::<f64>()
        .ok()
        .filter(|rounded| rounded.is_finite())
        .ok_or(SimError::ExactArithmetic {
            operation: "round-eight",
        })
}

#[cfg(test)]
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

pub(super) fn checked_pairwise_sum(
    values: &[f64],
    operation: &'static str,
) -> Result<f64, SimError> {
    const NUMPY_BLOCK_SIZE: usize = 128;
    if values.len() < 8 {
        return values
            .iter()
            .try_fold(-0.0, |sum, value| checked_finite(sum + value, operation));
    }
    if values.len() <= NUMPY_BLOCK_SIZE {
        let mut accumulators = [
            values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7],
        ];
        let mut index = 8;
        while index + 7 < values.len() {
            for lane in 0..8 {
                accumulators[lane] =
                    checked_finite(accumulators[lane] + values[index + lane], operation)?;
            }
            index += 8;
        }
        let left = checked_float_sum(
            &[
                checked_float_sum(&[accumulators[0], accumulators[1]], operation)?,
                checked_float_sum(&[accumulators[2], accumulators[3]], operation)?,
            ],
            operation,
        )?;
        let right = checked_float_sum(
            &[
                checked_float_sum(&[accumulators[4], accumulators[5]], operation)?,
                checked_float_sum(&[accumulators[6], accumulators[7]], operation)?,
            ],
            operation,
        )?;
        let mut result = checked_float_sum(&[left, right], operation)?;
        while index < values.len() {
            result = checked_finite(result + values[index], operation)?;
            index += 1;
        }
        return Ok(result);
    }
    let mut middle = values.len() / 2;
    middle -= middle % 8;
    checked_float_sum(
        &[
            checked_pairwise_sum(&values[..middle], operation)?,
            checked_pairwise_sum(&values[middle..], operation)?,
        ],
        operation,
    )
}

pub(super) fn checked_python_float_sum(
    values: impl IntoIterator<Item = f64>,
    operation: &'static str,
) -> Result<f64, SimError> {
    let mut high: f64 = 0.0;
    let mut low: f64 = 0.0;
    for value in values {
        let next = checked_finite(high + value, operation)?;
        let correction = if high.abs() >= value.abs() {
            checked_finite((high - next) + value, operation)?
        } else {
            checked_finite((value - next) + high, operation)?
        };
        low = checked_finite(low + correction, operation)?;
        high = next;
    }
    if low == 0.0 {
        Ok(high)
    } else {
        checked_finite(high + low, operation)
    }
}
