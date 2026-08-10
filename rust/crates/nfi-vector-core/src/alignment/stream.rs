use std::collections::{BTreeMap, BTreeSet};

use crate::VectorCoreError;

use super::model::{MergeSpec, MergedFrame, NumericFrame};
use super::support::{
    informative_events, output_names, validate_frame, validate_identity, validate_ordered,
    InformativeEvent, OutputBuilder, OutputNames, StoredInformativeRow,
};

/// Bounded cursor for causally ordered alignment chunks.
///
/// Informative chunks must not contain an effective key after the maximum base
/// timestamp in the same call. This explicit backpressure rule keeps retention
/// to one informative row instead of silently buffering an unbounded future.
#[derive(Debug)]
pub struct MergeStream {
    spec: MergeSpec,
    schema: Option<StreamSchema>,
    last_base_timestamp: Option<i64>,
    last_informative_key: Option<i64>,
    last_informative: Option<StoredInformativeRow>,
    has_exact_match: bool,
}

impl MergeStream {
    /// Creates an empty bounded alignment cursor.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for an invalid merge specification.
    pub fn new(spec: MergeSpec) -> Result<Self, VectorCoreError> {
        spec.validate()?;
        Ok(Self {
            spec,
            schema: None,
            last_base_timestamp: None,
            last_informative_key: None,
            last_informative: None,
            has_exact_match: false,
        })
    }

    /// Aligns one ordered chunk without retaining historical input batches.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for mismatched identities, unordered
    /// chunks, or informative rows that have not become visible yet.
    pub fn execute(
        &mut self,
        base: &NumericFrame,
        informative: &NumericFrame,
    ) -> Result<MergedFrame, VectorCoreError> {
        validate_identity(base, &self.spec.base, &self.spec)?;
        validate_identity(informative, &self.spec.informative, &self.spec)?;
        validate_frame(base, &self.spec)?;
        validate_frame(informative, &self.spec)?;
        validate_ordered(base, &self.spec)?;
        validate_ordered(informative, &self.spec)?;
        if !self.spec.ffill
            && base.timestamps_ms.is_empty()
            && !informative.timestamps_ms.is_empty()
        {
            return Err(self
                .spec
                .error("non-ffill stream cannot retain informative exact rows without base rows"));
        }
        let names = output_names(base, informative, &self.spec)?;
        let schema = StreamSchema::new(base, informative, &names, &self.spec);
        self.validate_schema(&schema)?;
        let events = informative_events(informative, &self.spec)?;
        self.validate_progress(base, &events)?;
        let mut exact = BTreeMap::<i64, Vec<usize>>::new();
        for event in &events {
            exact
                .entry(event.key_ms)
                .or_default()
                .push(event.source_row);
        }
        self.prepare_ffill(base, informative, &events, &exact)?;

        let mut output = OutputBuilder::new(base, informative, &names);
        let mut event_cursor = 0;
        for base_row in 0..base.timestamps_ms.len() {
            let timestamp = base.timestamps_ms[base_row];
            while event_cursor < events.len() && events[event_cursor].key_ms <= timestamp {
                if self.spec.ffill {
                    self.last_informative = Some(StoredInformativeRow::from_frame(
                        informative,
                        events[event_cursor].source_row,
                    ));
                }
                event_cursor += 1;
            }
            let matches = exact.get(&timestamp).map(Vec::as_slice);
            if let Some(matches) = matches {
                self.has_exact_match = true;
                output.extend(base, base_row, Some(matches));
            } else if self.spec.ffill && self.has_exact_match {
                output.extend_stored(base, base_row, self.last_informative.as_ref());
            } else {
                output.extend(base, base_row, None);
            }
        }
        self.last_base_timestamp = base
            .timestamps_ms
            .last()
            .copied()
            .or(self.last_base_timestamp);
        self.last_informative_key = events
            .last()
            .map(|event| event.key_ms)
            .or(self.last_informative_key);
        self.schema = Some(schema);
        Ok(output.finish(base.identity.clone()))
    }

    fn validate_schema(&self, schema: &StreamSchema) -> Result<(), VectorCoreError> {
        if self
            .schema
            .as_ref()
            .is_some_and(|previous| previous != schema)
        {
            return Err(self
                .spec
                .error("base or informative numeric/date schema changed across stream calls"));
        }
        Ok(())
    }

    fn validate_progress(
        &self,
        base: &NumericFrame,
        events: &[InformativeEvent],
    ) -> Result<(), VectorCoreError> {
        if base.timestamps_ms.first().is_some_and(|timestamp| {
            self.last_base_timestamp
                .is_some_and(|last| *timestamp <= last)
        }) {
            return Err(self
                .spec
                .error("base stream timestamp did not advance across calls"));
        }
        if events.first().is_some_and(|event| {
            self.last_informative_key
                .is_some_and(|last| event.key_ms <= last)
        }) {
            return Err(self
                .spec
                .error("informative stream effective timestamp did not advance across calls"));
        }
        if events.iter().any(|event| {
            self.last_base_timestamp
                .is_some_and(|last| event.key_ms <= last)
        }) {
            return Err(self.spec.error(
                "informative stream contains a late row that could change emitted base output",
            ));
        }
        if let Some(last_base) = base.timestamps_ms.last() {
            if events.iter().any(|event| event.key_ms > *last_base) {
                return Err(self.spec.error(
                    "informative stream contains a future row; split before its effective timestamp",
                ));
            }
        }
        Ok(())
    }

    fn prepare_ffill(
        &mut self,
        base: &NumericFrame,
        informative: &NumericFrame,
        events: &[InformativeEvent],
        exact: &BTreeMap<i64, Vec<usize>>,
    ) -> Result<(), VectorCoreError> {
        // Freqtrade may repair every row before its first exact match from a
        // later historical informative row. No bounded cursor can emit a
        // leading base chunk exactly until that first match is present.
        if self.spec.ffill && !self.has_exact_match && !base.timestamps_ms.is_empty() {
            if !base
                .timestamps_ms
                .iter()
                .any(|timestamp| exact.contains_key(timestamp))
            {
                return Err(self.spec.error(
                    "ffill stream requires an exact informative match in its first nonempty base chunk",
                ));
            }
            self.has_exact_match = true;
        }
        if base.timestamps_ms.is_empty() && self.spec.ffill {
            if let Some(event) = events.last() {
                self.last_informative = Some(StoredInformativeRow::from_frame(
                    informative,
                    event.source_row,
                ));
            }
        }
        Ok(())
    }

    /// Number of informative rows retained across calls.
    #[must_use]
    pub fn retained(&self) -> usize {
        usize::from(self.last_informative.is_some())
    }
}

#[derive(Debug, Eq, PartialEq)]
struct StreamSchema {
    base_numeric: BTreeSet<String>,
    informative_numeric: BTreeSet<String>,
    base_date: String,
    informative_date: String,
    output_date: String,
}

impl StreamSchema {
    fn new(
        base: &NumericFrame,
        informative: &NumericFrame,
        names: &OutputNames,
        spec: &MergeSpec,
    ) -> Self {
        Self {
            base_numeric: base.columns.keys().cloned().collect(),
            informative_numeric: informative.columns.keys().cloned().collect(),
            base_date: "date".to_owned(),
            informative_date: spec.date_column.clone(),
            output_date: names.date.clone(),
        }
    }
}
