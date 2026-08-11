//! Exact Freqtrade OHLCV preparation for native indicator execution.
//!
//! [`crate::raw_ohlcv`] is intentionally a non-transforming transport boundary.
//! This module owns the observable Freqtrade 2026.5.1 preparation semantics:
//! timerange/startup bounding, duplicate aggregation, fixed-timeframe resampling,
//! gap filling, and the inclusive execution/context slice.

use std::collections::BTreeMap;

use nfi_vector_core::alignment::{
    FrameCatalog, FrameIdentity, NumericFrame, SourceLocation, Timeframe,
};
use nfi_vector_core::VectorCoreError;

use crate::VectorInputError;

const OHLCV_COLUMNS: [&str; 5] = ["open", "high", "low", "close", "volume"];
const DAY_MS: i64 = 86_400_000;

/// A finite timerange with inclusive start and stop boundaries.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClosedTimerange {
    pub start_ms: i64,
    pub stop_ms: i64,
}

impl ClosedTimerange {
    /// Parse the closed calendar-date, Unix-second, and Unix-millisecond forms
    /// accepted by the Python vector worker.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error for open, malformed, out-of-range, or
    /// reversed boundaries.
    pub fn parse(value: &str) -> Result<Self, VectorInputError> {
        if value.matches('-').count() != 1 {
            return Err(invalid("timerange must contain exactly one separator"));
        }
        let Some((start, stop)) = value.split_once('-') else {
            return Err(invalid("timerange must contain exactly one separator"));
        };
        let start_ms = parse_boundary(start)?;
        let stop_ms = parse_boundary(stop)?;
        if start_ms > stop_ms {
            return Err(invalid(format!(
                "timerange start is after its stop boundary: {value}"
            )));
        }
        Ok(Self { start_ms, stop_ms })
    }
}

/// Inclusive row positions processed by Freqtrade's chronological loop.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InclusiveExecutionPositions {
    pub start_index: usize,
    pub stop_index: usize,
}

/// A callback-visible slice and the first row allowed to emit simulator events.
#[derive(Clone, Debug, PartialEq)]
pub struct PreparedExecutionOhlcv {
    pub frame: NumericFrame,
    pub execution_start_index: usize,
}

/// Bound every raw frame independently by its timeframe, then apply exact
/// Freqtrade duplicate and gap preparation.
///
/// This function never mutates or substitutes an input catalog frame.
///
/// # Errors
///
/// Returns an error for invalid timeranges, unsupported timeframes, arithmetic
/// overflow, missing OHLCV columns, or Arrow-null OHLCV values.
pub fn prepare_freqtrade_ohlcv_catalog(
    raw: &FrameCatalog,
    timerange: &str,
    startup_candles: usize,
) -> Result<FrameCatalog, VectorInputError> {
    let timerange = ClosedTimerange::parse(timerange)?;
    let source = SourceLocation::new(
        "freqtrade-ohlcv-preparation",
        "native/freqtrade_ohlcv",
        0,
        0,
    );
    let mut entries = Vec::new();
    for identity in raw.identities() {
        let frame = raw.lookup(identity, &source)?;
        let duration_ms = supported_duration(&identity.timeframe)?;
        let startup_ms =
            duration_ms
                .checked_mul(i64::try_from(startup_candles).map_err(|_| {
                    invalid("startup candle count does not fit timestamp arithmetic")
                })?)
                .ok_or_else(|| invalid("startup window is outside timestamp range"))?;
        let load_start = timerange
            .start_ms
            .checked_sub(startup_ms)
            .ok_or_else(|| invalid("startup window is outside timestamp range"))?;
        let bounded = select_closed(frame, load_start, timerange.stop_ms)?;
        let prepared = clean_frame(&bounded)?;
        entries.push((identity.clone(), prepared));
    }
    FrameCatalog::new(entries).map_err(Into::into)
}

/// Aggregate duplicates, resample one supported fixed timeframe, and fill
/// missing candles exactly like Freqtrade 2026.5.1.
///
/// Duplicate rows use `open:first`, `high:max`, `low:min`, `close:last`, and
/// `volume:max`. Resample buckets use the corresponding OHLC aggregation and
/// sum the duplicate-normalized volume. Empty buckets receive the previous
/// close for OHLC and zero volume.
///
/// # Errors
///
/// Returns an error for unsupported timeframes, invalid shapes, missing OHLCV
/// columns, Arrow-null numeric cells, or an unrepresentable output range.
pub fn clean_frame(frame: &NumericFrame) -> Result<NumericFrame, VectorInputError> {
    frame.validate()?;
    let duration_ms = supported_duration(&frame.identity.timeframe)?;
    validate_ohlcv_columns(frame)?;
    if frame.timestamps_ms.is_empty() {
        return Ok(empty_ohlcv(frame.identity.clone()));
    }

    let mut duplicate_groups = BTreeMap::<i64, DuplicateAggregate>::new();
    for row in 0..frame.timestamps_ms.len() {
        let values = row_values(frame, row)?;
        duplicate_groups
            .entry(frame.timestamps_ms[row])
            .or_default()
            .observe(values);
    }

    let mut buckets = BTreeMap::<i64, ResampleAggregate>::new();
    for (timestamp_ms, aggregate) in duplicate_groups {
        let bucket_ms = timestamp_ms.div_euclid(duration_ms) * duration_ms;
        buckets
            .entry(bucket_ms)
            .or_default()
            .observe(aggregate.finish());
    }
    let first_bucket = *buckets
        .first_key_value()
        .ok_or_else(|| invalid("non-empty OHLCV source produced no resample bucket"))?
        .0;
    let last_bucket = *buckets
        .last_key_value()
        .ok_or_else(|| invalid("non-empty OHLCV source produced no resample bucket"))?
        .0;
    let row_count = last_bucket
        .checked_sub(first_bucket)
        .and_then(|span| span.checked_div(duration_ms))
        .and_then(|rows| rows.checked_add(1))
        .and_then(|rows| usize::try_from(rows).ok())
        .ok_or_else(|| invalid("resampled OHLCV range is outside addressable memory"))?;

    let mut output_timestamps = Vec::new();
    output_timestamps
        .try_reserve_exact(row_count)
        .map_err(|_| invalid("cannot allocate resampled OHLCV timestamps"))?;
    let mut output = OutputColumns::with_capacity(row_count)?;
    let mut previous_close = None;
    for offset in 0..row_count {
        let offset = i64::try_from(offset)
            .map_err(|_| invalid("resampled OHLCV row offset is out of range"))?;
        let current_timestamp = first_bucket
            .checked_add(
                offset
                    .checked_mul(duration_ms)
                    .ok_or_else(|| invalid("resampled OHLCV timestamp is out of range"))?,
            )
            .ok_or_else(|| invalid("resampled OHLCV timestamp is out of range"))?;
        output_timestamps.push(current_timestamp);
        let aggregate = buckets.get(&current_timestamp);
        let close = aggregate
            .and_then(|value| value.close)
            .or(previous_close)
            .unwrap_or(f64::NAN);
        if !close.is_nan() {
            previous_close = Some(close);
        }
        output.open.push(Some(
            aggregate.and_then(|value| value.open).unwrap_or(close),
        ));
        output.high.push(Some(
            aggregate.and_then(|value| value.high).unwrap_or(close),
        ));
        output
            .low
            .push(Some(aggregate.and_then(|value| value.low).unwrap_or(close)));
        output.close.push(Some(close));
        output.volume.push(Some(
            aggregate.and_then(|value| value.volume).unwrap_or(0.0),
        ));
    }

    Ok(NumericFrame {
        identity: frame.identity.clone(),
        timestamps_ms: output_timestamps,
        columns: output.into_map(),
    })
}

/// Locate the inclusive rows Freqtrade processes after satisfying any missing
/// startup history from rows inside the requested range.
///
/// # Errors
///
/// Returns an error for an invalid timerange or non-increasing candle dates.
pub fn execution_positions(
    frame: &NumericFrame,
    timerange: &str,
    startup_candles: usize,
) -> Result<Option<InclusiveExecutionPositions>, VectorInputError> {
    frame.validate()?;
    ensure_strictly_increasing(frame)?;
    let timerange = ClosedTimerange::parse(timerange)?;
    let available_before_start = frame
        .timestamps_ms
        .partition_point(|timestamp| *timestamp < timerange.start_ms);
    let missing_startup = startup_candles.saturating_sub(available_before_start);
    let range_start = frame
        .timestamps_ms
        .partition_point(|timestamp| *timestamp < timerange.start_ms);
    let range_stop_exclusive = frame
        .timestamps_ms
        .partition_point(|timestamp| *timestamp <= timerange.stop_ms);
    let rows_in_range = range_stop_exclusive.saturating_sub(range_start);
    if rows_in_range == 0 || missing_startup >= rows_in_range {
        return Ok(None);
    }
    Ok(Some(InclusiveExecutionPositions {
        start_index: range_start + missing_startup,
        stop_index: range_stop_exclusive - 1,
    }))
}

/// Retain startup rows as callback-only context and identify the first row
/// allowed to execute. As in Freqtrade, the first trimmed row is not executable
/// because decision signals are shifted to the following candle open.
///
/// # Errors
///
/// Returns the errors from [`execution_positions`] or a slicing shape error.
pub fn prepare_execution_ohlcv(
    frame: &NumericFrame,
    timerange: &str,
    startup_candles: usize,
) -> Result<PreparedExecutionOhlcv, VectorInputError> {
    let Some(positions) = execution_positions(frame, timerange, startup_candles)? else {
        return Ok(PreparedExecutionOhlcv {
            frame: slice_frame(frame, 0, 0)?,
            execution_start_index: 0,
        });
    };
    let first_executable = positions
        .start_index
        .checked_add(1)
        .ok_or_else(|| invalid("execution start index is out of range"))?;
    if first_executable > positions.stop_index {
        return Ok(PreparedExecutionOhlcv {
            frame: slice_frame(frame, 0, 0)?,
            execution_start_index: 0,
        });
    }
    let context_rows = startup_candles.min(positions.start_index);
    let context_start = positions.start_index - context_rows;
    let stop_exclusive = positions
        .stop_index
        .checked_add(1)
        .ok_or_else(|| invalid("execution stop index is out of range"))?;
    Ok(PreparedExecutionOhlcv {
        frame: slice_frame(frame, context_start, stop_exclusive)?,
        execution_start_index: first_executable - context_start,
    })
}

#[derive(Clone, Copy, Debug, Default)]
struct DuplicateAggregate {
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    close: Option<f64>,
    volume: Option<f64>,
}

impl DuplicateAggregate {
    fn observe(&mut self, values: [f64; 5]) {
        self.open = self.open.or_else(|| present(values[0]));
        self.high = optional_max(self.high, present(values[1]));
        self.low = optional_min(self.low, present(values[2]));
        if let Some(close) = present(values[3]) {
            self.close = Some(close);
        }
        self.volume = optional_max(self.volume, present(values[4]));
    }

    fn finish(self) -> [Option<f64>; 5] {
        [self.open, self.high, self.low, self.close, self.volume]
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct ResampleAggregate {
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    close: Option<f64>,
    volume: Option<f64>,
}

impl ResampleAggregate {
    fn observe(&mut self, values: [Option<f64>; 5]) {
        self.open = self.open.or(values[0]);
        self.high = optional_max(self.high, values[1]);
        self.low = optional_min(self.low, values[2]);
        if let Some(close) = values[3] {
            self.close = Some(close);
        }
        if let Some(volume) = values[4] {
            self.volume = Some(self.volume.unwrap_or(0.0) + volume);
        }
    }
}

struct OutputColumns {
    open: Vec<Option<f64>>,
    high: Vec<Option<f64>>,
    low: Vec<Option<f64>>,
    close: Vec<Option<f64>>,
    volume: Vec<Option<f64>>,
}

impl OutputColumns {
    fn with_capacity(rows: usize) -> Result<Self, VectorInputError> {
        fn allocate(rows: usize) -> Result<Vec<Option<f64>>, VectorInputError> {
            let mut values = Vec::new();
            values
                .try_reserve_exact(rows)
                .map_err(|_| invalid("cannot allocate resampled OHLCV values"))?;
            Ok(values)
        }
        Ok(Self {
            open: allocate(rows)?,
            high: allocate(rows)?,
            low: allocate(rows)?,
            close: allocate(rows)?,
            volume: allocate(rows)?,
        })
    }

    fn into_map(self) -> BTreeMap<String, Vec<Option<f64>>> {
        BTreeMap::from([
            ("open".to_owned(), self.open),
            ("high".to_owned(), self.high),
            ("low".to_owned(), self.low),
            ("close".to_owned(), self.close),
            ("volume".to_owned(), self.volume),
        ])
    }
}

fn supported_duration(timeframe: &Timeframe) -> Result<i64, VectorInputError> {
    if !matches!(timeframe.as_str(), "5m" | "15m" | "1h" | "4h" | "1d") {
        return Err(invalid(format!(
            "unsupported Freqtrade OHLCV preparation timeframe: {}",
            timeframe.as_str()
        )));
    }
    Ok(timeframe.resample_duration_ms())
}

fn validate_ohlcv_columns(frame: &NumericFrame) -> Result<(), VectorInputError> {
    for name in OHLCV_COLUMNS {
        let values = frame.columns.get(name).ok_or_else(|| {
            invalid(format!(
                "candle frame for {} {} is missing column {name:?}",
                frame.identity.pair,
                frame.identity.timeframe.as_str()
            ))
        })?;
        if let Some(row) = values.iter().position(Option::is_none) {
            return Err(invalid(format!(
                "candle frame for {} {} column {name:?} contains Arrow null at row {row}",
                frame.identity.pair,
                frame.identity.timeframe.as_str()
            )));
        }
    }
    Ok(())
}

fn row_values(frame: &NumericFrame, row: usize) -> Result<[f64; 5], VectorInputError> {
    OHLCV_COLUMNS
        .map(|name| {
            frame.columns[name][row].ok_or_else(|| {
                invalid(format!(
                    "candle frame for {} {} column {name:?} contains Arrow null at row {row}",
                    frame.identity.pair,
                    frame.identity.timeframe.as_str()
                ))
            })
        })
        .into_iter()
        .collect::<Result<Vec<_>, _>>()?
        .try_into()
        .map_err(|_| invalid("internal OHLCV column count mismatch"))
}

fn present(value: f64) -> Option<f64> {
    (!value.is_nan()).then_some(value)
}

fn optional_max(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.max(right)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn optional_min(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.min(right)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn select_closed(
    frame: &NumericFrame,
    start_ms: i64,
    stop_ms: i64,
) -> Result<NumericFrame, VectorInputError> {
    frame.validate()?;
    let selected = frame
        .timestamps_ms
        .iter()
        .enumerate()
        .filter_map(|(row, timestamp)| {
            (*timestamp >= start_ms && *timestamp <= stop_ms).then_some(row)
        })
        .collect::<Vec<_>>();
    let columns = frame
        .columns
        .iter()
        .map(|(name, values)| {
            (
                name.clone(),
                selected.iter().map(|row| values[*row]).collect::<Vec<_>>(),
            )
        })
        .collect();
    Ok(NumericFrame {
        identity: frame.identity.clone(),
        timestamps_ms: selected
            .iter()
            .map(|row| frame.timestamps_ms[*row])
            .collect(),
        columns,
    })
}

fn slice_frame(
    frame: &NumericFrame,
    start: usize,
    stop_exclusive: usize,
) -> Result<NumericFrame, VectorInputError> {
    if start > stop_exclusive || stop_exclusive > frame.timestamps_ms.len() {
        return Err(invalid("execution OHLCV slice is outside the source frame"));
    }
    Ok(NumericFrame {
        identity: frame.identity.clone(),
        timestamps_ms: frame.timestamps_ms[start..stop_exclusive].to_vec(),
        columns: frame
            .columns
            .iter()
            .map(|(name, values)| (name.clone(), values[start..stop_exclusive].to_vec()))
            .collect(),
    })
}

fn empty_ohlcv(identity: FrameIdentity) -> NumericFrame {
    NumericFrame {
        identity,
        timestamps_ms: Vec::new(),
        columns: OHLCV_COLUMNS
            .map(|name| (name.to_owned(), Vec::new()))
            .into_iter()
            .collect(),
    }
}

fn ensure_strictly_increasing(frame: &NumericFrame) -> Result<(), VectorInputError> {
    if frame
        .timestamps_ms
        .windows(2)
        .any(|window| window[0] >= window[1])
    {
        return Err(invalid(format!(
            "candle frame for {} {} is not strictly chronological",
            frame.identity.pair,
            frame.identity.timeframe.as_str()
        )));
    }
    Ok(())
}

fn parse_boundary(value: &str) -> Result<i64, VectorInputError> {
    if !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(invalid("timerange boundary must be numeric"));
    }
    match value.len() {
        8 => parse_calendar_boundary(value),
        10 => value
            .parse::<i64>()
            .ok()
            .and_then(|seconds| seconds.checked_mul(1_000))
            .ok_or_else(|| invalid(format!("invalid timerange boundary: {value:?}"))),
        13 => value
            .parse::<i64>()
            .map_err(|_| invalid(format!("invalid timerange boundary: {value:?}"))),
        _ => Err(invalid("unsupported timerange boundary width")),
    }
}

fn parse_calendar_boundary(value: &str) -> Result<i64, VectorInputError> {
    let year = value[0..4]
        .parse::<i64>()
        .map_err(|_| invalid(format!("invalid timerange boundary: {value:?}")))?;
    let month = value[4..6]
        .parse::<u8>()
        .map_err(|_| invalid(format!("invalid timerange boundary: {value:?}")))?;
    let day = value[6..8]
        .parse::<u8>()
        .map_err(|_| invalid(format!("invalid timerange boundary: {value:?}")))?;
    if !(1..=9_999).contains(&year)
        || !(1..=12).contains(&month)
        || day == 0
        || day > days_in_month(year, month)
    {
        return Err(invalid(format!("invalid timerange boundary: {value:?}")));
    }
    days_from_civil(year, month, day)
        .checked_mul(DAY_MS)
        .ok_or_else(|| invalid(format!("invalid timerange boundary: {value:?}")))
}

fn days_in_month(year: i64, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        _ => 0,
    }
}

fn days_from_civil(year: i64, month: u8, day: u8) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let shifted_month = i64::from(month) + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

fn invalid(message: impl Into<String>) -> VectorInputError {
    VectorCoreError::InvalidProgram(format!(
        "Freqtrade OHLCV preparation failed: {}",
        message.into()
    ))
    .into()
}

#[cfg(test)]
mod tests {
    use super::*;

    const ORACLE_START_MS: i64 = 1_704_153_600_000;

    fn identity(timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(
            "ORACLE/USDT",
            Timeframe::parse(timeframe).expect("timeframe"),
        )
        .expect("identity")
    }

    fn frame(
        timeframe: &str,
        timestamps_ms: Vec<i64>,
        rows: impl IntoIterator<Item = [Option<f64>; 5]>,
    ) -> NumericFrame {
        let rows = rows.into_iter().collect::<Vec<_>>();
        NumericFrame {
            identity: identity(timeframe),
            timestamps_ms,
            columns: OHLCV_COLUMNS
                .into_iter()
                .enumerate()
                .map(|(column, name)| {
                    (
                        name.to_owned(),
                        rows.iter().map(|row| row[column]).collect(),
                    )
                })
                .collect(),
        }
    }

    fn complete_row(value: f64) -> [Option<f64>; 5] {
        [
            Some(value),
            Some(value + 1.0),
            Some(value - 1.0),
            Some(value + 0.5),
            Some(value),
        ]
    }

    fn assert_column(frame: &NumericFrame, name: &str, expected: &[f64]) {
        let actual = frame.columns[name]
            .iter()
            .map(|value| value.expect("prepared value"))
            .collect::<Vec<_>>();
        assert_eq!(actual, expected);
    }

    #[test]
    fn matches_python_duplicate_and_gap_oracle() {
        // Generated with vector_worker._clean_ohlcv_like_freqtrade on 2026-08-12.
        let raw = frame(
            "5m",
            vec![1_704_067_200_000, 1_704_067_200_000, 1_704_067_800_000],
            vec![
                [Some(10.0), Some(12.0), Some(9.0), Some(11.0), Some(2.0)],
                [Some(11.0), Some(13.0), Some(8.0), Some(12.0), Some(3.0)],
                [Some(20.0), Some(21.0), Some(19.0), Some(20.0), Some(4.0)],
            ],
        );

        let prepared = clean_frame(&raw).expect("prepared");

        assert_eq!(
            prepared.timestamps_ms,
            [1_704_067_200_000, 1_704_067_500_000, 1_704_067_800_000]
        );
        assert_column(&prepared, "open", &[10.0, 12.0, 20.0]);
        assert_column(&prepared, "high", &[13.0, 12.0, 21.0]);
        assert_column(&prepared, "low", &[8.0, 12.0, 19.0]);
        assert_column(&prepared, "close", &[12.0, 12.0, 20.0]);
        assert_column(&prepared, "volume", &[3.0, 0.0, 4.0]);
    }

    #[test]
    fn matches_python_fixed_anchor_oracles_for_every_native_timeframe() {
        // Each expected triplet was generated by the Python helper from two
        // candles separated by exactly two timeframe durations.
        for (timeframe, duration_ms) in [
            ("5m", 300_000),
            ("15m", 900_000),
            ("1h", 3_600_000),
            ("4h", 14_400_000),
            ("1d", 86_400_000),
        ] {
            let raw = frame(
                timeframe,
                vec![ORACLE_START_MS, ORACLE_START_MS + 2 * duration_ms],
                vec![complete_row(10.0), complete_row(20.0)],
            );

            let prepared = clean_frame(&raw).expect("prepared");

            assert_eq!(
                prepared.timestamps_ms,
                [
                    ORACLE_START_MS,
                    ORACLE_START_MS + duration_ms,
                    ORACLE_START_MS + 2 * duration_ms,
                ],
                "{timeframe}"
            );
            assert_column(&prepared, "open", &[10.0, 10.5, 20.0]);
            assert_column(&prepared, "high", &[11.0, 10.5, 21.0]);
            assert_column(&prepared, "low", &[9.0, 10.5, 19.0]);
            assert_column(&prepared, "close", &[10.5, 10.5, 20.5]);
            assert_column(&prepared, "volume", &[10.0, 0.0, 20.0]);
        }
    }

    #[test]
    fn bounds_startup_independently_for_each_timeframe() {
        let base = frame(
            "5m",
            (0..19)
                .map(|row| 1_704_150_000_000 + i64::from(row) * 300_000)
                .collect(),
            (0..19).map(|row| complete_row(f64::from(row))),
        );
        let informative = frame(
            "15m",
            (0..11)
                .map(|row| 1_704_146_400_000 + i64::from(row) * 900_000)
                .collect(),
            (0..11).map(|row| complete_row(f64::from(row))),
        );
        let catalog = FrameCatalog::new([
            (base.identity.clone(), base),
            (informative.identity.clone(), informative),
        ])
        .expect("catalog");

        let bounded =
            prepare_freqtrade_ohlcv_catalog(&catalog, "1704153600-1704155400", 2).expect("bounded");
        let source = SourceLocation::new("test", "oracle", 1, 1);

        assert_eq!(
            bounded
                .lookup(&identity("5m"), &source)
                .expect("base")
                .timestamps_ms,
            (0..=8)
                .map(|row| 1_704_153_000_000 + i64::from(row) * 300_000)
                .collect::<Vec<_>>()
        );
        assert_eq!(
            bounded
                .lookup(&identity("15m"), &source)
                .expect("informative")
                .timestamps_ms,
            (0..=4)
                .map(|row| 1_704_151_800_000 + i64::from(row) * 900_000)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn execution_positions_are_inclusive_and_context_is_never_executable() {
        let source = frame(
            "5m",
            (0..5)
                .map(|row| 1_651_359_300_000 + i64::from(row) * 300_000)
                .collect(),
            (0..5).map(|row| complete_row(f64::from(row))),
        );

        let positions = execution_positions(&source, "1651360200-1651360500", 3)
            .expect("positions")
            .expect("nonempty");
        let prepared =
            prepare_execution_ohlcv(&source, "1651360200-1651360500", 3).expect("prepared");

        assert_eq!(
            positions,
            InclusiveExecutionPositions {
                start_index: 3,
                stop_index: 4,
            }
        );
        assert_eq!(prepared.frame.timestamps_ms, source.timestamps_ms);
        assert_eq!(prepared.execution_start_index, 4);
    }

    #[test]
    fn missing_startup_consumes_in_range_rows_and_can_leave_no_execution() {
        // Python `_execution_positions` oracle: only one candle precedes the
        // boundary, so two of the requested three startup rows come from the
        // closed timerange itself.
        let source = frame(
            "5m",
            (0..4)
                .map(|row| 1_704_153_300_000 + i64::from(row) * 300_000)
                .collect(),
            (0..4).map(|row| complete_row(f64::from(row))),
        );

        assert_eq!(
            execution_positions(&source, "1704153600-1704154500", 3).expect("positions"),
            Some(InclusiveExecutionPositions {
                start_index: 3,
                stop_index: 3,
            })
        );
        let prepared =
            prepare_execution_ohlcv(&source, "1704153600-1704154500", 3).expect("prepared");
        assert!(prepared.frame.timestamps_ms.is_empty());
        assert_eq!(prepared.execution_start_index, 0);
    }

    #[test]
    fn timerange_calendar_seconds_and_milliseconds_are_the_same_closed_range() {
        let expected = ClosedTimerange {
            start_ms: 1_704_067_200_000,
            stop_ms: 1_704_153_600_000,
        };
        assert_eq!(
            ClosedTimerange::parse("20240101-20240102").expect("calendar"),
            expected
        );
        assert_eq!(
            ClosedTimerange::parse("1704067200-1704153600").expect("seconds"),
            expected
        );
        assert_eq!(
            ClosedTimerange::parse("1704067200000-1704153600000").expect("milliseconds"),
            expected
        );
    }

    #[test]
    fn fails_closed_on_unsupported_timeframe_range_shape_and_arrow_null() {
        let unsupported = frame("30m", vec![ORACLE_START_MS], vec![complete_row(1.0)]);
        assert!(clean_frame(&unsupported)
            .expect_err("unsupported timeframe")
            .to_string()
            .contains("unsupported Freqtrade OHLCV preparation timeframe"));

        for timerange in [
            "20240101-",
            "-20240101",
            "20240101-20231231",
            "20240230-20240301",
            "20240101-20240201-extra",
        ] {
            assert!(ClosedTimerange::parse(timerange).is_err(), "{timerange}");
        }

        let mut missing = frame("5m", vec![ORACLE_START_MS], vec![complete_row(1.0)]);
        missing.columns.remove("volume");
        assert!(clean_frame(&missing)
            .expect_err("missing volume")
            .to_string()
            .contains("missing column \"volume\""));

        let mut nullable = frame("5m", vec![ORACLE_START_MS], vec![complete_row(1.0)]);
        nullable.columns.get_mut("close").expect("close")[0] = None;
        assert!(clean_frame(&nullable)
            .expect_err("Arrow null")
            .to_string()
            .contains("contains Arrow null at row 0"));
    }

    #[test]
    fn present_nan_uses_pandas_missing_value_aggregation() {
        let raw = frame(
            "5m",
            vec![ORACLE_START_MS, ORACLE_START_MS + 600_000],
            vec![
                [Some(10.0), Some(11.0), Some(9.0), Some(10.5), Some(3.0)],
                [
                    Some(f64::NAN),
                    Some(f64::NAN),
                    Some(f64::NAN),
                    Some(f64::NAN),
                    Some(f64::NAN),
                ],
            ],
        );

        let prepared = clean_frame(&raw).expect("prepared");

        assert_column(&prepared, "open", &[10.0, 10.5, 10.5]);
        assert_column(&prepared, "high", &[11.0, 10.5, 10.5]);
        assert_column(&prepared, "low", &[9.0, 10.5, 10.5]);
        assert_column(&prepared, "close", &[10.5, 10.5, 10.5]);
        assert_column(&prepared, "volume", &[3.0, 0.0, 0.0]);
    }
}
