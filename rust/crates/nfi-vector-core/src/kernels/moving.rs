//! Exact scalar ports of the TA-Lib v0.6.4 moving kernels used by NFI.

use std::collections::VecDeque;

use serde_json::{Map, Value};

use crate::VectorCoreError;

const MAX_PERIOD: usize = 100_000;
type Bands = (Vec<f64>, Vec<f64>, Vec<f64>);

/// Bounded state for one exact TA-Lib-compatible moving kernel.
#[derive(Debug)]
pub(super) enum MovingStream {
    Sum {
        period: usize,
        total: f64,
        values: VecDeque<f64>,
        average: bool,
    },
    Ema {
        period: usize,
        count: usize,
        seed_total: f64,
        previous: Option<f64>,
    },
    Extreme {
        period: usize,
        seen: usize,
        values: VecDeque<f64>,
        maximum: bool,
    },
    Roc {
        period: usize,
        seen: usize,
        values: VecDeque<f64>,
    },
    Stddev {
        period: usize,
        total_one: f64,
        total_two: f64,
        values: VecDeque<f64>,
        nb_dev: f64,
    },
    Bbands {
        period: usize,
        total_one: f64,
        total_two: f64,
        values: VecDeque<f64>,
        nb_dev_up: f64,
        nb_dev_down: f64,
    },
}

/// Build bounded streaming state for a moving indicator, if it is supported.
pub(super) fn stream(
    name: &str,
    arguments: &Map<String, Value>,
) -> Result<Option<MovingStream>, VectorCoreError> {
    let state = match name {
        "SMA" => MovingStream::Sum {
            period: argument_period(arguments, 30, "SMA")?,
            total: 0.0,
            values: VecDeque::new(),
            average: true,
        },
        "SUM" => MovingStream::Sum {
            period: argument_period(arguments, 30, "SUM")?,
            total: 0.0,
            values: VecDeque::new(),
            average: false,
        },
        "EMA" => MovingStream::Ema {
            period: argument_period(arguments, 30, "EMA")?,
            count: 0,
            seed_total: 0.0,
            previous: None,
        },
        "MIN" => MovingStream::Extreme {
            period: argument_period(arguments, 30, "MIN")?,
            seen: 0,
            values: VecDeque::new(),
            maximum: false,
        },
        "MAX" => MovingStream::Extreme {
            period: argument_period(arguments, 30, "MAX")?,
            seen: 0,
            values: VecDeque::new(),
            maximum: true,
        },
        "ROC" => MovingStream::Roc {
            period: argument_period_minimum(arguments, 10, "ROC", 1)?,
            seen: 0,
            values: VecDeque::new(),
        },
        "STDDEV" => MovingStream::Stddev {
            period: argument_period(arguments, 5, "STDDEV")?,
            total_one: 0.0,
            total_two: 0.0,
            values: VecDeque::new(),
            nb_dev: argument_number(arguments, "nbdev", 1.0)?,
        },
        "BBANDS" => {
            if argument_integer(arguments, "matype", 0)? != 0 {
                return Err(VectorCoreError::InvalidProgram(
                    "BBANDS only supports TA-Lib SMA matype 0".to_owned(),
                ));
            }
            MovingStream::Bbands {
                period: argument_period(arguments, 5, "BBANDS")?,
                total_one: 0.0,
                total_two: 0.0,
                values: VecDeque::new(),
                nb_dev_up: argument_number(arguments, "nbdevup", 2.0)?,
                nb_dev_down: argument_number(arguments, "nbdevdn", 2.0)?,
            }
        }
        _ => return Ok(None),
    };
    Ok(Some(state))
}

impl MovingStream {
    /// Process exactly the supplied rows and return one same-length output per column.
    pub(super) fn execute(&mut self, inputs: &[&[f64]]) -> Result<Vec<Vec<f64>>, VectorCoreError> {
        let values = single_stream_input(inputs)?;
        match self {
            Self::Bbands { .. } => {
                let mut upper = Vec::with_capacity(values.len());
                let mut middle = Vec::with_capacity(values.len());
                let mut lower = Vec::with_capacity(values.len());
                for value in values {
                    let (next_upper, next_middle, next_lower) = self.next_bbands(*value);
                    upper.push(next_upper);
                    middle.push(next_middle);
                    lower.push(next_lower);
                }
                Ok(vec![upper, middle, lower])
            }
            _ => Ok(vec![values
                .iter()
                .map(|value| self.next_single(*value))
                .collect()]),
        }
    }

    /// Number of historical values retained for cross-batch exactness.
    pub(super) fn retained(&self) -> usize {
        match self {
            Self::Sum { values, .. }
            | Self::Extreme { values, .. }
            | Self::Roc { values, .. }
            | Self::Stddev { values, .. }
            | Self::Bbands { values, .. } => values.len(),
            Self::Ema { .. } => 0,
        }
    }

    fn next_single(&mut self, value: f64) -> f64 {
        match self {
            Self::Sum {
                period,
                total,
                values,
                average,
            } => next_sum(value, *period, total, values, *average),
            Self::Ema {
                period,
                count,
                seed_total,
                previous,
            } => next_ema(value, *period, count, seed_total, previous),
            Self::Extreme {
                period,
                seen,
                values,
                maximum,
            } => next_extreme(value, *period, seen, values, *maximum),
            Self::Roc {
                period,
                seen,
                values,
            } => next_roc(value, *period, seen, values),
            Self::Stddev {
                period,
                total_one,
                total_two,
                values,
                nb_dev,
            } => next_stddev(value, *period, total_one, total_two, values, *nb_dev),
            Self::Bbands { .. } => unreachable!("BBANDS has three outputs"),
        }
    }

    fn next_bbands(&mut self, value: f64) -> BandsRow {
        let Self::Bbands {
            period,
            total_one,
            total_two,
            values,
            nb_dev_up,
            nb_dev_down,
        } = self
        else {
            unreachable!("only BBANDS has three outputs");
        };
        next_bbands(
            value,
            *period,
            total_one,
            total_two,
            values,
            *nb_dev_up,
            *nb_dev_down,
        )
    }
}

type BandsRow = (f64, f64, f64);

fn argument_period(
    arguments: &Map<String, Value>,
    default: usize,
    name: &str,
) -> Result<usize, VectorCoreError> {
    argument_period_minimum(arguments, default, name, 2)
}

fn argument_period_minimum(
    arguments: &Map<String, Value>,
    default: usize,
    name: &str,
    minimum: usize,
) -> Result<usize, VectorCoreError> {
    let period = argument_integer(arguments, "timeperiod", default)?;
    validate_period(period, minimum, name)?;
    Ok(period)
}

fn argument_integer(
    arguments: &Map<String, Value>,
    name: &str,
    default: usize,
) -> Result<usize, VectorCoreError> {
    let Some(value) = arguments.get(name) else {
        return Ok(default);
    };
    value
        .as_u64()
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!(
                "moving kernel argument {name} must be an integer"
            ))
        })
}

fn argument_number(
    arguments: &Map<String, Value>,
    name: &str,
    default: f64,
) -> Result<f64, VectorCoreError> {
    let Some(value) = arguments.get(name) else {
        return Ok(default);
    };
    value.as_f64().ok_or_else(|| {
        VectorCoreError::InvalidProgram(format!("moving kernel argument {name} must be numeric"))
    })
}

fn single_stream_input<'a>(inputs: &'a [&[f64]]) -> Result<&'a [f64], VectorCoreError> {
    match inputs {
        [values] => Ok(values),
        _ => Err(VectorCoreError::InvalidProgram(
            "moving stream requires exactly one input column".to_owned(),
        )),
    }
}

fn next_sum(
    value: f64,
    period: usize,
    total: &mut f64,
    values: &mut VecDeque<f64>,
    average: bool,
) -> f64 {
    *total += value;
    values.push_back(value);
    if values.len() < period {
        return f64::NAN;
    }
    let current = *total;
    let trailing = values
        .pop_front()
        .expect("full rolling sum has a trailing value");
    *total -= trailing;
    if average {
        current / period_as_f64(period)
    } else {
        current
    }
}

fn next_ema(
    value: f64,
    period: usize,
    count: &mut usize,
    seed_total: &mut f64,
    previous: &mut Option<f64>,
) -> f64 {
    if *count < period - 1 {
        *count += 1;
        *seed_total += value;
        return f64::NAN;
    }
    if *count == period - 1 {
        *count += 1;
        *seed_total += value;
        let seed = *seed_total / period_as_f64(period);
        *previous = Some(seed);
        return seed;
    }
    let previous_value = previous.as_mut().expect("EMA seed exists after warmup");
    let k = 2.0 / (period_as_f64(period) + 1.0);
    *previous_value = ((value - *previous_value) * k) + *previous_value;
    *previous_value
}

fn next_extreme(
    value: f64,
    period: usize,
    seen: &mut usize,
    values: &mut VecDeque<f64>,
    maximum: bool,
) -> f64 {
    *seen = (*seen + 1).min(period);
    values.push_back(value);
    if *seen < period {
        return f64::NAN;
    }
    let mut extreme = values[0];
    for candidate in values.iter().skip(1) {
        if if maximum {
            *candidate > extreme
        } else {
            *candidate < extreme
        } {
            extreme = *candidate;
        }
    }
    values.pop_front();
    extreme
}

fn next_roc(value: f64, period: usize, seen: &mut usize, values: &mut VecDeque<f64>) -> f64 {
    let output = if *seen < period {
        f64::NAN
    } else {
        let previous = values[0];
        if exact_equal(previous, 0.0) {
            0.0
        } else {
            ((value / previous) - 1.0) * 100.0
        }
    };
    *seen = (*seen + 1).min(period);
    values.push_back(value);
    if values.len() > period {
        values.pop_front();
    }
    output
}

fn next_stddev(
    value: f64,
    period: usize,
    total_one: &mut f64,
    total_two: &mut f64,
    values: &mut VecDeque<f64>,
    nb_dev: f64,
) -> f64 {
    let Some((variance, _)) = next_variance(value, period, total_one, total_two, values) else {
        return f64::NAN;
    };
    if variance > 0.0 {
        if is_one(nb_dev) {
            variance.sqrt()
        } else {
            variance.sqrt() * nb_dev
        }
    } else {
        0.0
    }
}

fn next_bbands(
    value: f64,
    period: usize,
    total_one: &mut f64,
    total_two: &mut f64,
    values: &mut VecDeque<f64>,
    nb_dev_up: f64,
    nb_dev_down: f64,
) -> BandsRow {
    let Some((variance, mean)) = next_variance(value, period, total_one, total_two, values) else {
        return (f64::NAN, f64::NAN, f64::NAN);
    };
    let deviation = if variance > 0.0 { variance.sqrt() } else { 0.0 };
    if exact_equal(nb_dev_up, nb_dev_down) {
        if is_one(nb_dev_up) {
            (mean + deviation, mean, mean - deviation)
        } else {
            let scaled = deviation * nb_dev_up;
            (mean + scaled, mean, mean - scaled)
        }
    } else if is_one(nb_dev_up) {
        (mean + deviation, mean, mean - (deviation * nb_dev_down))
    } else if is_one(nb_dev_down) {
        (mean + (deviation * nb_dev_up), mean, mean - deviation)
    } else {
        (
            mean + (deviation * nb_dev_up),
            mean,
            mean - (deviation * nb_dev_down),
        )
    }
}

fn next_variance(
    value: f64,
    period: usize,
    total_one: &mut f64,
    total_two: &mut f64,
    values: &mut VecDeque<f64>,
) -> Option<(f64, f64)> {
    *total_one += value;
    *total_two += value * value;
    values.push_back(value);
    if values.len() < period {
        return None;
    }
    let mean_one = *total_one / period_as_f64(period);
    let mean_two = *total_two / period_as_f64(period);
    let trailing = values
        .pop_front()
        .expect("full rolling variance has a trailing value");
    *total_one -= trailing;
    *total_two -= trailing * trailing;
    Some((mean_two - (mean_one * mean_one), mean_one))
}

pub(super) fn sma(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 2, "SMA")?;
    Ok(rolling_sum(values, period, true))
}

pub(super) fn ema(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 2, "EMA")?;
    let mut output = warmup(values.len());
    if values.len() < period {
        return Ok(output);
    }
    let mut total = 0.0;
    for value in &values[..period] {
        total += value;
    }
    let period_as_f64 = period_as_f64(period);
    let k = 2.0 / (period_as_f64 + 1.0);
    let mut previous = total / period_as_f64;
    output[period - 1] = previous;
    for (index, value) in values.iter().enumerate().skip(period) {
        previous = ((*value - previous) * k) + previous;
        output[index] = previous;
    }
    Ok(output)
}

pub(super) fn min(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    rolling_extreme(values, period, false)
}

pub(super) fn max(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    rolling_extreme(values, period, true)
}

pub(super) fn sum(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 2, "SUM")?;
    Ok(rolling_sum(values, period, false))
}

pub(super) fn roc(values: &[f64], period: usize) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 1, "ROC")?;
    let mut output = warmup(values.len());
    for index in period..values.len() {
        let previous = values[index - period];
        output[index] = if exact_equal(previous, 0.0) {
            0.0
        } else {
            ((values[index] / previous) - 1.0) * 100.0
        };
    }
    Ok(output)
}

pub(super) fn stddev(
    values: &[f64],
    period: usize,
    nb_dev: f64,
) -> Result<Vec<f64>, VectorCoreError> {
    stddev_with_nbdev(values, period, nb_dev)
}

pub(super) fn bbands_sma(
    values: &[f64],
    period: usize,
    nb_dev_up: f64,
    nb_dev_down: f64,
) -> Result<Bands, VectorCoreError> {
    validate_period(period, 2, "BBANDS")?;
    let middle = sma(values, period)?;
    let deviations = stddev_using_precalculated_sma(values, &middle, period);
    let mut upper = warmup(values.len());
    let mut lower = warmup(values.len());
    for index in (period - 1)..values.len() {
        let deviation = deviations[index];
        let average = middle[index];
        if exact_equal(nb_dev_up, nb_dev_down) {
            if is_one(nb_dev_up) {
                upper[index] = average + deviation;
                lower[index] = average - deviation;
            } else {
                let scaled = deviation * nb_dev_up;
                upper[index] = average + scaled;
                lower[index] = average - scaled;
            }
        } else if is_one(nb_dev_up) {
            upper[index] = average + deviation;
            lower[index] = average - (deviation * nb_dev_down);
        } else if is_one(nb_dev_down) {
            lower[index] = average - deviation;
            upper[index] = average + (deviation * nb_dev_up);
        } else {
            upper[index] = average + (deviation * nb_dev_up);
            lower[index] = average - (deviation * nb_dev_down);
        }
    }
    Ok((upper, middle, lower))
}

fn validate_period(period: usize, minimum: usize, name: &str) -> Result<(), VectorCoreError> {
    if !(minimum..=MAX_PERIOD).contains(&period) {
        return Err(VectorCoreError::InvalidProgram(format!(
            "{name} timeperiod must be in {minimum}..={MAX_PERIOD}"
        )));
    }
    Ok(())
}

fn warmup(length: usize) -> Vec<f64> {
    vec![f64::NAN; length]
}

/// TA-Lib permits periods through 100,000, so this cast is exact on all targets.
#[allow(clippy::cast_precision_loss)]
fn period_as_f64(period: usize) -> f64 {
    period as f64
}

/// TA-Lib selects optimized band branches with exact IEEE equality.
#[allow(clippy::float_cmp)]
fn exact_equal(left: f64, right: f64) -> bool {
    left == right
}

fn is_one(value: f64) -> bool {
    exact_equal(value, 1.0)
}

fn rolling_sum(values: &[f64], period: usize, average: bool) -> Vec<f64> {
    let mut output = warmup(values.len());
    if values.len() < period {
        return output;
    }
    let mut total = 0.0;
    let mut trailing = 0;
    for value in &values[..period - 1] {
        total += value;
    }
    for today in (period - 1)..values.len() {
        total += values[today];
        let current = total;
        total -= values[trailing];
        output[today] = if average {
            current / period_as_f64(period)
        } else {
            current
        };
        trailing += 1;
    }
    output
}

fn rolling_extreme(
    values: &[f64],
    period: usize,
    maximum: bool,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 2, if maximum { "MAX" } else { "MIN" })?;
    let mut output = warmup(values.len());
    if values.len() < period {
        return Ok(output);
    }
    let mut extreme_index = None;
    let mut extreme = 0.0;
    for (trailing, today) in ((period - 1)..values.len()).enumerate() {
        let current = values[today];
        if extreme_index.is_none_or(|index| index < trailing) {
            extreme_index = Some(trailing);
            extreme = values[trailing];
            for (index, candidate) in values.iter().enumerate().take(today + 1).skip(trailing + 1) {
                if if maximum {
                    *candidate > extreme
                } else {
                    *candidate < extreme
                } {
                    extreme_index = Some(index);
                    extreme = *candidate;
                }
            }
        } else if if maximum {
            current >= extreme
        } else {
            current <= extreme
        } {
            extreme_index = Some(today);
            extreme = current;
        }
        output[today] = extreme;
    }
    Ok(output)
}

fn stddev_with_nbdev(
    values: &[f64],
    period: usize,
    nb_dev: f64,
) -> Result<Vec<f64>, VectorCoreError> {
    validate_period(period, 2, "STDDEV")?;
    let mut output = variance(values, period);
    for value in output.iter_mut().skip(period - 1) {
        if *value > 0.0 {
            *value = if is_one(nb_dev) {
                value.sqrt()
            } else {
                value.sqrt() * nb_dev
            };
        } else {
            *value = 0.0;
        }
    }
    Ok(output)
}

fn variance(values: &[f64], period: usize) -> Vec<f64> {
    let mut output = warmup(values.len());
    if values.len() < period {
        return output;
    }
    let mut total_one = 0.0;
    let mut total_two = 0.0;
    for value in &values[..period - 1] {
        total_one += value;
        total_two += value * value;
    }
    for (trailing, today) in ((period - 1)..values.len()).enumerate() {
        let value = values[today];
        total_one += value;
        total_two += value * value;
        let mean_one = total_one / period_as_f64(period);
        let mean_two = total_two / period_as_f64(period);
        let value = values[trailing];
        total_one -= value;
        total_two -= value * value;
        output[today] = mean_two - (mean_one * mean_one);
    }
    output
}

fn stddev_using_precalculated_sma(values: &[f64], averages: &[f64], period: usize) -> Vec<f64> {
    let mut output = warmup(values.len());
    if values.len() < period {
        return output;
    }
    let mut total_two = 0.0;
    for value in &values[..period - 1] {
        total_two += value * value;
    }
    for (trailing, index) in ((period - 1)..values.len()).enumerate() {
        let value = values[index];
        total_two += value * value;
        let mut mean_two = total_two / period_as_f64(period);
        let value = values[trailing];
        total_two -= value * value;
        mean_two -= averages[index] * averages[index];
        output[index] = if mean_two > 0.0 { mean_two.sqrt() } else { 0.0 };
    }
    output
}

#[cfg(test)]
mod tests {
    use serde_json::{json, Map, Value};

    use super::*;

    fn assert_bits(actual: &[f64], expected: &[f64]) {
        assert_eq!(actual.len(), expected.len());
        for (actual, expected) in actual.iter().zip(expected) {
            assert_eq!(actual.to_bits(), expected.to_bits());
        }
    }

    #[test]
    fn moving_averages_and_sum_match_talib_order() {
        let values = [1.0, 2.0, 3.0, 4.0, 5.0];
        assert_bits(
            &sma(&values, 3).expect("valid SMA"),
            &[f64::NAN, f64::NAN, 2.0, 3.0, 4.0],
        );
        assert_bits(
            &ema(&values, 3).expect("valid EMA"),
            &[f64::NAN, f64::NAN, 2.0, 3.0, 4.0],
        );
        assert_bits(
            &sum(&values, 3).expect("valid SUM"),
            &[f64::NAN, f64::NAN, 6.0, 9.0, 12.0],
        );
    }

    #[test]
    fn extrema_and_roc_preserve_talib_comparisons_and_zero_rule() {
        let values = [3.0, 1.0, 2.0, 4.0, 0.0];
        assert_bits(
            &min(&values, 3).expect("valid MIN"),
            &[f64::NAN, f64::NAN, 1.0, 1.0, 0.0],
        );
        assert_bits(
            &max(&values, 3).expect("valid MAX"),
            &[f64::NAN, f64::NAN, 3.0, 4.0, 4.0],
        );
        assert_bits(
            &roc(&[0.0, 2.0, 4.0], 1).expect("valid ROC"),
            &[f64::NAN, 0.0, 100.0],
        );
    }

    #[test]
    fn stddev_and_sma_bbands_match_talib_variance_path() {
        let values = [1.0, 2.0, 3.0, 4.0, 5.0];
        assert_bits(
            &stddev(&values, 3, 1.0).expect("valid STDDEV"),
            &[
                f64::NAN,
                f64::NAN,
                f64::from_bits(0x3fea_20bd_700c_2c40),
                f64::from_bits(0x3fea_20bd_700c_2c3b),
                f64::from_bits(0x3fea_20bd_700c_2c45),
            ],
        );
        let (upper, middle, lower) = bbands_sma(&values, 3, 2.0, 2.0).expect("valid BBANDS");
        assert_bits(&middle, &[f64::NAN, f64::NAN, 2.0, 3.0, 4.0]);
        assert_bits(
            &upper,
            &[
                f64::NAN,
                f64::NAN,
                f64::from_bits(0x400d_105e_b806_1620),
                f64::from_bits(0x4012_882f_5c03_0b0f),
                f64::from_bits(0x4016_882f_5c03_0b11),
            ],
        );
        assert_bits(
            &lower,
            &[
                f64::NAN,
                f64::NAN,
                f64::from_bits(0x3fd7_7d0a_3fcf_4f00),
                f64::from_bits(0x3ff5_df42_8ff3_d3c5),
                f64::from_bits(0x4002_efa1_47f9_e9de),
            ],
        );
    }

    #[test]
    fn invalid_periods_fail_closed() {
        assert!(sma(&[1.0], 1).is_err());
        assert!(roc(&[1.0], 0).is_err());
        assert!(bbands_sma(&[1.0], MAX_PERIOD + 1, 2.0, 2.0).is_err());
    }

    #[test]
    fn streams_match_batch_kernels_across_arbitrary_boundaries() {
        let values = [
            3.0, 1.0, 4.0, 1.5, 9.0, 2.0, 6.0, 5.0, 3.5, 5.5, 8.0, 9.5, 7.0,
        ];
        let period = 3;
        let defaults = arguments(&json!({"timeperiod": 3}));
        assert_stream(
            "SMA",
            &defaults,
            &values,
            &[sma(&values, period).expect("SMA")],
            period,
        );
        assert_stream(
            "EMA",
            &defaults,
            &values,
            &[ema(&values, period).expect("EMA")],
            period,
        );
        assert_stream(
            "MIN",
            &defaults,
            &values,
            &[min(&values, period).expect("MIN")],
            period,
        );
        assert_stream(
            "MAX",
            &defaults,
            &values,
            &[max(&values, period).expect("MAX")],
            period,
        );
        assert_stream(
            "SUM",
            &defaults,
            &values,
            &[sum(&values, period).expect("SUM")],
            period,
        );
        assert_stream(
            "ROC",
            &defaults,
            &values,
            &[roc(&values, period).expect("ROC")],
            period,
        );
        let stddev_arguments = arguments(&json!({"timeperiod": 3, "nbdev": 1.75}));
        assert_stream(
            "STDDEV",
            &stddev_arguments,
            &values,
            &[stddev(&values, period, 1.75).expect("STDDEV")],
            period,
        );
        let bbands_arguments = arguments(&json!({
            "timeperiod": 3,
            "nbdevup": 2.0,
            "nbdevdn": 1.5,
            "matype": 0,
        }));
        let (upper, middle, lower) = bbands_sma(&values, period, 2.0, 1.5).expect("BBANDS");
        assert_stream(
            "BBANDS",
            &bbands_arguments,
            &values,
            &[upper, middle, lower],
            period,
        );
    }

    #[test]
    fn streaming_rejects_wrong_input_arity_and_unsupported_bbands_matype() {
        let mut state = stream("SMA", &arguments(&json!({"timeperiod": 3})))
            .expect("valid stream")
            .expect("moving stream");
        assert!(state.execute(&[]).is_err());
        assert!(state.execute(&[&[1.0][..], &[1.0][..]]).is_err());
        assert!(stream("BBANDS", &arguments(&json!({"matype": 1}))).is_err());
        assert!(stream("RSI", &Map::new())
            .expect("supported lookup")
            .is_none());
    }

    fn arguments(value: &Value) -> Map<String, Value> {
        value.as_object().expect("argument object").clone()
    }

    fn assert_stream(
        name: &str,
        arguments: &Map<String, Value>,
        values: &[f64],
        expected: &[Vec<f64>],
        period: usize,
    ) {
        let mut state = stream(name, arguments)
            .expect("valid moving stream")
            .expect("moving operation");
        let mut actual = vec![Vec::new(); expected.len()];
        let mut start = 0;
        for requested in [1, 2, 1, 3, 1, 5] {
            if start == values.len() {
                break;
            }
            let end = (start + requested).min(values.len());
            let output = state.execute(&[&values[start..end]]).expect("stream batch");
            for (actual, output) in actual.iter_mut().zip(output) {
                actual.extend(output);
            }
            assert!(state.retained() <= period);
            start = end;
        }
        assert_eq!(start, values.len());
        for (actual, expected) in actual.iter().zip(expected) {
            assert_bits(actual, expected);
        }
    }
}
