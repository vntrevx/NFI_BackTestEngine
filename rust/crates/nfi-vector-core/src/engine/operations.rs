//! Type-safe scalar and column operations for the exact execution substrate.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::column::{OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::float::{binary, compare, BinaryFloatOp, FloatComparison};
use crate::program::ProgramNode;

use super::runtime::{NodeValue, RuntimeColumn};

pub(super) fn literal_value(node: &ProgramNode) -> Result<NodeValue<'static>, VectorCoreError> {
    if let Some(special) = node.parameters.get("special").and_then(Value::as_str) {
        return Ok(NodeValue::Float(match special {
            "nan" => f64::NAN,
            "+infinity" => f64::INFINITY,
            "-infinity" => f64::NEG_INFINITY,
            _ => return Err(node_error(node, "literal has an unknown special float")),
        }));
    }
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
        Value::String(value) => NodeValue::Text(value.clone()),
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
    let left = resolve_numeric(values, left)?;
    let right = resolve_numeric(values, right)?;
    if node.value_type.ends_with("-column") {
        let output = (0..rows)
            .map(|row| {
                let left = left.at(row);
                let right = right.at(row);
                match (left, right) {
                    (Some(left), Some(right)) => Some(binary(left, right, operation)),
                    _ => None,
                }
            })
            .collect();
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            output,
        ))))
    } else {
        match (left.at(0), right.at(0)) {
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
    let left = resolve_numeric(values, left)?;
    let right = resolve_numeric(values, right)?;
    if node.value_type == "bool-column" {
        let output = (0..rows)
            .map(|row| {
                let left = left.at(row);
                let right = right.at(row);
                match (left, right) {
                    (Some(left), Some(right)) => Some(compare(left, right, comparison)),
                    _ => None,
                }
            })
            .collect();
        Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean(output),
        )))
    } else {
        match (left.at(0), right.at(0)) {
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
    let inputs = node
        .inputs
        .iter()
        .map(|input| resolve_bool(values, input))
        .collect::<Result<Vec<_>, _>>()?;
    let apply = |row| -> Option<bool> {
        let mut result = inputs[0].at(row);
        for input in inputs.iter().skip(1) {
            let right = input.at(row);
            result = match (result, right) {
                (Some(left), Some(right)) => Some(if operator == "and" {
                    left && right
                } else {
                    left || right
                }),
                _ => None,
            };
        }
        result
    };
    if node.value_type == "bool-column" {
        Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean((0..rows).map(apply).collect()),
        )))
    } else {
        Ok(apply(0).map_or(NodeValue::Null, NodeValue::Bool))
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
        let input = resolve_bool(values, input)?;
        return Ok(NodeValue::Column(RuntimeColumn::Owned(
            OwnedColumn::boolean(
                (0..rows)
                    .map(|row| input.at(row).map(|item| !item))
                    .collect(),
            ),
        )));
    }
    if !matches!(operator, "negate" | "positive") {
        return Err(node_error(node, "numeric unary operator is unsupported"));
    }
    let input = resolve_numeric(values, input)?;
    let apply = |row| -> Option<f64> {
        input.at(row).map(|value| {
            if operator == "negate" {
                crate::float::canonicalize(-value)
            } else {
                value
            }
        })
    };
    if node.value_type.ends_with("-column") {
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            (0..rows).map(apply).collect(),
        ))))
    } else {
        Ok(apply(0).map_or(NodeValue::Null, NodeValue::Float))
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
    let condition = resolve_bool(values, &node.inputs[0])?;
    let mut when_true = None;
    let mut when_false = None;
    let mut output = Vec::with_capacity(rows);
    for row in 0..rows {
        output.push(match condition.at(row) {
            Some(true) => {
                let value = if let Some(value) = when_true {
                    value
                } else {
                    let value = resolve_numeric(values, &node.inputs[1])?;
                    when_true = Some(value);
                    value
                };
                value.at(row)
            }
            Some(false) => {
                let value = if let Some(value) = when_false {
                    value
                } else {
                    let value = resolve_numeric(values, &node.inputs[2])?;
                    when_false = Some(value);
                    value
                };
                value.at(row)
            }
            None => None,
        });
    }
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
    let value = resolve_numeric(values, node)?;
    Ok((0..rows).map(|row| value.at(row)).collect())
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
            ValueType::I64 => Ok(OwnedColumn::i64(
                (0..rows).map(|row| column.i64_at(row)).collect(),
            )),
            ValueType::Bool => Ok(OwnedColumn::boolean(
                (0..rows).map(|row| column.bool_at(row)).collect(),
            )),
            ValueType::Text => Ok(OwnedColumn::text(
                (0..rows)
                    .map(|row| column.text_at(row).map(str::to_owned))
                    .collect(),
            )),
            ValueType::TimestampMs => Ok(OwnedColumn::timestamp_ms(
                (0..rows).map(|row| column.timestamp_ms_at(row)).collect(),
            )),
        },
        NodeValue::Null => Ok(OwnedColumn::f64(vec![None; rows])),
        NodeValue::Integer(value) => Ok(OwnedColumn::i64(vec![Some(*value); rows])),
        NodeValue::Float(value) => Ok(OwnedColumn::f64(vec![Some(*value); rows])),
        NodeValue::Bool(value) => Ok(OwnedColumn::boolean(vec![Some(*value); rows])),
        NodeValue::Text(value) => Ok(OwnedColumn::text(vec![Some(value.clone()); rows])),
        _ => Err(VectorCoreError::InvalidOutput(
            "requested output is not a supported scalar or column".to_owned(),
        )),
    }
}

pub(super) fn numeric_at(
    values: &BTreeMap<String, NodeValue<'_>>,
    node: &str,
    row: usize,
) -> Result<Option<f64>, VectorCoreError> {
    Ok(resolve_numeric(values, node)?.at(row))
}

#[derive(Clone, Copy)]
enum NumericValue<'values, 'batch> {
    Null,
    Integer(i64),
    Float(f64),
    I64(&'values RuntimeColumn<'batch>),
    F64(&'values RuntimeColumn<'batch>),
}

impl NumericValue<'_, '_> {
    fn at(self, row: usize) -> Option<f64> {
        match self {
            Self::Null => None,
            Self::Integer(value) => Some(integer_as_f64(value)),
            Self::Float(value) => Some(value),
            Self::I64(column) => column.i64_at(row).map(integer_as_f64),
            Self::F64(column) => column.f64_at(row),
        }
    }
}

fn resolve_numeric<'values, 'batch>(
    values: &'values BTreeMap<String, NodeValue<'batch>>,
    node: &str,
) -> Result<NumericValue<'values, 'batch>, VectorCoreError> {
    match resolve_value(values, node)? {
        NodeValue::Null => Ok(NumericValue::Null),
        NodeValue::Integer(value) => Ok(NumericValue::Integer(*value)),
        NodeValue::Float(value) => Ok(NumericValue::Float(*value)),
        NodeValue::Column(column) if column.value_type() == ValueType::I64 => {
            Ok(NumericValue::I64(column))
        }
        NodeValue::Column(column) if column.value_type() == ValueType::F64 => {
            Ok(NumericValue::F64(column))
        }
        _ => Err(VectorCoreError::Execution {
            node: node.to_owned(),
            message: "value is not numeric".to_owned(),
        }),
    }
}

#[derive(Clone, Copy)]
enum BoolValue<'values, 'batch> {
    Null,
    Scalar(bool),
    Column(&'values RuntimeColumn<'batch>),
}

impl BoolValue<'_, '_> {
    fn at(self, row: usize) -> Option<bool> {
        match self {
            Self::Null => None,
            Self::Scalar(value) => Some(value),
            Self::Column(column) => column.bool_at(row),
        }
    }
}

fn resolve_bool<'values, 'batch>(
    values: &'values BTreeMap<String, NodeValue<'batch>>,
    node: &str,
) -> Result<BoolValue<'values, 'batch>, VectorCoreError> {
    match resolve_value(values, node)? {
        NodeValue::Null => Ok(BoolValue::Null),
        NodeValue::Bool(value) => Ok(BoolValue::Scalar(*value)),
        NodeValue::Column(column) if column.value_type() == ValueType::Bool => {
            Ok(BoolValue::Column(column))
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
        "int-column" => Some(ValueType::I64),
        "bool-column" => Some(ValueType::Bool),
        "string-column" => Some(ValueType::Text),
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
