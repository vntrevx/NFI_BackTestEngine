use std::collections::{BTreeMap, BTreeSet};

use crate::VectorCoreError;

use super::model::{FrameIdentity, MergeSpec, MergedFrame, NumericFrame};

#[derive(Clone, Debug)]
pub(super) struct InformativeEvent {
    pub(super) key_ms: i64,
    pub(super) source_row: usize,
}

#[derive(Clone, Debug)]
pub(super) struct StoredInformativeRow {
    pub(super) timestamp_ms: i64,
    pub(super) values: BTreeMap<String, Option<f64>>,
}

impl StoredInformativeRow {
    pub(super) fn from_frame(frame: &NumericFrame, source_row: usize) -> Self {
        Self {
            timestamp_ms: frame.timestamps_ms[source_row],
            values: frame
                .columns
                .iter()
                .map(|(name, values)| (name.clone(), values[source_row]))
                .collect(),
        }
    }
}

pub(super) struct OutputNames {
    pub(super) numeric: BTreeMap<String, String>,
    pub(super) date: String,
}

pub(super) struct OutputBuilder<'frame> {
    timestamps_ms: Vec<i64>,
    columns: BTreeMap<String, Vec<Option<f64>>>,
    dates: BTreeMap<String, Vec<Option<i64>>>,
    informative: &'frame NumericFrame,
    names: OutputNames,
}

impl<'frame> OutputBuilder<'frame> {
    pub(super) fn new(
        base: &NumericFrame,
        informative: &'frame NumericFrame,
        names: &OutputNames,
    ) -> Self {
        let mut columns = base
            .columns
            .keys()
            .map(|name| (name.clone(), Vec::new()))
            .collect::<BTreeMap<_, _>>();
        for output_name in names.numeric.values() {
            columns.insert(output_name.clone(), Vec::new());
        }
        let dates = BTreeMap::from([(names.date.clone(), Vec::new())]);
        Self {
            timestamps_ms: Vec::new(),
            columns,
            dates,
            informative,
            names: OutputNames {
                numeric: names.numeric.clone(),
                date: names.date.clone(),
            },
        }
    }

    pub(super) fn extend(
        &mut self,
        base: &NumericFrame,
        base_row: usize,
        matches: Option<&[usize]>,
    ) {
        match matches {
            Some(rows) => {
                for row in rows {
                    self.extend_one(base, base_row, Some(*row));
                }
            }
            None => self.extend_one(base, base_row, None),
        }
    }

    pub(super) fn extend_stored(
        &mut self,
        base: &NumericFrame,
        base_row: usize,
        stored: Option<&StoredInformativeRow>,
    ) {
        for (name, values) in &base.columns {
            self.columns
                .get_mut(name)
                .expect("base column exists")
                .push(values[base_row]);
        }
        for (source, output) in &self.names.numeric {
            self.columns
                .get_mut(output)
                .expect("informative column exists")
                .push(match stored {
                    Some(row) => row.values.get(source).copied().flatten(),
                    None => Some(f64::NAN),
                });
        }
        self.dates
            .get_mut(&self.names.date)
            .expect("date column exists")
            .push(stored.map(|row| row.timestamp_ms));
        self.timestamps_ms.push(base.timestamps_ms[base_row]);
    }

    fn extend_one(&mut self, base: &NumericFrame, base_row: usize, informative_row: Option<usize>) {
        for (name, values) in &base.columns {
            self.columns
                .get_mut(name)
                .expect("base column exists")
                .push(values[base_row]);
        }
        for (source, output) in &self.names.numeric {
            self.columns
                .get_mut(output)
                .expect("informative column exists")
                .push(
                    informative_row
                        .map_or(Some(f64::NAN), |row| self.informative.columns[source][row]),
                );
        }
        self.dates
            .get_mut(&self.names.date)
            .expect("date column exists")
            .push(informative_row.map(|row| self.informative.timestamps_ms[row]));
        self.timestamps_ms.push(base.timestamps_ms[base_row]);
    }

    pub(super) fn finish(self, identity: FrameIdentity) -> MergedFrame {
        MergedFrame {
            identity,
            timestamps_ms: self.timestamps_ms,
            columns: self.columns,
            informative_dates_ms: self.dates,
        }
    }
}

pub(super) fn validate_identity(
    frame: &NumericFrame,
    expected: &FrameIdentity,
    spec: &MergeSpec,
) -> Result<(), VectorCoreError> {
    if &frame.identity != expected {
        return Err(spec.error(format!(
            "frame identity is {} {} but merge requires {} {}",
            frame.identity.pair,
            frame.identity.timeframe.as_str(),
            expected.pair,
            expected.timeframe.as_str()
        )));
    }
    Ok(())
}

pub(super) fn validate_frame(
    frame: &NumericFrame,
    spec: &MergeSpec,
) -> Result<(), VectorCoreError> {
    frame
        .validate()
        .map_err(|_| spec.error("frame has invalid column names or row lengths"))
}

pub(super) fn output_names(
    base: &NumericFrame,
    informative: &NumericFrame,
    spec: &MergeSpec,
) -> Result<OutputNames, VectorCoreError> {
    let mut occupied = base.columns.keys().cloned().collect::<BTreeSet<_>>();
    let date = spec.output_name(&spec.date_column)?;
    if !occupied.insert(date.clone()) {
        return Err(spec.error(format!(
            "informative date output {date:?} collides with a base column"
        )));
    }
    let mut numeric = BTreeMap::new();
    for name in informative.columns.keys() {
        let output = spec.output_name(name)?;
        if !occupied.insert(output.clone()) {
            return Err(spec.error(format!("informative output column {output:?} collides")));
        }
        numeric.insert(name.clone(), output);
    }
    Ok(OutputNames { numeric, date })
}

pub(super) fn informative_events(
    frame: &NumericFrame,
    spec: &MergeSpec,
) -> Result<Vec<InformativeEvent>, VectorCoreError> {
    let mut events = frame
        .timestamps_ms
        .iter()
        .enumerate()
        .map(|(source_row, timestamp)| {
            Ok(InformativeEvent {
                key_ms: spec.effective_timestamp(*timestamp)?,
                source_row,
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    events.sort_by_key(|event| event.key_ms);
    Ok(events)
}

pub(super) fn validate_ordered(
    frame: &NumericFrame,
    spec: &MergeSpec,
) -> Result<(), VectorCoreError> {
    if frame.timestamps_ms.windows(2).any(|pair| pair[0] > pair[1]) {
        return Err(spec.error(format!(
            "frame {} {} timestamps are not ordered",
            frame.identity.pair,
            frame.identity.timeframe.as_str()
        )));
    }
    Ok(())
}

pub(super) fn ordered_by_timestamp(frame: &NumericFrame) -> NumericFrame {
    let mut rows = (0..frame.timestamps_ms.len()).collect::<Vec<_>>();
    rows.sort_by_key(|row| frame.timestamps_ms[*row]);
    NumericFrame {
        identity: frame.identity.clone(),
        timestamps_ms: rows.iter().map(|row| frame.timestamps_ms[*row]).collect(),
        columns: frame
            .columns
            .iter()
            .map(|(name, values)| {
                (
                    name.clone(),
                    rows.iter().map(|row| values[*row]).collect::<Vec<_>>(),
                )
            })
            .collect(),
    }
}
