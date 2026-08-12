//! Identity-aware dataframe values used by informative indicator programs.
//!
//! The generic executor must not use the base Arrow batch length for an
//! informative frame.  This module keeps every dataframe's identity, row
//! count, visible columns, and typed informative dates together.  Catalog
//! sources remain borrowed; projections and drops are cheap visibility
//! overlays, while only an actual merge owns new column buffers.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, OnceLock};

use serde_json::{Map, Value};

use crate::alignment::{
    merge, FrameCatalog, FrameIdentity, MergeSpec, NumericFrame, SourceLocation, Timeframe,
};
use crate::column::{OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::program::{IndicatorProgram, ProgramNode};

const BASE_DATE_COLUMN: &str = "date";

#[derive(Clone, Debug)]
enum FrameStorage<'catalog> {
    Borrowed(&'catalog NumericFrame),
    Owned(Arc<NumericFrame>),
}

impl FrameStorage<'_> {
    fn frame(&self) -> &NumericFrame {
        match self {
            Self::Borrowed(frame) => frame,
            Self::Owned(frame) => frame,
        }
    }
}

/// One runtime dataframe and its cheap projection/drop overlay.
///
/// `NumericFrame` stores its primary `date` column as `timestamps_ms`.
/// Informative date columns created by a merge remain typed timestamps in the
/// separate map below, instead of being silently coerced to numeric columns.
#[derive(Clone, Debug)]
pub(super) struct RuntimeFrame<'catalog> {
    storage: FrameStorage<'catalog>,
    source_columns: Arc<BTreeMap<String, OnceLock<OwnedColumn>>>,
    timestamp_columns: Arc<BTreeMap<String, Vec<Option<i64>>>>,
    overlays: Arc<BTreeMap<String, OwnedColumn>>,
    visible_columns: BTreeSet<String>,
}

impl<'catalog> RuntimeFrame<'catalog> {
    /// Borrow a validated source without copying candle buffers.
    #[must_use]
    pub(super) fn borrowed(frame: &'catalog NumericFrame) -> Self {
        let mut visible_columns = frame.columns.keys().cloned().collect::<BTreeSet<_>>();
        visible_columns.insert(BASE_DATE_COLUMN.to_owned());
        Self {
            storage: FrameStorage::Borrowed(frame),
            source_columns: source_column_cache(frame),
            timestamp_columns: Arc::new(BTreeMap::new()),
            overlays: Arc::new(BTreeMap::new()),
            visible_columns,
        }
    }

    fn owned(frame: NumericFrame, timestamp_columns: BTreeMap<String, Vec<Option<i64>>>) -> Self {
        let mut visible_columns = frame.columns.keys().cloned().collect::<BTreeSet<_>>();
        visible_columns.extend(timestamp_columns.keys().cloned());
        visible_columns.insert(BASE_DATE_COLUMN.to_owned());
        Self {
            source_columns: source_column_cache(&frame),
            storage: FrameStorage::Owned(Arc::new(frame)),
            timestamp_columns: Arc::new(timestamp_columns),
            overlays: Arc::new(BTreeMap::new()),
            visible_columns,
        }
    }

    #[must_use]
    pub(super) fn identity(&self) -> &FrameIdentity {
        &self.storage.frame().identity
    }

    #[must_use]
    pub(super) fn len(&self) -> usize {
        self.storage.frame().timestamps_ms.len()
    }

    #[must_use]
    pub(super) fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[must_use]
    pub(super) fn has_column(&self, name: &str) -> bool {
        self.visible_columns.contains(name)
    }

    pub(super) fn column_names(&self) -> impl Iterator<Item = &str> {
        self.visible_columns.iter().map(String::as_str)
    }

    #[must_use]
    pub(super) fn column_type(&self, name: &str) -> Option<ValueType> {
        if !self.has_column(name) {
            return None;
        }
        if let Some(column) = self.overlays.get(name) {
            return Some(column.as_view().value_type());
        }
        if name == BASE_DATE_COLUMN || self.timestamp_columns.contains_key(name) {
            Some(ValueType::TimestampMs)
        } else if self.storage.frame().columns.contains_key(name) {
            Some(ValueType::F64)
        } else {
            None
        }
    }

    /// Materialize one visible frame column for the existing `NodeValue`
    /// column representation.  The runtime can call this only when a compiled
    /// column-read reaches the corresponding dataframe handle.
    #[must_use]
    pub(super) fn owned_column(&self, name: &str) -> Option<OwnedColumn> {
        if !self.has_column(name) {
            return None;
        }
        if let Some(column) = self.overlays.get(name) {
            return Some(column.clone());
        }
        if name == BASE_DATE_COLUMN {
            return Some(OwnedColumn::timestamp_ms(
                self.storage
                    .frame()
                    .timestamps_ms
                    .iter()
                    .copied()
                    .map(Some)
                    .collect(),
            ));
        }
        if let Some(values) = self.timestamp_columns.get(name) {
            return Some(OwnedColumn::timestamp_ms(values.clone()));
        }
        let values = self.storage.frame().columns.get(name)?;
        self.source_columns.get(name).map(|cached| {
            cached
                .get_or_init(|| OwnedColumn::f64(values.clone()))
                .clone()
        })
    }

    /// Add or replace one typed dataframe column without copying the source
    /// frame. The overlay owns the Arrow buffer and retains its physical type.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for an empty name, a row-count mismatch,
    /// or a collision when the compiled write requested rejection.
    pub(super) fn with_column(
        mut self,
        name: impl Into<String>,
        column: OwnedColumn,
        collision_reject: bool,
        source: &SourceLocation,
    ) -> Result<Self, VectorCoreError> {
        let name = name.into();
        if name.is_empty() {
            return Err(source.error("dataframe overlay column name is empty"));
        }
        if column.len() != self.len() {
            return Err(source.error(format!(
                "dataframe overlay column {name:?} has {} rows; expected {}",
                column.len(),
                self.len()
            )));
        }
        if collision_reject && self.has_column(&name) {
            return Err(source.error(format!(
                "dataframe overlay column {name:?} collides with an existing column"
            )));
        }
        Arc::make_mut(&mut self.overlays).insert(name.clone(), column);
        self.visible_columns.insert(name);
        Ok(self)
    }

    fn require_non_empty(&self, source: &SourceLocation) -> Result<Self, VectorCoreError> {
        if self.is_empty() {
            return Err(source.error(format!(
                "frame {} {} is empty",
                self.identity().pair,
                self.identity().timeframe.as_str()
            )));
        }
        Ok(self.clone())
    }

    fn project(
        &self,
        always_keep: &BTreeSet<String>,
        drop_candidates: &BTreeSet<String>,
        keep: &BTreeSet<String>,
    ) -> Self {
        let mut projected = self.clone();
        projected.visible_columns.retain(|column| {
            always_keep.contains(column)
                || !drop_candidates.contains(column)
                || keep.contains(column)
        });
        projected
    }

    fn drop_if_present(&self, column: &str) -> Self {
        let mut dropped = self.clone();
        dropped.visible_columns.remove(column);
        dropped
    }

    fn numeric_for_merge(
        &self,
        date_column: &str,
        source: &SourceLocation,
    ) -> Result<(NumericFrame, BTreeSet<String>), VectorCoreError> {
        if !self.has_column(date_column) {
            return Err(source.error(format!(
                "frame {} {} has no visible join column {date_column:?}",
                self.identity().pair,
                self.identity().timeframe.as_str()
            )));
        }
        let timestamps_ms = self.required_timestamp_column(date_column, source)?;
        let mut columns = self
            .storage
            .frame()
            .columns
            .iter()
            .filter(|(name, _)| self.has_column(name) && !self.overlays.contains_key(name.as_str()))
            .map(|(name, values)| (name.clone(), values.clone()))
            .collect::<BTreeMap<_, _>>();
        let mut timestamp_names = BTreeSet::new();
        for (name, values) in self.timestamp_columns.iter().filter(|(name, _)| {
            self.has_column(name)
                && name.as_str() != date_column
                && !self.overlays.contains_key(name.as_str())
        }) {
            if columns.contains_key(name) {
                return Err(source.error(format!(
                    "frame column {name:?} has conflicting numeric and timestamp types"
                )));
            }
            let encoded = values
                .iter()
                .map(|value| {
                    value
                        .map(|value| exact_timestamp_as_f64(value, name, source))
                        .transpose()
                })
                .collect::<Result<Vec<_>, _>>()?;
            columns.insert(name.clone(), encoded);
            timestamp_names.insert(name.clone());
        }
        for (name, column) in self
            .overlays
            .iter()
            .filter(|(name, _)| self.has_column(name) && name.as_str() != date_column)
        {
            match column.as_view().value_type() {
                ValueType::F64 => {
                    columns.insert(
                        name.clone(),
                        (0..self.len())
                            .map(|row| column.as_view().f64_at(row))
                            .collect(),
                    );
                }
                ValueType::TimestampMs => {
                    let encoded = (0..self.len())
                        .map(|row| {
                            column
                                .as_view()
                                .timestamp_ms_at(row)
                                .map(|value| exact_timestamp_as_f64(value, name, source))
                                .transpose()
                        })
                        .collect::<Result<Vec<_>, _>>()?;
                    columns.insert(name.clone(), encoded);
                    timestamp_names.insert(name.clone());
                }
                value_type => {
                    return Err(source.error(format!(
                        "visible dataframe overlay {name:?} has non-mergeable type {}",
                        value_type.label()
                    )));
                }
            }
        }
        Ok((
            NumericFrame {
                identity: self.identity().clone(),
                timestamps_ms,
                columns,
            },
            timestamp_names,
        ))
    }

    fn required_timestamp_column(
        &self,
        name: &str,
        source: &SourceLocation,
    ) -> Result<Vec<i64>, VectorCoreError> {
        if let Some(column) = self.overlays.get(name) {
            if column.as_view().value_type() != ValueType::TimestampMs {
                return Err(source.error(format!(
                    "frame join column {name:?} has type {}; expected timestamp",
                    column.as_view().value_type().label()
                )));
            }
            return (0..self.len())
                .map(|row| {
                    column.as_view().timestamp_ms_at(row).ok_or_else(|| {
                        source.error(format!("frame join column {name:?} is null at row {row}"))
                    })
                })
                .collect();
        }
        if name == BASE_DATE_COLUMN {
            return Ok(self.storage.frame().timestamps_ms.clone());
        }
        self.timestamp_columns
            .get(name)
            .ok_or_else(|| {
                source.error(format!(
                    "frame join column {name:?} is not a timestamp column"
                ))
            })?
            .iter()
            .enumerate()
            .map(|(row, value)| {
                value.ok_or_else(|| {
                    source.error(format!("frame join column {name:?} is null at row {row}"))
                })
            })
            .collect()
    }
}

fn source_column_cache(frame: &NumericFrame) -> Arc<BTreeMap<String, OnceLock<OwnedColumn>>> {
    Arc::new(
        frame
            .columns
            .keys()
            .map(|name| (name.clone(), OnceLock::new()))
            .collect(),
    )
}

/// Immutable external context for frame and metadata opcodes.
#[derive(Debug)]
pub(super) struct FrameRuntime<'catalog> {
    catalog: &'catalog FrameCatalog,
    metadata: &'catalog BTreeMap<String, String>,
}

impl<'catalog> FrameRuntime<'catalog> {
    #[must_use]
    pub(super) const fn new(
        catalog: &'catalog FrameCatalog,
        metadata: &'catalog BTreeMap<String, String>,
    ) -> Self {
        Self { catalog, metadata }
    }

    pub(super) fn metadata_read(
        &self,
        node: &ProgramNode,
        source: &SourceLocation,
    ) -> Result<String, VectorCoreError> {
        let key = string_parameter(node, "key", source)?;
        self.metadata.get(key).cloned().ok_or_else(|| {
            source.error(format!(
                "runtime metadata has no string value for key {key:?}"
            ))
        })
    }

    pub(super) fn frame_source(
        &self,
        node: &ProgramNode,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let timeframe_value = string_parameter(node, "timeframe", source)?;
        let timeframe = Timeframe::parse(timeframe_value.to_owned())
            .map_err(|error| source.error(format!("invalid frame-source timeframe: {error}")))?;
        let selector = object_parameter(node, "pair", source)?;
        let kind = object_string(selector, "kind", node, source)?;
        let pair = match kind {
            "literal" => object_string(selector, "value", node, source)?.to_owned(),
            "metadata" => {
                let key = object_string(selector, "key", node, source)?;
                self.metadata.get(key).cloned().ok_or_else(|| {
                    source.error(format!(
                        "runtime metadata has no string value for frame pair key {key:?}"
                    ))
                })?
            }
            other => {
                return Err(source.error(format!(
                    "frame-source pair selector kind {other:?} is unsupported"
                )));
            }
        };
        let identity = FrameIdentity::new(pair, timeframe)
            .map_err(|error| source.error(format!("invalid frame-source identity: {error}")))?;
        Ok(RuntimeFrame::borrowed(
            self.catalog.lookup(&identity, source)?,
        ))
    }

    pub(super) fn frame_nonempty(
        frame: &RuntimeFrame<'catalog>,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        frame.require_non_empty(source)
    }

    pub(super) fn frame_project(
        node: &ProgramNode,
        frame: &RuntimeFrame<'catalog>,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let always_keep = string_set_parameter(node, "always_keep", source)?;
        let drop_candidates = string_set_parameter(node, "drop_candidates", source)?;
        let keep = string_set_parameter(node, "keep", source)?;
        Ok(frame.project(&always_keep, &drop_candidates, &keep))
    }

    pub(super) fn frame_drop_if_present(
        node: &ProgramNode,
        frame: &RuntimeFrame<'catalog>,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let column = string_parameter(node, "column", source)?;
        Ok(frame.drop_if_present(column))
    }

    pub(super) fn informative_merge(
        node: &ProgramNode,
        base: &RuntimeFrame<'catalog>,
        informative: &RuntimeFrame<'catalog>,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let base_timeframe = parse_timeframe_parameter(node, "base_timeframe", source)?;
        let informative_timeframe =
            parse_timeframe_parameter(node, "informative_timeframe", source)?;
        let date_column = string_parameter(node, "date_column", source)?;
        let ffill = bool_parameter(node, "ffill", source)?;
        let append_timeframe = bool_parameter(node, "append_timeframe", source)?;
        let suffix = optional_string_parameter(node, "suffix", source)?;
        let expected_base = FrameIdentity::new(base.identity().pair.clone(), base_timeframe)
            .map_err(|error| source.error(format!("invalid base merge identity: {error}")))?;
        let expected_informative =
            FrameIdentity::new(informative.identity().pair.clone(), informative_timeframe)
                .map_err(|error| {
                    source.error(format!("invalid informative merge identity: {error}"))
                })?;
        let spec = MergeSpec {
            base: expected_base,
            informative: expected_informative,
            ffill,
            append_timeframe,
            suffix,
            date_column: date_column.to_owned(),
            source: source.clone(),
        };
        let (base_numeric, base_timestamps) = base.numeric_for_merge(BASE_DATE_COLUMN, source)?;
        let (informative_numeric, informative_timestamps) =
            informative.numeric_for_merge(date_column, source)?;
        let informative_suffix = output_suffix(&spec, source)?;
        let informative_timestamp_outputs = informative_timestamps
            .into_iter()
            .map(|name| (format!("{name}_{informative_suffix}"), name))
            .collect::<BTreeMap<_, _>>();
        let merged = merge(&base_numeric, &informative_numeric, &spec)?;
        let mut numeric_columns = BTreeMap::new();
        let mut timestamp_columns = merged.informative_dates_ms;
        for (name, values) in merged.columns {
            if base_timestamps.contains(&name) || informative_timestamp_outputs.contains_key(&name)
            {
                let decoded = values
                    .into_iter()
                    .map(|value| {
                        value
                            .map(|value| exact_f64_as_timestamp(value, &name, source))
                            .transpose()
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                timestamp_columns.insert(name, decoded);
            } else {
                numeric_columns.insert(name, values);
            }
        }
        Ok(RuntimeFrame::owned(
            NumericFrame {
                identity: merged.identity,
                timestamps_ms: merged.timestamps_ms,
                columns: numeric_columns,
            },
            timestamp_columns,
        ))
    }
}

/// Convert the program source map into the alignment layer's located error
/// contract. A validated program has one entry for every node; missing entries
/// still fail closed instead of fabricating a strategy location.
pub(super) fn node_source(
    program: &IndicatorProgram,
    node: &ProgramNode,
) -> Result<SourceLocation, VectorCoreError> {
    let location = program.source_map.get(&node.id).ok_or_else(|| {
        VectorCoreError::InvalidProgram(format!("node {} has no source-map entry", node.id))
    })?;
    Ok(SourceLocation::new(
        &node.id,
        &location.path,
        location.line,
        location.column,
    ))
}

fn output_suffix(spec: &MergeSpec, source: &SourceLocation) -> Result<String, VectorCoreError> {
    if spec.append_timeframe {
        if spec
            .suffix
            .as_ref()
            .is_some_and(|suffix| !suffix.is_empty())
        {
            return Err(source.error("suffix cannot be combined with append_timeframe"));
        }
        Ok(spec.informative.timeframe.as_str().to_owned())
    } else {
        spec.suffix
            .as_ref()
            .filter(|suffix| !suffix.is_empty())
            .cloned()
            .ok_or_else(|| source.error("informative merge requires a non-empty suffix"))
    }
}

fn exact_timestamp_as_f64(
    value: i64,
    column: &str,
    source: &SourceLocation,
) -> Result<f64, VectorCoreError> {
    const MAX_EXACT_F64_INTEGER: i64 = 1_i64 << f64::MANTISSA_DIGITS;
    if !(-MAX_EXACT_F64_INTEGER..=MAX_EXACT_F64_INTEGER).contains(&value) {
        return Err(source.error(format!(
            "timestamp column {column:?} has a value that cannot be represented exactly"
        )));
    }
    #[allow(clippy::cast_precision_loss)]
    Ok(value as f64)
}

fn exact_f64_as_timestamp(
    value: f64,
    column: &str,
    source: &SourceLocation,
) -> Result<i64, VectorCoreError> {
    const MAX_EXACT_F64_INTEGER: f64 = 9_007_199_254_740_992.0;
    if !value.is_finite()
        || value.fract() != 0.0
        || !(-MAX_EXACT_F64_INTEGER..=MAX_EXACT_F64_INTEGER).contains(&value)
    {
        return Err(source.error(format!(
            "timestamp column {column:?} was not preserved as an integer"
        )));
    }
    #[allow(clippy::cast_possible_truncation)]
    let decoded = value as i64;
    Ok(decoded)
}

fn parse_timeframe_parameter(
    node: &ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<Timeframe, VectorCoreError> {
    let value = string_parameter(node, name, source)?;
    Timeframe::parse(value.to_owned())
        .map_err(|error| source.error(format!("invalid {name}: {error}")))
}

fn string_parameter<'node>(
    node: &'node ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<&'node str, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| {
            source.error(format!(
                "node {} requires string parameter {name:?}",
                node.id
            ))
        })
}

fn optional_string_parameter(
    node: &ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<Option<String>, VectorCoreError> {
    match node.parameters.get(name) {
        Some(Value::Null) | None => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        _ => Err(source.error(format!(
            "node {} requires nullable string parameter {name:?}",
            node.id
        ))),
    }
}

fn bool_parameter(
    node: &ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<bool, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_bool)
        .ok_or_else(|| source.error(format!("node {} requires bool parameter {name:?}", node.id)))
}

fn object_parameter<'node>(
    node: &'node ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<&'node Map<String, Value>, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_object)
        .ok_or_else(|| {
            source.error(format!(
                "node {} requires object parameter {name:?}",
                node.id
            ))
        })
}

fn object_string<'object>(
    object: &'object Map<String, Value>,
    name: &str,
    node: &ProgramNode,
    source: &SourceLocation,
) -> Result<&'object str, VectorCoreError> {
    object.get(name).and_then(Value::as_str).ok_or_else(|| {
        source.error(format!(
            "node {} pair selector requires string field {name:?}",
            node.id
        ))
    })
}

fn string_set_parameter(
    node: &ProgramNode,
    name: &str,
    source: &SourceLocation,
) -> Result<BTreeSet<String>, VectorCoreError> {
    let values = node
        .parameters
        .get(name)
        .and_then(Value::as_array)
        .ok_or_else(|| {
            source.error(format!(
                "node {} requires array parameter {name:?}",
                node.id
            ))
        })?;
    values
        .iter()
        .map(|value| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                source.error(format!(
                    "node {} parameter {name:?} must contain only strings",
                    node.id
                ))
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::program::Lookback;

    fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
    }

    fn frame(
        pair: &str,
        timeframe: &str,
        timestamps_ms: Vec<i64>,
        columns: impl IntoIterator<Item = (&'static str, Vec<Option<f64>>)>,
    ) -> NumericFrame {
        NumericFrame {
            identity: identity(pair, timeframe),
            timestamps_ms,
            columns: columns
                .into_iter()
                .map(|(name, values)| (name.to_owned(), values))
                .collect(),
        }
    }

    fn node(op: &str, parameters: Map<String, Value>) -> ProgramNode {
        ProgramNode {
            id: "n7".to_owned(),
            function: "f1".to_owned(),
            source_order: 7,
            op: op.to_owned(),
            value_type: "dataframe".to_owned(),
            inputs: Vec::new(),
            parameters,
            lookback: Lookback {
                kind: "finite".to_owned(),
                candles: Some(0),
                expression: None,
                causal: true,
            },
        }
    }

    fn source() -> SourceLocation {
        SourceLocation::new("n7", "NostalgiaForInfinityX7.py", 4819, 15)
    }

    #[test]
    fn literal_and_metadata_sources_resolve_exact_identity_without_copy() {
        let eth = frame("ETH/USDT", "1h", vec![0], [("close", vec![Some(7.0)])]);
        let btc = frame(
            "BTC/USDT",
            "4h",
            vec![0, 1],
            [("close", vec![Some(1.0), Some(2.0)])],
        );
        let catalog = FrameCatalog::new([(eth.identity.clone(), eth), (btc.identity.clone(), btc)])
            .expect("catalog");
        let metadata = BTreeMap::from([("pair".to_owned(), "ETH/USDT".to_owned())]);
        let runtime = FrameRuntime::new(&catalog, &metadata);
        let literal = node(
            "frame-source",
            serde_json::from_value(serde_json::json!({
                "pair": {"kind": "literal", "value": "BTC/USDT"},
                "timeframe": "4h"
            }))
            .expect("parameters"),
        );
        let selected = runtime
            .frame_source(&literal, &source())
            .expect("literal source");
        assert_eq!(selected.identity(), &identity("BTC/USDT", "4h"));
        assert_eq!(selected.len(), 2);

        let dynamic = node(
            "frame-source",
            serde_json::from_value(serde_json::json!({
                "pair": {"kind": "metadata", "key": "pair"},
                "timeframe": "1h"
            }))
            .expect("parameters"),
        );
        assert_eq!(
            runtime
                .frame_source(&dynamic, &source())
                .expect("metadata source")
                .identity(),
            &identity("ETH/USDT", "1h")
        );
    }

    #[test]
    fn metadata_and_empty_frame_errors_keep_strategy_location() {
        let empty = frame("ETH/USDT", "1h", Vec::new(), []);
        let catalog = FrameCatalog::new([(empty.identity.clone(), empty)]).expect("catalog");
        let metadata = BTreeMap::new();
        let runtime = FrameRuntime::new(&catalog, &metadata);
        let metadata_node = node(
            "metadata-read",
            serde_json::from_value(serde_json::json!({"key": "pair"})).expect("parameters"),
        );
        let error = runtime
            .metadata_read(&metadata_node, &source())
            .expect_err("missing metadata");
        assert!(matches!(
            error,
            VectorCoreError::Execution { node, message }
                if node == "n7"
                    && message.starts_with("NostalgiaForInfinityX7.py:4819:15:")
        ));

        let selected = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "1h"), &source())
                .expect("empty source"),
        );
        let error = FrameRuntime::frame_nonempty(&selected, &source()).expect_err("empty frame");
        assert!(matches!(
            error,
            VectorCoreError::Execution { node, message }
                if node == "n7"
                    && message.contains("NostalgiaForInfinityX7.py:4819:15:")
                    && message.contains("ETH/USDT 1h is empty")
        ));
    }

    #[test]
    fn projection_and_drop_are_frame_local_visibility_overlays() {
        let input = frame(
            "ETH/USDT",
            "1h",
            vec![0, 1, 2],
            [
                ("open", vec![Some(1.0); 3]),
                ("close", vec![Some(2.0); 3]),
                ("RSI_14", vec![Some(3.0); 3]),
            ],
        );
        let catalog = FrameCatalog::new([(input.identity.clone(), input)]).expect("catalog");
        let original = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "1h"), &source())
                .expect("source"),
        );
        let projection = node(
            "frame-project",
            serde_json::from_value(serde_json::json!({
                "always_keep": ["date"],
                "drop_candidates": ["open", "close", "volume"],
                "keep": ["close"]
            }))
            .expect("parameters"),
        );
        let projected =
            FrameRuntime::frame_project(&projection, &original, &source()).expect("projection");
        assert_eq!(projected.len(), 3);
        assert_eq!(
            projected.column_names().collect::<Vec<_>>(),
            ["RSI_14", "close", "date"]
        );
        assert!(original.has_column("open"));

        let drop_node = node(
            "frame-drop-if-present",
            serde_json::from_value(serde_json::json!({"column": "close"})).expect("parameters"),
        );
        let dropped =
            FrameRuntime::frame_drop_if_present(&drop_node, &projected, &source()).expect("drop");
        assert!(!dropped.has_column("close"));
        assert!(projected.has_column("close"));
    }

    #[test]
    fn exact_non_ffill_merge_uses_each_frames_own_row_count_and_keeps_dates_typed() {
        let base = frame(
            "ETH/USDT",
            "5m",
            vec![0, 3_300_000, 3_600_000],
            [("close", vec![Some(10.0), Some(11.0), Some(12.0)])],
        );
        let informative = frame(
            "BTC/USDT",
            "1h",
            vec![0, 3_600_000],
            [("RSI_14", vec![Some(40.0), Some(50.0)])],
        );
        let catalog = FrameCatalog::new([
            (base.identity.clone(), base),
            (informative.identity.clone(), informative),
        ])
        .expect("catalog");
        let base = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "5m"), &source())
                .expect("base"),
        );
        let informative = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("BTC/USDT", "1h"), &source())
                .expect("informative"),
        );
        let merge_node = node(
            "informative-merge",
            serde_json::from_value(serde_json::json!({
                "base_timeframe": "5m",
                "informative_timeframe": "1h",
                "ffill": false,
                "append_timeframe": true,
                "date_column": "date",
                "suffix": null
            }))
            .expect("parameters"),
        );
        let merged = FrameRuntime::informative_merge(&merge_node, &base, &informative, &source())
            .expect("merge");
        assert_eq!(merged.len(), 3);
        assert_eq!(merged.owned_column("RSI_14_1h").expect("numeric").len(), 3);
        assert_eq!(merged.column_type("date_1h"), Some(ValueType::TimestampMs));
        let dates = merged.owned_column("date_1h").expect("date");
        assert_eq!(dates.as_view().timestamp_ms_at(0), None);
        assert_eq!(dates.as_view().timestamp_ms_at(1), Some(0));
        assert_eq!(dates.as_view().timestamp_ms_at(2), None);
    }

    #[test]
    fn typed_column_overlays_are_cheap_visible_and_shape_checked() {
        let input = frame(
            "ETH/USDT",
            "5m",
            vec![0, 300_000],
            [("close", vec![Some(10.0), Some(11.0)])],
        );
        let catalog = FrameCatalog::new([(input.identity.clone(), input)]).expect("catalog");
        let original = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "5m"), &source())
                .expect("source"),
        );
        let with_numeric = original
            .clone()
            .with_column(
                "RSI_14",
                OwnedColumn::f64(vec![Some(40.0), Some(41.0)]),
                true,
                &source(),
            )
            .expect("numeric overlay");
        let with_bool = with_numeric
            .with_column(
                "protection",
                OwnedColumn::boolean(vec![Some(true), Some(false)]),
                true,
                &source(),
            )
            .expect("bool overlay");
        let with_timestamp = with_bool
            .with_column(
                "observed_at",
                OwnedColumn::timestamp_ms(vec![Some(0), Some(300_000)]),
                true,
                &source(),
            )
            .expect("timestamp overlay");

        assert!(!original.has_column("RSI_14"));
        assert_eq!(with_timestamp.column_type("RSI_14"), Some(ValueType::F64));
        assert_eq!(
            with_timestamp.column_type("protection"),
            Some(ValueType::Bool)
        );
        assert_eq!(
            with_timestamp.column_type("observed_at"),
            Some(ValueType::TimestampMs)
        );
        assert_eq!(
            with_timestamp
                .owned_column("protection")
                .expect("bool")
                .as_view()
                .bool_at(1),
            Some(false)
        );

        let collision = with_timestamp
            .clone()
            .with_column(
                "close",
                OwnedColumn::f64(vec![Some(1.0), Some(2.0)]),
                true,
                &source(),
            )
            .expect_err("collision");
        assert!(matches!(
            collision,
            VectorCoreError::Execution { message, .. } if message.contains("collides")
        ));
        let wrong_rows = with_timestamp
            .with_column("bad", OwnedColumn::f64(vec![Some(1.0)]), true, &source())
            .expect_err("row mismatch");
        assert!(matches!(
            wrong_rows,
            VectorCoreError::Execution { message, .. }
                if message.contains("has 1 rows; expected 2")
        ));
    }

    #[test]
    fn merge_rejects_visible_bool_overlay_but_accepts_it_after_drop() {
        let base = frame(
            "ETH/USDT",
            "5m",
            vec![3_300_000],
            [("close", vec![Some(10.0)])],
        );
        let informative = frame("ETH/USDT", "1h", vec![0], [("RSI_14", vec![Some(40.0)])]);
        let catalog = FrameCatalog::new([
            (base.identity.clone(), base),
            (informative.identity.clone(), informative),
        ])
        .expect("catalog");
        let base = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "5m"), &source())
                .expect("base"),
        )
        .with_column(
            "protection",
            OwnedColumn::boolean(vec![Some(true)]),
            true,
            &source(),
        )
        .expect("bool overlay");
        let informative = RuntimeFrame::borrowed(
            catalog
                .lookup(&identity("ETH/USDT", "1h"), &source())
                .expect("informative"),
        );
        let merge_node = node(
            "informative-merge",
            serde_json::from_value(serde_json::json!({
                "base_timeframe": "5m",
                "informative_timeframe": "1h",
                "ffill": false,
                "append_timeframe": true,
                "date_column": "date",
                "suffix": null
            }))
            .expect("parameters"),
        );
        let error = FrameRuntime::informative_merge(&merge_node, &base, &informative, &source())
            .expect_err("bool cannot cross numeric alignment");
        assert!(matches!(
            error,
            VectorCoreError::Execution { message, .. }
                if message.contains("non-mergeable type Boolean")
        ));

        let drop_node = node(
            "frame-drop-if-present",
            serde_json::from_value(serde_json::json!({"column": "protection"}))
                .expect("parameters"),
        );
        let dropped =
            FrameRuntime::frame_drop_if_present(&drop_node, &base, &source()).expect("drop bool");
        assert!(
            FrameRuntime::informative_merge(&merge_node, &dropped, &informative, &source(),)
                .is_ok()
        );
    }
}
