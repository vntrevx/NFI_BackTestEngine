//! Type-safe scalar and column operations for the exact execution substrate.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::column::{OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::float::{binary, compare, BinaryFloatOp, FloatComparison};
use crate::program::ProgramNode;

use super::runtime::{NodeValue, RuntimeColumn};

pub(super) fn literal_value(node: &ProgramNode) -> Result<NodeValue<'static>, VectorCoreError> {
    let value = node
        .parameters
        .get("value")
        .ok_or_else(|| node_error(node, "literal has no value"))?;
    Ok(match value {
        Value::Null => NodeValue::Null,
        Value::Bool(value) => NodeValue::Bool(*value),
        Value::Number(value) if value.is_i64() => NodeValue::Integer(
            value
                .as_i64()
                .ok_or_else(|| node_error(node, "integer literal is outside i64"))?,
        ),
        Value::Number(value) => NodeValue::Float(
            value
                .as_f64()
                .ok_or_else(|| node_error(node, "number literal is outside f64"))?,
        ),
        Value::String(_) => NodeValue::Text,
        _ => NodeValue::Json,
    })
}

pub(super) fn execute_binary<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let [left, right] = two_inputs(node)?;
    let operation = match string_parameter(node, "operator")? {
        "add" => BinaryFloatOp::Add,
        "subtract" => BinaryFloatOp::Subtract,
        "multiply" => BinaryFloatOp::Multiply,
        "divide" => BinaryFloatOp::Divide,
        "modulo" => BinaryFloatOp::Remainder,
        other => {
            return Err(node_error(
                node,
                format!("binary operator is not in the exact substrate: {other}"),
            ));
        }
    };
    if node.value_type.ends_with("-column") {
        let output = (0..rows)
            .map(|row| {
                let left = numeric_at(values, left, row)?;
                let right = numeric_at(values, right, row)?;
                Ok(match (left, right) {
                    (Some(left), Some(right)) => Some(binary(left, right, operation)),
                    _ => None,
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            output,
        ))))
    } else {
        match (numeric_at(values, left, 0)?, numeric_at(values, right, 0)?) {
            (Some(left), Some(right)) => Ok(NodeValue::Float(binary(left, right, operation))),
            _ => Ok(NodeValue::Null),
        }
    }
}

pub(super) fn execute_compare<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let [left, right] = two_inputs(node)?;
    let comparison = match string_parameter(node, "operator")? {
        "equal" => FloatComparison::Equal,
        "not-equal" => FloatComparison::NotEqual,
        "less-than" => FloatComparison::Less,
        "less-than-or-equal" => FloatComparison::LessEqual,
        "greater-than" => FloatComparison::Greater,
        "greater-than-or-equal" => FloatComparison::GreaterEqual,
        other => return Err(node_error(node, format!("unsupported comparison: {other}"))),
    };
    if node.value_type == "bool-column" {
        let output = (0..rows)
            .map(|row| {
                let left = numeric_at(values, left, row)?;
                let right = numeric_at(values, right, row)?;
                Ok(match (left, right) {
                    (Some(left), Some(right)) => Some(compare(left, right, comparison)),
                    _ => None,
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean(output),
        )))
    } else {
        match (numeric_at(values, left, 0)?, numeric_at(values, right, 0)?) {
            (Some(left), Some(right)) => Ok(NodeValue::Bool(compare(left, right, comparison))),
            _ => Ok(NodeValue::Null),
        }
    }
}

pub(super) fn execute_logical<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let operator = string_parameter(node, "operator")?;
    if !matches!(operator, "and" | "or") || node.inputs.is_empty() {
        return Err(node_error(
            node,
            "logical node has an unsupported operator or arity",
        ));
    }
    let apply = |row| -> Result<Option<bool>, VectorCoreError> {
        let mut result = bool_at(values, &node.inputs[0], row)?;
        for input in node.inputs.iter().skip(1) {
            let right = bool_at(values, input, row)?;
            result = match (result, right) {
                (Some(left), Some(right)) => Some(if operator == "and" {
                    left && right
                } else {
                    left || right
                }),
                _ => None,
            };
        }
        Ok(result)
    };
    if node.value_type == "bool-column" {
        Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean((0..rows).map(apply).collect::<Result<Vec<_>, _>>()?),
        )))
    } else {
        Ok(apply(0)?.map_or(NodeValue::Null, NodeValue::Bool))
    }
}

pub(super) fn execute_unary<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let input = single_input(node)?;
    let operator = string_parameter(node, "operator")?;
    if node.value_type == "bool-column" {
        if !matches!(operator, "not" | "invert") {
            return Err(node_error(node, "boolean unary operator is unsupported"));
        }
        return Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean(
                (0..rows)
                    .map(|row| bool_at(values, input, row).map(|value| value.map(|item| !item)))
                    .collect::<Result<Vec<_>, _>>()?,
            ),
        )));
    }
    if !matches!(operator, "negate" | "positive") {
        return Err(node_error(node, "numeric unary operator is unsupported"));
    }
    let apply = |row| -> Result<Option<f64>, VectorCoreError> {
        Ok(numeric_at(values, input, row)?.map(|value| {
            if operator == "negate" {
                crate::float::canonicalize(-value)
            } else {
                value
            }
        }))
    };
    if node.value_type.ends_with("-column") {
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            (0..rows).map(apply).collect::<Result<Vec<_>, _>>()?,
        ))))
    } else {
        Ok(apply(0)?.map_or(NodeValue::Null, NodeValue::Float))
    }
}

pub(super) fn execute_select<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    if node.inputs.len() != 3 || node.value_type != "f64-column" {
        return Err(node_error(
            node,
            "select substrate requires three inputs and f64 output",
        ));
    }
    let output = (0..rows)
        .map(|row| match bool_at(values, &node.inputs[0], row)? {
            Some(true) => numeric_at(values, &node.inputs[1], row),
            Some(false) => numeric_at(values, &node.inputs[2], row),
            None => Ok(None),
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
        output,
    ))))
}

pub(super) fn resolve_value<'values, 'batch>(
    values: &'values BTreeMap<String, NodeValue<'batch>>,
    start: &str,
) -> Result<&'values NodeValue<'batch>, VectorCoreError> {
    let mut current = start;
    for _ in 0..=values.len() {
        let value = values
            .get(current)
            .ok_or_else(|| VectorCoreError::Execution {
                node: start.to_owned(),
                message: format!("input node {current} has no runtime value"),
            })?;
        if let NodeValue::Alias(next) = value {
            current = next;
        } else {
            return Ok(value);
        }
    }
    Err(VectorCoreError::Execution {
        node: start.to_owned(),
        message: "runtime alias cycle".to_owned(),
    })
}

pub(super) fn collect_numeric(
    values: &BTreeMap<String, NodeValue<'_>>,
    node: &str,
    rows: usize,
) -> Result<Vec<Option<f64>>, VectorCoreError> {
    (0..rows).map(|row| numeric_at(values, node, row)).collect()
}

pub(super) fn to_owned_column(
    value: &NodeValue<'_>,
    rows: usize,
) -> Result<OwnedColumn, VectorCoreError> {
    match value {
        NodeValue::Column(column) => match column.value_type() {
            ValueType::F64 => Ok(OwnedColumn::f64(
                (0..rows).map(|row| column.f64_at(row)).collect(),
            )),
            ValueType::Bool => Ok(OwnedColumn::boolean(
                (0..rows).map(|row| column.bool_at(row)).collect(),
            )),
            ValueType::TimestampMs => Ok(OwnedColumn::timestamp_ms(
                (0..rows).map(|row| column.timestamp_ms_at(row)).collect(),
            )),
        },
        NodeValue::Null => Ok(OwnedColumn::f64(vec![None; rows])),
        NodeValue::Integer(value) => Ok(OwnedColumn::f64(vec![Some(integer_as_f64(*value)); rows])),
        NodeValue::Float(value) => Ok(OwnedColumn::f64(vec![Some(*value); rows])),
        NodeValue::Bool(value) => Ok(OwnedColumn::boolean(vec![Some(*value); rows])),
        _ => Err(VectorCoreError::InvalidOutput(
            "requested output is not a supported scalar or column".to_owned(),
        )),
    }
}

fn numeric_at(
    values: &BTreeMap<String, NodeValue<'_>>,
    node: &str,
    row: usize,
) -> Result<Option<f64>, VectorCoreError> {
    match resolve_value(values, node)? {
        NodeValue::Null => Ok(None),
        NodeValue::Integer(value) => Ok(Some(integer_as_f64(*value))),
        NodeValue::Float(value) => Ok(Some(*value)),
        NodeValue::Column(column) if column.value_type() == ValueType::F64 => {
            Ok(column.f64_at(row))
        }
        _ => Err(VectorCoreError::Execution {
            node: node.to_owned(),
            message: "value is not numeric".to_owned(),
        }),
    }
}

fn bool_at(
    values: &BTreeMap<String, NodeValue<'_>>,
    node: &str,
    row: usize,
) -> Result<Option<bool>, VectorCoreError> {
    match resolve_value(values, node)? {
        NodeValue::Null => Ok(None),
        NodeValue::Bool(value) => Ok(Some(*value)),
        NodeValue::Column(column) if column.value_type() == ValueType::Bool => {
            Ok(column.bool_at(row))
        }
        _ => Err(VectorCoreError::Execution {
            node: node.to_owned(),
            message: "value is not boolean".to_owned(),
        }),
    }
}

pub(super) fn single_input(node: &ProgramNode) -> Result<&str, VectorCoreError> {
    if let [input] = node.inputs.as_slice() {
        Ok(input)
    } else {
        Err(node_error(node, "node requires exactly one input"))
    }
}

fn two_inputs(node: &ProgramNode) -> Result<[&str; 2], VectorCoreError> {
    if let [left, right] = node.inputs.as_slice() {
        Ok([left, right])
    } else {
        Err(node_error(node, "node requires exactly two inputs"))
    }
}

pub(super) fn string_parameter<'node>(
    node: &'node ProgramNode,
    name: &str,
) -> Result<&'node str, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| node_error(node, format!("missing string parameter {name}")))
}

pub(super) fn unsigned_parameter(node: &ProgramNode, name: &str) -> Result<usize, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| node_error(node, format!("missing bounded integer parameter {name}")))
}

pub(super) fn column_type(value_type: &str) -> Option<ValueType> {
    match value_type {
        "f64-column" => Some(ValueType::F64),
        "bool-column" => Some(ValueType::Bool),
        "timestamp-column" => Some(ValueType::TimestampMs),
        _ => None,
    }
}

#[allow(clippy::cast_precision_loss)]
fn integer_as_f64(value: i64) -> f64 {
    // Python/Pandas converts integer literals to double before broadcasting.
    // Preserving that observable conversion is more important than rejecting
    // source integers above the exactly representable f64 range.
    value as f64
}

pub(super) fn node_error(node: &ProgramNode, message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::Execution {
        node: node.id.clone(),
        message: message.into(),
    }
}
