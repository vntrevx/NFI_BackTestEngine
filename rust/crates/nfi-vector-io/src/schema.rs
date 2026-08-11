//! Arrow projection and fixed-width file-backed row layout.

use std::collections::{BTreeMap, BTreeSet};

use arrow2::datatypes::{DataType, Schema};
use nfi_sim_core::{
    FileBackedFeatureKind, FILE_BACKED_FEATURE_BYTES, FILE_BACKED_ROW_HEADER_BYTES,
};

use crate::loader::VectorPair;
use crate::values::is_numeric_type;
use crate::VectorInputError;

#[derive(Debug)]
pub(crate) struct FeatureLayout {
    pub(crate) name: String,
    pub(crate) kind: FileBackedFeatureKind,
    pub(crate) source_index: usize,
}

pub(crate) fn projected_source_indices(
    schema: &Schema,
    pair: &VectorPair,
) -> Result<Vec<usize>, VectorInputError> {
    let mut required = required_columns(pair);
    // Freqtrade omits tag columns when a strategy never populated them. Keep
    // them in the projection only when present; the decoder supplies `None`
    // for an omitted tag instead of treating the vector as malformed.
    for optional in ["nfi_exec_enter_tag", "nfi_exec_exit_tag"] {
        if schema.fields.iter().any(|field| field.name == optional) {
            required.insert(optional.to_owned());
        }
    }

    let mut indices = Vec::with_capacity(required.len());
    for column in required {
        let index = schema
            .fields
            .iter()
            .position(|field| field.name == column)
            .ok_or_else(|| VectorInputError::MissingColumn {
                pair: pair.pair.clone(),
                column,
            })?;
        indices.push(index);
    }
    indices.sort_unstable();
    indices.dedup();
    Ok(indices)
}

pub(crate) fn column_positions(schema: &Schema) -> BTreeMap<String, usize> {
    schema
        .fields
        .iter()
        .enumerate()
        .map(|(index, field)| (field.name.clone(), index))
        .collect()
}

pub(crate) fn feature_layout(
    schema: &Schema,
    projected_positions: &BTreeMap<String, usize>,
    pair: &VectorPair,
) -> Result<(Vec<FeatureLayout>, usize), VectorInputError> {
    let feature_layouts = pair
        .feature_columns
        .iter()
        .map(|name| {
            let source_index = projected_positions[name];
            let data_type = &schema.fields[source_index].data_type;
            let kind = match data_type {
                DataType::Boolean => FileBackedFeatureKind::Boolean,
                data_type if is_numeric_type(data_type) => FileBackedFeatureKind::Number,
                actual => {
                    return Err(VectorInputError::ColumnType {
                        pair: pair.pair.clone(),
                        column: name.clone(),
                        actual: Box::new(actual.clone()),
                        expected: "numeric or boolean",
                    });
                }
            };
            Ok(FeatureLayout {
                name: name.clone(),
                kind,
                source_index,
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let feature_bytes = feature_layouts
        .len()
        .checked_mul(FILE_BACKED_FEATURE_BYTES)
        .ok_or_else(|| file_backing_error(&pair.pair, "feature row is too wide"))?;
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES
        .checked_add(feature_bytes)
        .ok_or_else(|| file_backing_error(&pair.pair, "pair row is too wide"))?;
    Ok((feature_layouts, row_stride))
}

pub(crate) fn file_backing_error(pair: &str, message: &'static str) -> VectorInputError {
    VectorInputError::FileBacking {
        pair: pair.to_owned(),
        source: std::io::Error::new(std::io::ErrorKind::InvalidData, message),
    }
}

pub(crate) fn required_columns(pair: &VectorPair) -> BTreeSet<String> {
    let mut columns = BTreeSet::from([
        "date".to_owned(),
        "open".to_owned(),
        "high".to_owned(),
        "low".to_owned(),
        "close".to_owned(),
        "volume".to_owned(),
        "nfi_exec_enter_long".to_owned(),
    ]);
    if pair.use_exit_signal.enabled() {
        columns.insert("nfi_exec_exit_long".to_owned());
    }
    if pair.can_short.enabled() {
        columns.insert("nfi_exec_enter_short".to_owned());
        if pair.use_exit_signal.enabled() {
            columns.insert("nfi_exec_exit_short".to_owned());
        }
    }
    if pair.include_funding.enabled() {
        columns.insert("nfi_exec_funding_rate".to_owned());
        columns.insert("nfi_exec_funding_mark_price".to_owned());
    }
    columns.extend(pair.feature_columns.iter().cloned());
    columns
}
