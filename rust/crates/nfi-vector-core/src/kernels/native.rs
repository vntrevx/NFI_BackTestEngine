//! Bounded native kernels for causal array operations used by NFI indicators.

use std::collections::VecDeque;

use crate::float::{binary, canonicalize, BinaryFloatOp, CANONICAL_NAN_BITS};
use crate::VectorCoreError;

use super::moving::{sum_stream, MovingStream};

const MAX_PERIOD: usize = 100_000;
const HOUR_MS: i64 = 3_600_000;
const DAY_MS: i64 = 86_400_000;

/// Bounded prefix-sum state for Chaikin money flow.
#[derive(Debug)]
pub struct ChaikinMoneyFlowStream {
    period: usize,
    prefix_mfv: f64,
    prefix_volume: f64,
    prefixes: VecDeque<(f64, f64)>,
}

impl ChaikinMoneyFlowStream {
    /// Creates a causal Chaikin money-flow stream.
    ///
    /// # Errors
    ///
    /// Returns an error when `timeperiod` is zero or exceeds the bounded kernel limit.
    pub fn new(timeperiod: usize) -> Result<Self, VectorCoreError> {
        validate_period(timeperiod, "Chaikin money flow")?;
        Ok(Self {
            period: timeperiod,
            prefix_mfv: 0.0,
            prefix_volume: 0.0,
            prefixes: VecDeque::with_capacity(timeperiod),
        })
    }

    /// Processes one ordered chunk and returns only that chunk's CMF rows.
    ///
    /// # Errors
    ///
    /// Returns an error when the four input columns have different lengths.
    #[allow(clippy::float_cmp)] // NumPy's exact zero mask is part of the source contract.
    pub fn execute(
        &mut self,
        high: &[f64],
        low: &[f64],
        close: &[f64],
        volume: &[f64],
    ) -> Result<Vec<f64>, VectorCoreError> {
        validate_equal_lengths("Chaikin money flow", &[high, low, close, volume])?;
        let mut output = Vec::with_capacity(high.len());
        for (((&high, &low), &close), &volume) in high.iter().zip(low).zip(close).zip(volume) {
            let range = binary(high, low, BinaryFloatOp::Subtract);
            let multiplier = if range == 0.0 {
                0.0
            } else {
                let close_minus_low = binary(close, low, BinaryFloatOp::Subtract);
                let high_minus_close = binary(high, close, BinaryFloatOp::Subtract);
                let numerator = binary(close_minus_low, high_minus_close, BinaryFloatOp::Subtract);
                binary(numerator, range, BinaryFloatOp::Divide)
            };
            let mfv = nan_to_num(binary(multiplier, volume, BinaryFloatOp::Multiply));
            let clean_volume = nan_to_num(volume);
            self.prefix_mfv = binary(self.prefix_mfv, mfv, BinaryFloatOp::Add);
            self.prefix_volume = binary(self.prefix_volume, clean_volume, BinaryFloatOp::Add);
            self.prefixes
                .push_back((self.prefix_mfv, self.prefix_volume));

            let sums = match self.prefixes.len().cmp(&self.period) {
                std::cmp::Ordering::Less => None,
                std::cmp::Ordering::Equal => Some((self.prefix_mfv, self.prefix_volume)),
                std::cmp::Ordering::Greater => {
                    let Some((old_mfv, old_volume)) = self.prefixes.pop_front() else {
                        return Err(VectorCoreError::InvalidState(
                            "Chaikin money-flow prefix state is empty".to_owned(),
                        ));
                    };
                    Some((
                        binary(self.prefix_mfv, old_mfv, BinaryFloatOp::Subtract),
                        binary(self.prefix_volume, old_volume, BinaryFloatOp::Subtract),
                    ))
                }
            };
            output.push(sums.map_or_else(canonical_nan, |(mfv_sum, volume_sum)| {
                if volume_sum == 0.0 {
                    canonical_nan()
                } else {
                    binary(mfv_sum, volume_sum, BinaryFloatOp::Divide)
                }
            }));
        }
        Ok(output)
    }

    /// Returns the number of historical prefix rows retained by the stream.
    #[must_use]
    pub fn retained(&self) -> usize {
        self.prefixes.len()
    }
}

/// Executes Chaikin money flow over one complete batch.
///
/// # Errors
///
/// Returns an error for an invalid period or unequal input lengths.
pub fn chaikin_money_flow(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    volume: &[f64],
    timeperiod: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    ChaikinMoneyFlowStream::new(timeperiod)?.execute(high, low, close, volume)
}

/// Bounded state for the older X7 Chaikin helper, whose volume denominator
/// uses TA-Lib `SUM` instead of `NumPy`'s NaN-to-zero prefix sum.
#[derive(Debug)]
pub struct LegacyChaikinMoneyFlowStream {
    period: usize,
    prefix_mfv: f64,
    mfv_prefixes: VecDeque<f64>,
    volume_sum: MovingStream,
}

impl LegacyChaikinMoneyFlowStream {
    /// Creates the exact legacy Chaikin stream.
    ///
    /// # Errors
    ///
    /// Returns an error when `timeperiod` is outside the bounded SUM contract.
    pub fn new(timeperiod: usize) -> Result<Self, VectorCoreError> {
        Ok(Self {
            period: timeperiod,
            prefix_mfv: 0.0,
            mfv_prefixes: VecDeque::with_capacity(timeperiod),
            volume_sum: sum_stream(timeperiod)?,
        })
    }

    /// Processes one ordered chunk with the legacy mixed NumPy/TA-Lib contract.
    ///
    /// # Errors
    ///
    /// Returns an error when the four input columns have different lengths.
    #[allow(clippy::float_cmp)] // The source uses exact NumPy zero comparisons.
    pub fn execute(
        &mut self,
        high: &[f64],
        low: &[f64],
        close: &[f64],
        volume: &[f64],
    ) -> Result<Vec<f64>, VectorCoreError> {
        validate_equal_lengths("legacy Chaikin money flow", &[high, low, close, volume])?;
        let volume_sums = self.volume_sum.execute(&[volume])?;
        let volume_sums = volume_sums.first().ok_or_else(|| {
            VectorCoreError::InvalidState("legacy Chaikin volume SUM returned no output".to_owned())
        })?;
        let mut output = Vec::with_capacity(high.len());
        for (row, (((&high, &low), &close), &volume)) in
            high.iter().zip(low).zip(close).zip(volume).enumerate()
        {
            let range = binary(high, low, BinaryFloatOp::Subtract);
            let multiplier = if range == 0.0 {
                0.0
            } else {
                let close_minus_low = binary(close, low, BinaryFloatOp::Subtract);
                let high_minus_close = binary(high, close, BinaryFloatOp::Subtract);
                let numerator = binary(close_minus_low, high_minus_close, BinaryFloatOp::Subtract);
                binary(numerator, range, BinaryFloatOp::Divide)
            };
            let mfv = nan_to_num(binary(multiplier, volume, BinaryFloatOp::Multiply));
            self.prefix_mfv = binary(self.prefix_mfv, mfv, BinaryFloatOp::Add);
            self.mfv_prefixes.push_back(self.prefix_mfv);
            let mfv_sum = match self.mfv_prefixes.len().cmp(&self.period) {
                std::cmp::Ordering::Less => None,
                std::cmp::Ordering::Equal => Some(self.prefix_mfv),
                std::cmp::Ordering::Greater => {
                    let old = self.mfv_prefixes.pop_front().ok_or_else(|| {
                        VectorCoreError::InvalidState(
                            "legacy Chaikin prefix state is empty".to_owned(),
                        )
                    })?;
                    Some(binary(self.prefix_mfv, old, BinaryFloatOp::Subtract))
                }
            };
            output.push(mfv_sum.map_or_else(canonical_nan, |mfv_sum| {
                let volume_sum = volume_sums[row];
                if volume_sum == 0.0 {
                    canonical_nan()
                } else {
                    binary(mfv_sum, volume_sum, BinaryFloatOp::Divide)
                }
            }));
        }
        Ok(output)
    }

    /// Number of retained rolling values across both numerator and denominator.
    #[must_use]
    pub fn retained(&self) -> usize {
        self.mfv_prefixes
            .len()
            .saturating_add(self.volume_sum.retained())
    }
}

/// Executes the legacy Chaikin helper over one complete batch.
///
/// # Errors
///
/// Returns an error for an invalid period or unequal input lengths.
pub fn legacy_chaikin_money_flow(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    volume: &[f64],
    timeperiod: usize,
) -> Result<Vec<f64>, VectorCoreError> {
    LegacyChaikinMoneyFlowStream::new(timeperiod)?.execute(high, low, close, volume)
}

/// One-row state for percentage change with an exact zero-denominator guard.
#[derive(Debug, Default)]
pub struct SafePercentChangeStream {
    previous: Option<f64>,
}

impl SafePercentChangeStream {
    /// Creates an empty percentage-change stream.
    #[must_use]
    pub const fn new() -> Self {
        Self { previous: None }
    }

    /// Processes a chunk using subtract, divide, then multiply order.
    #[must_use]
    #[allow(clippy::float_cmp)] // NumPy `where=prev != 0` uses exact IEEE equality.
    pub fn execute(&mut self, values: &[f64]) -> Vec<f64> {
        values
            .iter()
            .map(|&current| {
                let result = self.previous.map_or_else(canonical_nan, |previous| {
                    if previous == 0.0 {
                        canonical_nan()
                    } else {
                        let difference = binary(current, previous, BinaryFloatOp::Subtract);
                        let ratio = binary(difference, previous, BinaryFloatOp::Divide);
                        binary(ratio, 100.0, BinaryFloatOp::Multiply)
                    }
                });
                self.previous = Some(current);
                result
            })
            .collect()
    }

    /// Returns the number of previous rows retained by the stream.
    #[must_use]
    pub fn retained(&self) -> usize {
        usize::from(self.previous.is_some())
    }
}

/// Executes safe consecutive-row percentage change over one batch.
#[must_use]
pub fn safe_percent_change(values: &[f64]) -> Vec<f64> {
    SafePercentChangeStream::new().execute(values)
}

/// One-row state for absolute consecutive differences.
#[derive(Debug, Default)]
pub struct AbsoluteDifferenceStream {
    previous: Option<f64>,
}

impl AbsoluteDifferenceStream {
    /// Creates an empty absolute-difference stream.
    #[must_use]
    pub const fn new() -> Self {
        Self { previous: None }
    }

    /// Processes a chunk using exact subtract-then-absolute-value order.
    #[must_use]
    pub fn execute(&mut self, values: &[f64]) -> Vec<f64> {
        values
            .iter()
            .map(|&current| {
                let result = self.previous.map_or_else(canonical_nan, |previous| {
                    canonicalize(binary(current, previous, BinaryFloatOp::Subtract).abs())
                });
                self.previous = Some(current);
                result
            })
            .collect()
    }

    /// Returns the number of previous rows retained by the stream.
    #[must_use]
    pub fn retained(&self) -> usize {
        usize::from(self.previous.is_some())
    }
}

/// Executes absolute consecutive-row differences over one batch.
#[must_use]
pub fn absolute_difference(values: &[f64]) -> Vec<f64> {
    AbsoluteDifferenceStream::new().execute(values)
}

/// Two projected UTC-day opening-range columns.
#[derive(Clone, Debug, PartialEq)]
pub struct UtcOpeningRangeOutput {
    /// Maximum high observed before the configured UTC cutoff.
    pub high: Vec<f64>,
    /// Minimum low observed before the configured UTC cutoff.
    pub low: Vec<f64>,
}

/// Bounded current-day state for a UTC opening range.
#[derive(Debug)]
pub struct UtcOpeningRangeStream {
    cutoff_ms: i64,
    current_day: Option<i64>,
    range_high: Option<f64>,
    range_low: Option<f64>,
    previous_timestamp: Option<i64>,
}

impl UtcOpeningRangeStream {
    /// Creates a UTC-day opening range using the first `cutoff_hours` hours.
    ///
    /// # Errors
    ///
    /// Returns an error unless the cutoff is between 1 and 23 hours inclusive.
    pub fn new(cutoff_hours: u8) -> Result<Self, VectorCoreError> {
        if !(1..24).contains(&cutoff_hours) {
            return Err(VectorCoreError::InvalidProgram(
                "UTC opening-range cutoff must satisfy 1 <= hours < 24".to_owned(),
            ));
        }
        Ok(Self {
            cutoff_ms: i64::from(cutoff_hours) * HOUR_MS,
            current_day: None,
            range_high: None,
            range_low: None,
            previous_timestamp: None,
        })
    }

    /// Projects the completed opening range over one ordered timestamp chunk.
    ///
    /// # Errors
    ///
    /// Returns an error for unequal input lengths or decreasing timestamps.
    pub fn execute(
        &mut self,
        timestamps_ms: &[i64],
        high: &[f64],
        low: &[f64],
    ) -> Result<UtcOpeningRangeOutput, VectorCoreError> {
        validate_equal_lengths("UTC opening range", &[high, low])?;
        if timestamps_ms.len() != high.len() {
            return Err(length_error("UTC opening range"));
        }
        validate_chronology(timestamps_ms, self.previous_timestamp, "UTC opening range")?;

        let mut output_high = Vec::with_capacity(high.len());
        let mut output_low = Vec::with_capacity(low.len());
        for ((&timestamp, &high), &low) in timestamps_ms.iter().zip(high).zip(low) {
            let day = timestamp.div_euclid(DAY_MS);
            if self.current_day != Some(day) {
                self.current_day = Some(day);
                self.range_high = None;
                self.range_low = None;
            }
            let within_day = timestamp.rem_euclid(DAY_MS);
            if within_day < self.cutoff_ms {
                update_max(&mut self.range_high, high);
                update_min(&mut self.range_low, low);
                output_high.push(canonical_nan());
                output_low.push(canonical_nan());
            } else {
                output_high.push(self.range_high.unwrap_or_else(canonical_nan));
                output_low.push(self.range_low.unwrap_or_else(canonical_nan));
            }
        }
        self.previous_timestamp = timestamps_ms.last().copied().or(self.previous_timestamp);
        Ok(UtcOpeningRangeOutput {
            high: output_high,
            low: output_low,
        })
    }

    /// Returns the number of day aggregates retained by the stream.
    #[must_use]
    pub fn retained(&self) -> usize {
        usize::from(self.current_day.is_some())
    }
}

/// Executes a UTC-day opening-range projection over one complete batch.
///
/// # Errors
///
/// Returns an error for an invalid cutoff, unequal lengths, or decreasing timestamps.
pub fn utc_opening_range(
    timestamps_ms: &[i64],
    high: &[f64],
    low: &[f64],
    cutoff_hours: u8,
) -> Result<UtcOpeningRangeOutput, VectorCoreError> {
    UtcOpeningRangeStream::new(cutoff_hours)?.execute(timestamps_ms, high, low)
}

/// Three projected columns for the preceding completed hourly inside bar.
#[derive(Clone, Debug, PartialEq)]
pub struct HourlyInsideBarOutput {
    /// `1.0` when hour H-1 is strictly inside H-2, otherwise `0.0`.
    pub ready: Vec<f64>,
    /// High of the H-2 mother hour, or canonical NaN when unavailable.
    pub mother_high: Vec<f64>,
    /// Low of the H-2 mother hour, or canonical NaN when unavailable.
    pub mother_low: Vec<f64>,
}

#[derive(Clone, Copy, Debug)]
struct HourAggregate {
    hour: i64,
    high: Option<f64>,
    low: Option<f64>,
}

impl HourAggregate {
    const fn new(hour: i64) -> Self {
        Self {
            hour,
            high: None,
            low: None,
        }
    }

    fn update(&mut self, high: f64, low: f64) {
        update_max(&mut self.high, high);
        update_min(&mut self.low, low);
    }
}

/// Bounded current-plus-two-prior-hour state for inside-bar projection.
#[derive(Debug, Default)]
pub struct HourlyInsideBarStream {
    current: Option<HourAggregate>,
    previous: Option<HourAggregate>,
    mother: Option<HourAggregate>,
    previous_timestamp: Option<i64>,
}

impl HourlyInsideBarStream {
    /// Creates an empty hourly inside-bar stream.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            current: None,
            previous: None,
            mother: None,
            previous_timestamp: None,
        }
    }

    /// Projects H-1 inside H-2 state over one ordered timestamp chunk.
    ///
    /// # Errors
    ///
    /// Returns an error for unequal input lengths or decreasing timestamps.
    pub fn execute(
        &mut self,
        timestamps_ms: &[i64],
        high: &[f64],
        low: &[f64],
    ) -> Result<HourlyInsideBarOutput, VectorCoreError> {
        validate_equal_lengths("hourly inside bar", &[high, low])?;
        if timestamps_ms.len() != high.len() {
            return Err(length_error("hourly inside bar"));
        }
        validate_chronology(timestamps_ms, self.previous_timestamp, "hourly inside bar")?;

        let mut ready = Vec::with_capacity(high.len());
        let mut mother_high = Vec::with_capacity(high.len());
        let mut mother_low = Vec::with_capacity(low.len());
        for ((&timestamp, &high), &low) in timestamps_ms.iter().zip(high).zip(low) {
            let hour = timestamp.div_euclid(HOUR_MS);
            if self.current.is_none_or(|current| current.hour != hour) {
                self.mother = self.previous;
                self.previous = self.current;
                self.current = Some(HourAggregate::new(hour));
            }

            let previous = self
                .previous
                .filter(|aggregate| aggregate.hour == hour.saturating_sub(1));
            let mother = self
                .mother
                .filter(|aggregate| aggregate.hour == hour.saturating_sub(2));
            let complete = previous.zip(mother);
            let is_inside = complete.is_some_and(|(previous, mother)| {
                previous
                    .high
                    .zip(mother.high)
                    .is_some_and(|(inner, outer)| inner < outer)
                    && previous
                        .low
                        .zip(mother.low)
                        .is_some_and(|(inner, outer)| inner > outer)
            });
            ready.push(if is_inside { 1.0 } else { 0.0 });
            mother_high.push(
                complete
                    .and_then(|(_, aggregate)| aggregate.high)
                    .unwrap_or_else(canonical_nan),
            );
            mother_low.push(
                complete
                    .and_then(|(_, aggregate)| aggregate.low)
                    .unwrap_or_else(canonical_nan),
            );
            let Some(current) = self.current.as_mut() else {
                return Err(VectorCoreError::InvalidState(
                    "hourly inside-bar current aggregate is missing".to_owned(),
                ));
            };
            current.update(high, low);
        }
        self.previous_timestamp = timestamps_ms.last().copied().or(self.previous_timestamp);
        Ok(HourlyInsideBarOutput {
            ready,
            mother_high,
            mother_low,
        })
    }

    /// Returns the number of hourly aggregates retained by the stream.
    #[must_use]
    pub fn retained(&self) -> usize {
        [self.current, self.previous, self.mother]
            .iter()
            .flatten()
            .count()
    }
}

/// Executes hourly inside-bar projection over one complete batch.
///
/// # Errors
///
/// Returns an error for unequal lengths or decreasing timestamps.
pub fn hourly_inside_bar(
    timestamps_ms: &[i64],
    high: &[f64],
    low: &[f64],
) -> Result<HourlyInsideBarOutput, VectorCoreError> {
    HourlyInsideBarStream::new().execute(timestamps_ms, high, low)
}

fn validate_period(period: usize, name: &str) -> Result<(), VectorCoreError> {
    if period == 0 || period > MAX_PERIOD {
        return Err(VectorCoreError::InvalidProgram(format!(
            "{name} period must satisfy 0 < period <= {MAX_PERIOD}"
        )));
    }
    Ok(())
}

fn validate_equal_lengths(name: &str, inputs: &[&[f64]]) -> Result<(), VectorCoreError> {
    if inputs.first().is_some_and(|first| {
        inputs
            .iter()
            .skip(1)
            .any(|input| input.len() != first.len())
    }) {
        return Err(length_error(name));
    }
    Ok(())
}

fn validate_chronology(
    timestamps_ms: &[i64],
    previous: Option<i64>,
    name: &str,
) -> Result<(), VectorCoreError> {
    let mut prior = previous;
    for &timestamp in timestamps_ms {
        if prior.is_some_and(|prior| timestamp < prior) {
            return Err(VectorCoreError::InvalidState(format!(
                "{name} timestamps are not chronological"
            )));
        }
        prior = Some(timestamp);
    }
    Ok(())
}

fn length_error(name: &str) -> VectorCoreError {
    VectorCoreError::InvalidState(format!("{name} input lengths differ"))
}

fn nan_to_num(value: f64) -> f64 {
    if value.is_nan() {
        0.0
    } else if value == f64::INFINITY {
        f64::MAX
    } else if value == f64::NEG_INFINITY {
        f64::MIN
    } else {
        value
    }
}

fn canonical_nan() -> f64 {
    f64::from_bits(CANONICAL_NAN_BITS)
}

fn update_max(current: &mut Option<f64>, candidate: f64) {
    if candidate.is_nan() {
        return;
    }
    if current.is_none_or(|value| candidate > value) {
        *current = Some(candidate);
    }
}

fn update_min(current: &mut Option<f64>, candidate: f64) {
    if candidate.is_nan() {
        return;
    }
    if current.is_none_or(|value| candidate < value) {
        *current = Some(candidate);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_bits(actual: &[f64], expected: &[f64]) {
        assert_eq!(actual.len(), expected.len());
        for (index, (actual, expected)) in actual.iter().zip(expected).enumerate() {
            assert_eq!(actual.to_bits(), expected.to_bits(), "row {index}");
        }
    }

    #[test]
    fn chaikin_money_flow_matches_numpy_prefix_subtract_order() {
        let high = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0];
        let low = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0];
        let close = [1.5, 2.0, 2.5, 4.5, 5.0, 5.5];
        let volume = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0];
        let actual = chaikin_money_flow(&high, &low, &close, &volume, 3).expect("valid CMF");
        let expected = [
            canonical_nan(),
            canonical_nan(),
            f64::from_bits(0xbfc5_5555_5555_5555),
            f64::from_bits(0x3fac_71c7_1c71_c71c),
            f64::from_bits(0x3fa5_5555_5555_5555),
            f64::from_bits(0xbfb1_1111_1111_1111),
        ];
        assert_bits(&actual, &expected);
    }

    #[test]
    fn legacy_chaikin_keeps_talib_volume_sum_nan_warmup() {
        let high = [2.0; 4];
        let low = [0.0; 4];
        let close = [1.5; 4];
        let volume = [f64::NAN, 1.0, 1.0, 1.0];
        let expected = [canonical_nan(), canonical_nan(), 0.5, 0.5];
        let actual =
            legacy_chaikin_money_flow(&high, &low, &close, &volume, 2).expect("valid legacy CMF");
        assert_bits(&actual, &expected);

        let modern = chaikin_money_flow(&high, &low, &close, &volume, 2).expect("valid modern CMF");
        assert_eq!(modern[1].to_bits(), 0.5_f64.to_bits());
        assert!(actual[1].is_nan());

        let mut stream = LegacyChaikinMoneyFlowStream::new(2).expect("valid stream");
        let mut chunked = stream
            .execute(&high[..1], &low[..1], &close[..1], &volume[..1])
            .expect("first chunk");
        chunked.extend(
            stream
                .execute(&high[1..], &low[1..], &close[1..], &volume[1..])
                .expect("second chunk"),
        );
        assert_bits(&chunked, &expected);
        assert!(stream.retained() <= 4);
    }

    #[test]
    fn chaikin_nan_to_num_matches_numpy_saturation() {
        let high = [2.0; 6];
        let low = [0.0, 0.0, 0.0, 0.0, 0.0, 2.0];
        let close = [f64::INFINITY, f64::NEG_INFINITY, f64::NAN, 1.5, 1.5, 2.0];
        let volume = [
            1.0,
            1.0,
            1.0,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::INFINITY,
        ];
        let actual = chaikin_money_flow(&high, &low, &close, &volume, 1).expect("valid CMF");
        assert_bits(&actual, &[f64::MAX, f64::MIN, 0.0, 1.0, 1.0, 0.0]);
    }

    #[test]
    fn scalar_native_streams_are_chunk_exact_and_bounded() {
        let values = [
            1.0,
            2.0,
            0.0,
            -0.0,
            4.0,
            f64::NAN,
            8.0,
            f64::INFINITY,
            f64::INFINITY,
        ];
        let expected_pct = safe_percent_change(&values);
        let expected_diff = absolute_difference(&values);
        assert_bits(
            &expected_pct,
            &[
                canonical_nan(),
                100.0,
                -100.0,
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                f64::INFINITY,
                canonical_nan(),
            ],
        );
        assert_bits(
            &expected_diff,
            &[
                canonical_nan(),
                1.0,
                2.0,
                0.0,
                4.0,
                canonical_nan(),
                canonical_nan(),
                f64::INFINITY,
                canonical_nan(),
            ],
        );
        let mut pct = SafePercentChangeStream::new();
        let mut diff = AbsoluteDifferenceStream::new();
        let mut actual_pct = Vec::new();
        let mut actual_diff = Vec::new();
        for chunk in [&values[..1], &values[1..4], &values[4..7], &values[7..]] {
            actual_pct.extend(pct.execute(chunk));
            actual_diff.extend(diff.execute(chunk));
            assert!(pct.retained() <= 1);
            assert!(diff.retained() <= 1);
        }
        assert_bits(&actual_pct, &expected_pct);
        assert_bits(&actual_diff, &expected_diff);
        assert_eq!(expected_pct[0].to_bits(), CANONICAL_NAN_BITS);
        assert_eq!(expected_diff[0].to_bits(), CANONICAL_NAN_BITS);
    }

    #[test]
    fn chaikin_stream_is_chunk_exact_and_period_bounded() {
        let high = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0];
        let low = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0];
        let close = [1.5, 2.0, 2.5, 4.5, 5.0, 5.5];
        let volume = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0];
        let expected = chaikin_money_flow(&high, &low, &close, &volume, 3).expect("batch");
        let mut stream = ChaikinMoneyFlowStream::new(3).expect("stream");
        let mut actual = Vec::new();
        for (start, end) in [(0, 1), (1, 3), (3, 4), (4, 6)] {
            actual.extend(
                stream
                    .execute(
                        &high[start..end],
                        &low[start..end],
                        &close[start..end],
                        &volume[start..end],
                    )
                    .expect("chunk"),
            );
            assert!(stream.retained() <= 3);
        }
        assert_bits(&actual, &expected);
    }

    #[test]
    fn utc_opening_range_is_chunk_exact_across_days() {
        let day = 1_700_000_000_000_i64.div_euclid(DAY_MS) * DAY_MS;
        let timestamps = [
            day,
            day + HOUR_MS,
            day + 3 * HOUR_MS,
            day + 4 * HOUR_MS,
            day + 6 * HOUR_MS,
            day + DAY_MS,
            day + DAY_MS + 2 * HOUR_MS,
            day + DAY_MS + 4 * HOUR_MS,
        ];
        let high = [10.0, 12.0, 11.0, 99.0, 100.0, f64::NAN, 9.0, 50.0];
        let low = [5.0, 4.0, 3.0, -10.0, -20.0, 6.0, f64::NAN, 0.0];
        let expected = utc_opening_range(&timestamps, &high, &low, 4).expect("batch");
        assert_bits(
            &expected.high,
            &[
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                12.0,
                12.0,
                canonical_nan(),
                canonical_nan(),
                9.0,
            ],
        );
        assert_bits(
            &expected.low,
            &[
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                3.0,
                3.0,
                canonical_nan(),
                canonical_nan(),
                6.0,
            ],
        );

        let mut stream = UtcOpeningRangeStream::new(4).expect("stream");
        let mut actual_high = Vec::new();
        let mut actual_low = Vec::new();
        for (start, end) in [(0, 2), (2, 5), (5, 6), (6, 8)] {
            let output = stream
                .execute(&timestamps[start..end], &high[start..end], &low[start..end])
                .expect("chunk");
            actual_high.extend(output.high);
            actual_low.extend(output.low);
            assert!(stream.retained() <= 1);
        }
        assert_bits(&actual_high, &expected.high);
        assert_bits(&actual_low, &expected.low);
    }

    #[test]
    fn hourly_inside_bar_projects_only_contiguous_completed_hours() {
        let hour = 1_700_000_000_000_i64.div_euclid(HOUR_MS) * HOUR_MS;
        let timestamps = [
            hour,
            hour + 30 * 60_000,
            hour + HOUR_MS,
            hour + HOUR_MS + 30 * 60_000,
            hour + 2 * HOUR_MS,
            hour + 2 * HOUR_MS + 30 * 60_000,
            hour + 4 * HOUR_MS,
        ];
        let high = [10.0, 12.0, 11.0, 10.0, 20.0, 21.0, 30.0];
        let low = [0.0, -2.0, -1.0, 0.0, 5.0, 4.0, 3.0];
        let expected = hourly_inside_bar(&timestamps, &high, &low).expect("batch");
        assert_bits(&expected.ready, &[0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0]);
        assert_bits(
            &expected.mother_high,
            &[
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                12.0,
                12.0,
                canonical_nan(),
            ],
        );
        assert_bits(
            &expected.mother_low,
            &[
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                canonical_nan(),
                -2.0,
                -2.0,
                canonical_nan(),
            ],
        );

        let mut stream = HourlyInsideBarStream::new();
        let mut actual = HourlyInsideBarOutput {
            ready: Vec::new(),
            mother_high: Vec::new(),
            mother_low: Vec::new(),
        };
        for (start, end) in [(0, 1), (1, 3), (3, 6), (6, 7)] {
            let chunk = stream
                .execute(&timestamps[start..end], &high[start..end], &low[start..end])
                .expect("chunk");
            actual.ready.extend(chunk.ready);
            actual.mother_high.extend(chunk.mother_high);
            actual.mother_low.extend(chunk.mother_low);
            assert!(stream.retained() <= 3);
        }
        assert_bits(&actual.ready, &expected.ready);
        assert_bits(&actual.mother_high, &expected.mother_high);
        assert_bits(&actual.mother_low, &expected.mother_low);
    }

    #[test]
    fn native_kernels_fail_closed_without_mutating_order_state() {
        assert!(ChaikinMoneyFlowStream::new(0).is_err());
        assert!(UtcOpeningRangeStream::new(0).is_err());
        assert!(chaikin_money_flow(&[1.0], &[], &[1.0], &[1.0], 1).is_err());

        let mut opening = UtcOpeningRangeStream::new(4).expect("stream");
        assert!(opening.execute(&[2, 1], &[1.0, 1.0], &[1.0, 1.0]).is_err());
        assert_eq!(opening.retained(), 0);

        let mut inside = HourlyInsideBarStream::new();
        assert!(inside.execute(&[2, 1], &[1.0, 1.0], &[1.0, 1.0]).is_err());
        assert_eq!(inside.retained(), 0);
    }
}
