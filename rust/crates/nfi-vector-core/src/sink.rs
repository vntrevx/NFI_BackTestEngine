//! Final-column sinks that keep intermediate vector buffers batch-local.

use std::collections::BTreeMap;

use crate::column::OwnedColumn;
use crate::error::VectorCoreError;

/// Final columns emitted for one source record batch.
#[derive(Debug, Clone)]
pub struct OutputBatch {
    rows: usize,
    columns: BTreeMap<String, OwnedColumn>,
}

impl OutputBatch {
    /// Construct a final batch after verifying every column has `rows` values.
    ///
    /// # Errors
    ///
    /// Returns a column-length error when a final buffer is inconsistent.
    pub fn new(
        rows: usize,
        columns: BTreeMap<String, OwnedColumn>,
    ) -> Result<Self, VectorCoreError> {
        for (name, column) in &columns {
            if column.len() != rows {
                return Err(VectorCoreError::ColumnLength {
                    column: name.clone(),
                    actual: column.len(),
                    expected: rows,
                });
            }
        }
        Ok(Self { rows, columns })
    }

    #[must_use]
    pub const fn len(&self) -> usize {
        self.rows
    }

    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.rows == 0
    }

    #[must_use]
    pub fn columns(&self) -> &BTreeMap<String, OwnedColumn> {
        &self.columns
    }

    #[must_use]
    pub fn estimated_bytes(&self) -> usize {
        self.columns
            .values()
            .map(OwnedColumn::estimated_bytes)
            .fold(0_usize, usize::saturating_add)
    }
}

/// Consumer of final columns. Implementations decide whether results persist.
pub trait BatchSink {
    /// Consume one final batch.
    ///
    /// # Errors
    ///
    /// Returns an output error when the sink cannot accept the batch.
    fn consume(&mut self, batch: OutputBatch) -> Result<(), VectorCoreError>;
}

/// Memory profile for a sink that intentionally retains no result columns.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SinkProfile {
    pub batches: usize,
    pub rows: usize,
    pub peak_batch_bytes: usize,
    pub retained_bytes: usize,
}

/// Acceptance-test sink proving output memory can remain bounded by one batch.
#[derive(Debug, Default)]
pub struct DiscardSink {
    profile: SinkProfile,
}

impl DiscardSink {
    #[must_use]
    pub const fn profile(&self) -> SinkProfile {
        self.profile
    }
}

impl BatchSink for DiscardSink {
    fn consume(&mut self, batch: OutputBatch) -> Result<(), VectorCoreError> {
        self.profile.batches = self.profile.batches.saturating_add(1);
        self.profile.rows = self.profile.rows.saturating_add(batch.len());
        self.profile.peak_batch_bytes = self.profile.peak_batch_bytes.max(batch.estimated_bytes());
        self.profile.retained_bytes = 0;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_batch_rejects_mixed_row_counts() {
        let columns = BTreeMap::from([("value".to_owned(), OwnedColumn::f64(vec![Some(1.0)]))]);
        assert!(matches!(
            OutputBatch::new(2, columns),
            Err(VectorCoreError::ColumnLength { .. })
        ));
    }

    #[test]
    fn discard_sink_retains_no_batch_buffers() {
        let mut sink = DiscardSink::default();
        for _ in 0..10 {
            sink.consume(
                OutputBatch::new(
                    64,
                    BTreeMap::from([("value".to_owned(), OwnedColumn::f64(vec![Some(1.0); 64]))]),
                )
                .expect("consistent output"),
            )
            .expect("discard cannot fail");
        }
        assert_eq!(sink.profile().batches, 10);
        assert_eq!(sink.profile().rows, 640);
        assert_eq!(sink.profile().retained_bytes, 0);
        assert!(sink.profile().peak_batch_bytes < 1_024);
    }
}
