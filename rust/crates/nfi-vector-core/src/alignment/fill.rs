//! Source-order forward fill for fully aligned frames.
//!
//! This is the final `DataFrame.ffill()` step used by NFI after every
//! informative merge. It deliberately does not inspect timestamps: pandas
//! fills in current row order, and merge is responsible for determining that
//! order before this operation runs.

use std::collections::{BTreeMap, BTreeSet};

use crate::VectorCoreError;

use super::{FrameIdentity, MergedFrame, SourceLocation};

/// Forward-fills every numeric and informative-date column in source order.
///
/// `None` and present `NaN` numeric cells are both missing. Leading missing
/// cells keep their original representation, while a missing cell after a
/// value receives that value bit-for-bit. Timestamps and row count are never
/// changed.
///
/// # Errors
///
/// Returns a source-located error when any column length differs from the
/// frame's timestamp length.
pub fn forward_fill(
    frame: &MergedFrame,
    source: &SourceLocation,
) -> Result<MergedFrame, VectorCoreError> {
    ForwardFillStream::new(source.clone()).execute(frame)
}

/// Bounded state for source-order forward fill across arbitrary chunks.
///
/// At most one non-missing value is retained for each numeric and
/// informative-date column. The first chunk fixes the frame schema;
/// later identity or column changes fail closed instead of reusing values
/// across unrelated frames.
#[derive(Debug)]
pub struct ForwardFillStream {
    source: SourceLocation,
    schema: Option<FillSchema>,
    last_numeric: BTreeMap<String, f64>,
    last_dates: BTreeMap<String, i64>,
}

impl ForwardFillStream {
    /// Creates a forward-fill stream with no retained values.
    #[must_use]
    pub fn new(source: SourceLocation) -> Self {
        Self {
            source,
            schema: None,
            last_numeric: BTreeMap::new(),
            last_dates: BTreeMap::new(),
        }
    }

    /// Forward-fills one chunk and retains only its last non-missing values.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for malformed column lengths or a frame
    /// identity/numeric/date schema that differs from the first chunk.
    pub fn execute(&mut self, frame: &MergedFrame) -> Result<MergedFrame, VectorCoreError> {
        validate_shape(frame, &self.source)?;
        self.validate_schema(frame)?;

        let mut output = frame.clone();
        for (name, values) in &mut output.columns {
            let mut last = self.last_numeric.get(name).copied();
            for value in values {
                match *value {
                    Some(present) if !present.is_nan() => last = Some(present),
                    Some(_) | None => {
                        if let Some(previous) = last {
                            *value = Some(previous);
                        }
                    }
                }
            }
            if let Some(last) = last {
                self.last_numeric.insert(name.clone(), last);
            }
        }
        for (name, values) in &mut output.informative_dates_ms {
            let mut last = self.last_dates.get(name).copied();
            for value in values {
                match *value {
                    Some(present) => last = Some(present),
                    None => {
                        if let Some(previous) = last {
                            *value = Some(previous);
                        }
                    }
                }
            }
            if let Some(last) = last {
                self.last_dates.insert(name.clone(), last);
            }
        }
        Ok(output)
    }

    /// Number of column values retained across calls.
    #[must_use]
    pub fn retained(&self) -> usize {
        self.last_numeric.len() + self.last_dates.len()
    }

    /// Maximum values this stream can retain for its established schema.
    #[must_use]
    pub fn retention_bound(&self) -> usize {
        self.schema.as_ref().map_or(0, FillSchema::column_count)
    }

    fn validate_schema(&mut self, frame: &MergedFrame) -> Result<(), VectorCoreError> {
        let actual = FillSchema::from_frame(frame);
        if let Some(expected) = &self.schema {
            if actual.identity != expected.identity {
                return Err(source_error(
                    &self.source,
                    format!(
                        "forward-fill frame identity changed from {:?} to {:?}",
                        expected.identity, actual.identity
                    ),
                ));
            }
            if actual.numeric != expected.numeric {
                return Err(source_error(
                    &self.source,
                    format!(
                        "forward-fill numeric columns changed from {:?} to {:?}",
                        expected.numeric, actual.numeric
                    ),
                ));
            }
            if actual.dates != expected.dates {
                return Err(source_error(
                    &self.source,
                    format!(
                        "forward-fill informative-date columns changed from {:?} to {:?}",
                        expected.dates, actual.dates
                    ),
                ));
            }
        } else {
            self.schema = Some(actual);
        }
        Ok(())
    }
}

#[derive(Debug)]
struct FillSchema {
    identity: FrameIdentity,
    numeric: BTreeSet<String>,
    dates: BTreeSet<String>,
}

impl FillSchema {
    fn from_frame(frame: &MergedFrame) -> Self {
        Self {
            identity: frame.identity.clone(),
            numeric: frame.columns.keys().cloned().collect(),
            dates: frame.informative_dates_ms.keys().cloned().collect(),
        }
    }

    fn column_count(&self) -> usize {
        self.numeric.len() + self.dates.len()
    }
}

fn validate_shape(frame: &MergedFrame, source: &SourceLocation) -> Result<(), VectorCoreError> {
    let expected = frame.timestamps_ms.len();
    for (name, values) in &frame.columns {
        if values.len() != expected {
            return Err(source_error(
                source,
                format!(
                    "forward-fill numeric column {name:?} has {} rows; expected {expected}",
                    values.len()
                ),
            ));
        }
    }
    for (name, values) in &frame.informative_dates_ms {
        if values.len() != expected {
            return Err(source_error(
                source,
                format!(
                    "forward-fill informative-date column {name:?} has {} rows; expected {expected}",
                    values.len()
                ),
            ));
        }
    }
    Ok(())
}

fn source_error(source: &SourceLocation, message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::Execution {
        node: source.node.clone(),
        message: format!(
            "{}:{}:{}: {}",
            source.path,
            source.line,
            source.column,
            message.into()
        ),
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::{forward_fill, ForwardFillStream};
    use crate::alignment::{FrameIdentity, MergedFrame, SourceLocation, Timeframe};

    fn source() -> SourceLocation {
        SourceLocation::new("fill-final", "strategy.py", 4864, 14)
    }

    fn identity(pair: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse("5m").expect("timeframe")).expect("identity")
    }

    fn frame() -> MergedFrame {
        MergedFrame {
            identity: identity("ETH/USDT"),
            timestamps_ms: (0_i64..9).map(|row| row * 300_000).collect(),
            columns: BTreeMap::from([
                (
                    "a".to_owned(),
                    vec![
                        None,
                        Some(f64::from_bits(0x7ff8_0000_0000_0042)),
                        Some(-0.0),
                        None,
                        Some(f64::INFINITY),
                        Some(f64::NAN),
                        Some(f64::NEG_INFINITY),
                        None,
                        Some(4.0),
                    ],
                ),
                (
                    "b".to_owned(),
                    vec![
                        Some(1.0),
                        None,
                        Some(f64::NAN),
                        Some(2.0),
                        None,
                        None,
                        Some(3.0),
                        None,
                        None,
                    ],
                ),
            ]),
            informative_dates_ms: BTreeMap::from([
                (
                    "date_1h".to_owned(),
                    vec![
                        None,
                        Some(10),
                        None,
                        Some(20),
                        None,
                        None,
                        Some(30),
                        None,
                        None,
                    ],
                ),
                (
                    "date_4h".to_owned(),
                    vec![None, None, Some(40), None, None, Some(50), None, None, None],
                ),
            ]),
        }
    }

    #[test]
    fn batch_preserves_leading_missing_and_fills_every_column_in_source_order() {
        let actual = forward_fill(&frame(), &source()).expect("fill");
        assert_eq!(actual.timestamps_ms, frame().timestamps_ms);
        assert_numeric_bits(
            &actual.columns["a"],
            &[
                None,
                Some(f64::from_bits(0x7ff8_0000_0000_0042)),
                Some(-0.0),
                Some(-0.0),
                Some(f64::INFINITY),
                Some(f64::INFINITY),
                Some(f64::NEG_INFINITY),
                Some(f64::NEG_INFINITY),
                Some(4.0),
            ],
        );
        assert_numeric_bits(
            &actual.columns["b"],
            &[
                Some(1.0),
                Some(1.0),
                Some(1.0),
                Some(2.0),
                Some(2.0),
                Some(2.0),
                Some(3.0),
                Some(3.0),
                Some(3.0),
            ],
        );
        assert_eq!(
            actual.informative_dates_ms["date_1h"],
            vec![
                None,
                Some(10),
                Some(10),
                Some(20),
                Some(20),
                Some(20),
                Some(30),
                Some(30),
                Some(30)
            ]
        );
        assert_eq!(
            actual.informative_dates_ms["date_4h"],
            vec![
                None,
                None,
                Some(40),
                Some(40),
                Some(40),
                Some(50),
                Some(50),
                Some(50),
                Some(50)
            ]
        );
    }

    #[test]
    fn arbitrary_chunks_are_bit_exact_to_batch_and_retention_is_bounded() {
        let input = frame();
        let expected = forward_fill(&input, &source()).expect("batch fill");
        let mut stream = ForwardFillStream::new(source());
        let mut parts = Vec::new();
        for range in [0..0, 0..1, 1..3, 3..4, 4..8, 8..9, 9..9] {
            let chunk = slice(&input, range.start, range.end);
            parts.push(stream.execute(&chunk).expect("stream fill"));
            assert!(stream.retained() <= stream.retention_bound());
            assert_eq!(stream.retention_bound(), 4);
        }
        let actual = concatenate(&parts);
        assert_frame_bits(&actual, &expected);
        assert_eq!(stream.retained(), 4);
    }

    #[test]
    fn malformed_shape_and_stream_schema_drift_fail_closed_at_source() {
        let mut malformed = frame();
        malformed.columns.get_mut("a").expect("column").pop();
        let error = forward_fill(&malformed, &source()).expect_err("shape rejection");
        assert_eq!(
            error.to_string(),
            "vector node fill-final failed: strategy.py:4864:14: forward-fill numeric column \
             \"a\" has 8 rows; expected 9"
        );

        let mut stream = ForwardFillStream::new(source());
        stream.execute(&slice(&frame(), 0, 2)).expect("first chunk");
        let mut drifted = slice(&frame(), 2, 4);
        drifted.columns.remove("b");
        let error = stream.execute(&drifted).expect_err("schema rejection");
        assert!(error
            .to_string()
            .contains("strategy.py:4864:14: forward-fill numeric columns changed"));

        let mut date_stream = ForwardFillStream::new(source());
        date_stream
            .execute(&slice(&frame(), 0, 2))
            .expect("first date chunk");
        let mut date_drifted = slice(&frame(), 2, 4);
        date_drifted.informative_dates_ms.remove("date_4h");
        let error = date_stream
            .execute(&date_drifted)
            .expect_err("date schema rejection");
        assert!(error
            .to_string()
            .contains("strategy.py:4864:14: forward-fill informative-date columns changed"));

        let mut identity_drift = slice(&frame(), 2, 4);
        identity_drift.identity = identity("BTC/USDT");
        let error = stream
            .execute(&identity_drift)
            .expect_err("identity rejection");
        assert!(error
            .to_string()
            .contains("strategy.py:4864:14: forward-fill frame identity changed"));
    }

    fn slice(frame: &MergedFrame, start: usize, end: usize) -> MergedFrame {
        MergedFrame {
            identity: frame.identity.clone(),
            timestamps_ms: frame.timestamps_ms[start..end].to_vec(),
            columns: frame
                .columns
                .iter()
                .map(|(name, values)| (name.clone(), values[start..end].to_vec()))
                .collect(),
            informative_dates_ms: frame
                .informative_dates_ms
                .iter()
                .map(|(name, values)| (name.clone(), values[start..end].to_vec()))
                .collect(),
        }
    }

    fn concatenate(parts: &[MergedFrame]) -> MergedFrame {
        let mut output = MergedFrame {
            identity: parts[0].identity.clone(),
            timestamps_ms: Vec::new(),
            columns: parts[0]
                .columns
                .keys()
                .map(|name| (name.clone(), Vec::new()))
                .collect(),
            informative_dates_ms: parts[0]
                .informative_dates_ms
                .keys()
                .map(|name| (name.clone(), Vec::new()))
                .collect(),
        };
        for part in parts {
            output.timestamps_ms.extend_from_slice(&part.timestamps_ms);
            for (name, values) in &part.columns {
                output.columns.get_mut(name).expect("column").extend(values);
            }
            for (name, values) in &part.informative_dates_ms {
                output
                    .informative_dates_ms
                    .get_mut(name)
                    .expect("date column")
                    .extend(values);
            }
        }
        output
    }

    fn assert_frame_bits(actual: &MergedFrame, expected: &MergedFrame) {
        assert_eq!(actual.identity, expected.identity);
        assert_eq!(actual.timestamps_ms, expected.timestamps_ms);
        assert_eq!(actual.informative_dates_ms, expected.informative_dates_ms);
        assert_eq!(
            actual.columns.keys().collect::<Vec<_>>(),
            expected.columns.keys().collect::<Vec<_>>()
        );
        for name in actual.columns.keys() {
            assert_numeric_bits(&actual.columns[name], &expected.columns[name]);
        }
    }

    fn assert_numeric_bits(actual: &[Option<f64>], expected: &[Option<f64>]) {
        assert_eq!(actual.len(), expected.len());
        for (actual, expected) in actual.iter().zip(expected) {
            match (actual, expected) {
                (Some(actual), Some(expected)) => assert_eq!(actual.to_bits(), expected.to_bits()),
                (None, None) => {}
                _ => panic!("option mismatch: {actual:?} != {expected:?}"),
            }
        }
    }
}
