use std::collections::BTreeMap;

use serde_json::Value as JsonValue;

use crate::column::{OwnedColumn, ValueType};
use crate::program::ProgramNode;
use crate::VectorCoreError;

#[derive(Clone, Debug)]
pub(super) enum RuntimeValue {
    DataFrame,
    Metadata,
    Unbound,
    Null,
    Bool(bool),
    Integer(i64),
    Float(f64),
    Text(String),
    Column(OwnedColumn),
    Alias(String),
}

pub(super) fn value<'a>(
    values: &'a BTreeMap<String, RuntimeValue>,
    start: &str,
) -> Result<&'a RuntimeValue, VectorCoreError> {
    let mut current = start;
    for _ in 0..=values.len() {
        let current_value = values
            .get(current)
            .ok_or_else(|| VectorCoreError::Execution {
                node: start.to_owned(),
                message: format!("mutation input node is absent: {current}"),
            })?;
        if let RuntimeValue::Alias(next) = current_value {
            current = next;
        } else {
            return Ok(current_value);
        }
    }
    Err(VectorCoreError::Execution {
        node: start.to_owned(),
        message: "mutation runtime alias cycle".to_owned(),
    })
}

pub(super) fn numeric_at(
    values: &BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<f64>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(f64::from(u8::from(*value)))),
        RuntimeValue::Integer(value) => Ok(Some(i64_as_f64(*value))),
        RuntimeValue::Float(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) => match column.as_view().value_type() {
            ValueType::Bool => Ok(column
                .as_view()
                .bool_at(row)
                .map(|value| f64::from(u8::from(value)))),
            ValueType::F64 => Ok(column.as_view().f64_at(row)),
            ValueType::I64 => Ok(column.as_view().i64_at(row).map(i64_as_f64)),
            _ => Err(type_error(node, "numeric")),
        },
        _ => Err(type_error(node, "numeric")),
    }
}

pub(super) fn bool_at(
    values: &BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<bool>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::Bool => {
            Ok(column.as_view().bool_at(row))
        }
        _ => Err(type_error(node, "Boolean")),
    }
}

pub(super) fn cast_i64_at(
    values: &BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<i64>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(i64::from(*value))),
        RuntimeValue::Integer(value) => Ok(Some(*value)),
        RuntimeValue::Float(value) => Ok(Some(f64_as_i64(*value)?)),
        RuntimeValue::Column(column) => match column.as_view().value_type() {
            ValueType::Bool => Ok(column.as_view().bool_at(row).map(i64::from)),
            ValueType::I64 => Ok(column.as_view().i64_at(row)),
            ValueType::F64 => column.as_view().f64_at(row).map(f64_as_i64).transpose(),
            _ => Err(type_error(node, "castable to integer")),
        },
        _ => Err(type_error(node, "castable to integer")),
    }
}

pub(super) fn cast_f64_at(
    values: &BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<f64>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(f64::from(u8::from(*value)))),
        RuntimeValue::Integer(value) => Ok(Some(i64_as_f64(*value))),
        RuntimeValue::Float(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) => match column.as_view().value_type() {
            ValueType::Bool => Ok(column
                .as_view()
                .bool_at(row)
                .map(|value| f64::from(u8::from(value)))),
            ValueType::I64 => Ok(column.as_view().i64_at(row).map(i64_as_f64)),
            ValueType::F64 => Ok(column.as_view().f64_at(row)),
            _ => Err(type_error(node, "castable to float")),
        },
        _ => Err(type_error(node, "castable to float")),
    }
}

pub(super) fn cast_bool_at(
    values: &BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<bool>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(*value)),
        RuntimeValue::Integer(value) => Ok(Some(*value != 0)),
        RuntimeValue::Float(value) => Ok(Some(*value != 0.0)),
        RuntimeValue::Column(column) => match column.as_view().value_type() {
            ValueType::Bool => Ok(column.as_view().bool_at(row)),
            ValueType::I64 => Ok(column.as_view().i64_at(row).map(|value| value != 0)),
            ValueType::F64 => Ok(column.as_view().f64_at(row).map(|value| value != 0.0)),
            _ => Err(type_error(node, "castable to Boolean")),
        },
        _ => Err(type_error(node, "castable to Boolean")),
    }
}

pub(super) fn text_at<'a>(
    values: &'a BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<&'a str, VectorCoreError> {
    text_at_optional(values, node, row)?.ok_or_else(|| type_error(node, "non-null text"))
}

pub(super) fn text_at_optional<'a>(
    values: &'a BTreeMap<String, RuntimeValue>,
    node: &str,
    row: usize,
) -> Result<Option<&'a str>, VectorCoreError> {
    match value(values, node)? {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Text(value) => Ok(Some(value)),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::Text => {
            Ok(column.as_view().text_at(row))
        }
        _ => Err(type_error(node, "text")),
    }
}

pub(super) fn scalar_text(value: &RuntimeValue) -> Result<String, VectorCoreError> {
    match value {
        RuntimeValue::Bool(value) => Ok(if *value { "True" } else { "False" }.to_owned()),
        RuntimeValue::Integer(value) => Ok(value.to_string()),
        RuntimeValue::Float(_) => Err(type_error("format-string", "an exact scalar formatter")),
        RuntimeValue::Text(value) => Ok(value.clone()),
        _ => Err(type_error("format-string", "scalar text")),
    }
}

pub(super) fn as_column(
    value: &RuntimeValue,
    rows: usize,
    preferred: Option<ValueType>,
) -> Result<OwnedColumn, VectorCoreError> {
    match value {
        RuntimeValue::Column(column) => Ok(column.clone()),
        RuntimeValue::Null => {
            null_column(preferred.ok_or_else(|| type_error("null", "typed"))?, rows)
        }
        RuntimeValue::Integer(value) => match preferred.unwrap_or(ValueType::I64) {
            ValueType::I64 => Ok(OwnedColumn::i64(vec![Some(*value); rows])),
            ValueType::F64 => Ok(OwnedColumn::f64(vec![Some(i64_as_f64(*value)); rows])),
            _ => Err(type_error("integer", "numeric column")),
        },
        RuntimeValue::Float(value) => Ok(OwnedColumn::f64(vec![Some(*value); rows])),
        RuntimeValue::Bool(value) => Ok(OwnedColumn::boolean(vec![Some(*value); rows])),
        RuntimeValue::Text(value) => Ok(OwnedColumn::text(vec![Some(value.clone()); rows])),
        _ => Err(type_error("mutation value", "column-compatible")),
    }
}

pub(super) fn null_column(
    value_type: ValueType,
    rows: usize,
) -> Result<OwnedColumn, VectorCoreError> {
    Ok(match value_type {
        ValueType::F64 => OwnedColumn::f64(vec![None; rows]),
        ValueType::I64 => OwnedColumn::i64(vec![None; rows]),
        ValueType::Bool => OwnedColumn::boolean(vec![None; rows]),
        ValueType::Text => OwnedColumn::text(vec![None; rows]),
        ValueType::TimestampMs => {
            return Err(type_error("null", "non-timestamp mutation column"));
        }
    })
}

pub(super) fn mask_values(value: &RuntimeValue, rows: usize) -> Result<Vec<bool>, VectorCoreError> {
    match value {
        RuntimeValue::Bool(value) => Ok(vec![*value; rows]),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::Bool => Ok((0
            ..rows)
            .map(|row| column.as_view().bool_at(row).unwrap_or(false))
            .collect()),
        _ => Err(type_error("frame-write mask", "Boolean column")),
    }
}

pub(super) fn assign_masked(
    existing: &OwnedColumn,
    source: &RuntimeValue,
    mask: &[bool],
    rows: usize,
) -> Result<OwnedColumn, VectorCoreError> {
    let view = existing.as_view();
    Ok(match view.value_type() {
        ValueType::F64 => OwnedColumn::f64(
            (0..rows)
                .map(|row| {
                    if mask[row] {
                        scalar_or_column_f64(source, row)
                    } else {
                        Ok(view.f64_at(row))
                    }
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        ValueType::I64 => OwnedColumn::i64(
            (0..rows)
                .map(|row| {
                    if mask[row] {
                        scalar_or_column_i64(source, row)
                    } else {
                        Ok(view.i64_at(row))
                    }
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        ValueType::Bool => OwnedColumn::boolean(
            (0..rows)
                .map(|row| {
                    if mask[row] {
                        scalar_or_column_bool(source, row)
                    } else {
                        Ok(view.bool_at(row))
                    }
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        ValueType::Text => OwnedColumn::text(
            (0..rows)
                .map(|row| {
                    if mask[row] {
                        scalar_or_column_text(source, row)
                    } else {
                        Ok(view.text_at(row).map(str::to_owned))
                    }
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        ValueType::TimestampMs => return Err(type_error("frame-write", "mutable value column")),
    })
}

pub(super) fn append_text(
    existing: &OwnedColumn,
    source: &RuntimeValue,
    mask: Option<&[bool]>,
    rows: usize,
) -> Result<OwnedColumn, VectorCoreError> {
    let view = existing.as_view();
    if view.value_type() != ValueType::Text {
        return Err(type_error("tag append", "text column"));
    }
    Ok(OwnedColumn::text(
        (0..rows)
            .map(|row| {
                if mask.is_some_and(|values| !values[row]) {
                    return Ok(view.text_at(row).map(str::to_owned));
                }
                match (view.text_at(row), scalar_or_column_text(source, row)?) {
                    (Some(left), Some(right)) => Ok(Some(format!("{left}{right}"))),
                    _ => Ok(None),
                }
            })
            .collect::<Result<Vec<_>, VectorCoreError>>()?,
    ))
}

pub(super) fn scalar_or_column_f64(
    value: &RuntimeValue,
    row: usize,
) -> Result<Option<f64>, VectorCoreError> {
    match value {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Integer(value) => Ok(Some(i64_as_f64(*value))),
        RuntimeValue::Float(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::F64 => {
            Ok(column.as_view().f64_at(row))
        }
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::I64 => {
            Ok(column.as_view().i64_at(row).map(i64_as_f64))
        }
        _ => Err(type_error("assignment", "numeric")),
    }
}

pub(super) fn scalar_or_column_i64(
    value: &RuntimeValue,
    row: usize,
) -> Result<Option<i64>, VectorCoreError> {
    match value {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Integer(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::I64 => {
            Ok(column.as_view().i64_at(row))
        }
        _ => Err(type_error("assignment", "integer")),
    }
}

pub(super) fn scalar_or_column_bool(
    value: &RuntimeValue,
    row: usize,
) -> Result<Option<bool>, VectorCoreError> {
    match value {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Bool(value) => Ok(Some(*value)),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::Bool => {
            Ok(column.as_view().bool_at(row))
        }
        _ => Err(type_error("assignment", "Boolean")),
    }
}

pub(super) fn scalar_or_column_text(
    value: &RuntimeValue,
    row: usize,
) -> Result<Option<String>, VectorCoreError> {
    match value {
        RuntimeValue::Null => Ok(None),
        RuntimeValue::Text(value) => Ok(Some(value.clone())),
        RuntimeValue::Column(column) if column.as_view().value_type() == ValueType::Text => {
            Ok(column.as_view().text_at(row).map(str::to_owned))
        }
        _ => Err(type_error("assignment", "text")),
    }
}

pub(super) fn shift_column(column: &OwnedColumn, periods: usize) -> OwnedColumn {
    let rows = column.len();
    let view = column.as_view();
    match view.value_type() {
        ValueType::F64 => OwnedColumn::f64(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .map_or(Some(crate::float::canonicalize(f64::NAN)), |index| {
                            view.f64_at(index)
                        })
                })
                .collect(),
        ),
        ValueType::I64 => OwnedColumn::i64(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.i64_at(index))
                })
                .collect(),
        ),
        ValueType::Bool => OwnedColumn::boolean(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.bool_at(index))
                })
                .collect(),
        ),
        ValueType::Text => OwnedColumn::text(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.text_at(index).map(str::to_owned))
                })
                .collect(),
        ),
        ValueType::TimestampMs => OwnedColumn::timestamp_ms(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.timestamp_ms_at(index))
                })
                .collect(),
        ),
    }
}

pub(super) fn single_input(node: &ProgramNode) -> Result<&str, VectorCoreError> {
    match node.inputs.as_slice() {
        [input] => Ok(input),
        _ => Err(VectorCoreError::Execution {
            node: node.id.clone(),
            message: "mutation node requires one input".to_owned(),
        }),
    }
}

pub(super) fn two_inputs(node: &ProgramNode) -> Result<[&str; 2], VectorCoreError> {
    match node.inputs.as_slice() {
        [left, right] => Ok([left, right]),
        _ => Err(VectorCoreError::Execution {
            node: node.id.clone(),
            message: "mutation node requires two inputs".to_owned(),
        }),
    }
}

pub(super) fn three_inputs(node: &ProgramNode) -> Result<[&str; 3], VectorCoreError> {
    match node.inputs.as_slice() {
        [first, second, third] => Ok([first, second, third]),
        _ => Err(VectorCoreError::Execution {
            node: node.id.clone(),
            message: "mutation node requires three inputs".to_owned(),
        }),
    }
}

pub(super) fn string_parameter<'a>(
    node: &'a ProgramNode,
    name: &str,
) -> Result<&'a str, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(JsonValue::as_str)
        .ok_or_else(|| VectorCoreError::Execution {
            node: node.id.clone(),
            message: format!("mutation node lacks string parameter {name}"),
        })
}

pub(super) fn type_error(node: &str, expected: &str) -> VectorCoreError {
    VectorCoreError::Execution {
        node: node.to_owned(),
        message: format!("mutation value is not {expected}"),
    }
}

#[allow(clippy::cast_precision_loss)]
pub(super) fn i64_as_f64(value: i64) -> f64 {
    // Pandas promotes integer operands to Float64 for mixed numeric vector
    // expressions. The cast itself is part of the observed Python contract.
    value as f64
}

#[allow(clippy::cast_possible_truncation)]
pub(super) fn f64_as_i64(value: f64) -> Result<i64, VectorCoreError> {
    // numpy/pandas astype(int) truncates finite floats toward zero. Source
    // validation cannot prove dataframe values, so unsafe domains fail closed.
    const I64_MIN_F64: f64 = -9_223_372_036_854_775_808.0;
    const I64_EXCLUSIVE_MAX_F64: f64 = 9_223_372_036_854_775_808.0;
    if !value.is_finite() || !(I64_MIN_F64..I64_EXCLUSIVE_MAX_F64).contains(&value) {
        return Err(type_error("float cast", "a finite in-range integer"));
    }
    Ok(value as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_boolean_values_are_numeric_zero_and_one() {
        let values = BTreeMap::from([
            ("scalar".to_owned(), RuntimeValue::Bool(true)),
            (
                "column".to_owned(),
                RuntimeValue::Column(OwnedColumn::boolean(vec![Some(false), None, Some(true)])),
            ),
        ]);

        assert_eq!(numeric_at(&values, "scalar", 0).unwrap(), Some(1.0));
        assert_eq!(numeric_at(&values, "column", 0).unwrap(), Some(0.0));
        assert_eq!(numeric_at(&values, "column", 1).unwrap(), None);
        assert_eq!(numeric_at(&values, "column", 2).unwrap(), Some(1.0));
    }
}
