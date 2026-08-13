//! Exact NumPy-shaped and native array operations used by indicator programs.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::column::{OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::float::{binary, canonicalize, BinaryFloatOp};
use crate::kernels::{AbsoluteDifferenceStream, HourlyInsideBarStream, UtcOpeningRangeStream};
use crate::program::{ProgramNode, SourceLocation};

use super::operations::resolve_value;
use super::runtime::{NodeValue, RuntimeColumn};

#[derive(Debug)]
struct OpeningRangeState {
    cutoff_hour: u8,
    stream: UtcOpeningRangeStream,
}

/// Explicit bounded state retained by streaming array-call kernels.
#[derive(Debug, Default)]
pub(super) struct ArrayCallState {
    absolute_differences: BTreeMap<String, AbsoluteDifferenceStream>,
    opening_ranges: BTreeMap<String, OpeningRangeState>,
    inside_bars: BTreeMap<String, HourlyInsideBarStream>,
}

impl ArrayCallState {
    /// Number of scalar aggregate values retained across batches.
    #[must_use]
    pub(super) fn retained(&self) -> usize {
        self.absolute_differences
            .values()
            .map(AbsoluteDifferenceStream::retained)
            .sum::<usize>()
            .saturating_add(
                self.opening_ranges
                    .values()
                    .map(|state| state.stream.retained())
                    .sum::<usize>(),
            )
            .saturating_add(
                self.inside_bars
                    .values()
                    .map(HourlyInsideBarStream::retained)
                    .sum::<usize>(),
            )
    }
}

/// Execute the bounded array-call surface emitted by the latest indicator compiler.
///
/// Unsupported families, names, signatures, arguments, and nullable inputs to a
/// stateful native kernel fail closed. Element-wise operations preserve Arrow null
/// independently from IEEE NaN.
pub(super) fn execute_array_call<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let family = parameter_string(node, "family", source)?;
    let name = parameter_string(node, "name", source)?;
    let arguments = arguments(node, source)?;
    match family {
        "numpy" if arguments.is_empty() => execute_numpy(node, name, values, rows, state, source),
        "native" => execute_native(node, name, arguments, values, rows, state, source),
        _ => Err(unsupported(node, source)),
    }
}

fn execute_numpy<'batch>(
    node: &ProgramNode,
    name: &str,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    match name {
        "maximum" => elementwise_extreme(node, values, rows, true, source),
        "minimum" => elementwise_extreme(node, values, rows, false, source),
        "abs" => elementwise_unary(node, values, rows, f64::abs, source),
        "sqrt" => square_root(node, values, rows, source),
        "absolute-difference" => absolute_difference(node, values, rows, state, source),
        "full_like" => full_like(node, values, rows, source),
        "divide" => divide_where(node, values, rows, source),
        "zeros_like" => zeros_like(node, values, rows, source),
        "fill-missing" => fill_missing(node, values, rows, source),
        "nan_to_num" => elementwise_unary(node, values, rows, numpy_nan_to_num, source),
        _ => Err(unsupported(node, source)),
    }
}

fn execute_native<'batch>(
    node: &ProgramNode,
    name: &str,
    arguments: &Map<String, Value>,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    match name {
        "opening-range-high" | "opening-range-low" => {
            opening_range(node, name, arguments, values, rows, state, source)
        }
        "inside-bar-ready" | "inside-bar-mother-high" | "inside-bar-mother-low"
            if arguments.is_empty() =>
        {
            inside_bar(node, name, values, rows, state, source)
        }
        _ => Err(unsupported(node, source)),
    }
}

fn elementwise_extreme<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    maximum: bool,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let [left, right] = two_inputs(node, source)?;
    require_f64_column(values, left, rows, node, source)?;
    require_f64_column(values, right, rows, node, source)?;
    let output = (0..rows)
        .map(|row| {
            let left = numeric_at(values, left, row, node, source)?;
            let right = numeric_at(values, right, row, node, source)?;
            Ok(match (left, right) {
                (Some(left), Some(right)) => {
                    Some(canonicalize(if left.is_nan() || right.is_nan() {
                        f64::NAN
                    } else if (maximum && left > right) || (!maximum && left < right) {
                        left
                    } else {
                        // NumPy chooses the right operand for equal values, including signed zero.
                        right
                    }))
                }
                _ => None,
            })
        })
        .collect::<Result<Vec<_>, VectorCoreError>>()?;
    Ok(column(output))
}

fn elementwise_unary<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    operation: fn(f64) -> f64,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let input = one_input(node, source)?;
    require_f64_column(values, input, rows, node, source)?;
    let output = (0..rows)
        .map(|row| {
            numeric_at(values, input, row, node, source)
                .map(|value| value.map(|value| canonicalize(operation(value))))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(column(output))
}

fn square_root<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    let input = one_input(node, source)?;
    match resolve_value(values, input).map_err(|error| contextual(node, source, &error))? {
        NodeValue::Null => {
            require_scalar_output(node, source)?;
            Ok(NodeValue::Null)
        }
        NodeValue::Integer(value) => {
            require_scalar_output(node, source)?;
            Ok(NodeValue::Float(canonicalize(i64_as_f64(*value).sqrt())))
        }
        NodeValue::Float(value) => {
            require_scalar_output(node, source)?;
            Ok(NodeValue::Float(canonicalize(value.sqrt())))
        }
        NodeValue::Column(input_column) if input_column.value_type() == ValueType::F64 => {
            require_output(node, "f64-column", source)?;
            require_length(input_column, rows, node, source)?;
            let output = (0..rows)
                .map(|row| {
                    input_column
                        .f64_at(row)
                        .map(|value| canonicalize(value.sqrt()))
                })
                .collect();
            Ok(column(output))
        }
        _ => Err(error(node, source, "numpy sqrt input is not numeric")),
    }
}

fn absolute_difference<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let input = one_input(node, source)?;
    let input = collect_present_f64(values, input, rows, node, source)?;
    let output = state
        .absolute_differences
        .entry(node.id.clone())
        .or_default()
        .execute(&input);
    Ok(column(output.into_iter().map(Some).collect()))
}

fn full_like<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let [template, fill] = two_inputs(node, source)?;
    require_f64_column(values, template, rows, node, source)?;
    let fill = numeric_scalar(values, fill, node, source)?
        .ok_or_else(|| error(node, source, "numpy full_like fill is Arrow null"))?;
    Ok(column(vec![Some(fill); rows]))
}

fn zeros_like<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let template = one_input(node, source)?;
    require_f64_column(values, template, rows, node, source)?;
    Ok(column(vec![Some(0.0); rows]))
}

fn fill_missing<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let [input, fill] = two_inputs(node, source)?;
    let input = require_f64_column(values, input, rows, node, source)?;
    let fill = numeric_scalar(values, fill, node, source)?
        .ok_or_else(|| error(node, source, "fill-missing value is Arrow null"))?;
    Ok(column(
        (0..rows)
            .map(|row| match input.f64_at(row) {
                Some(value) if !value.is_nan() => Some(value),
                _ => Some(fill),
            })
            .collect(),
    ))
}

fn divide_where<'batch>(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let [numerator, denominator, out, where_mask] = four_inputs(node, source)?;
    require_f64_column(values, numerator, rows, node, source)?;
    require_f64_column(values, denominator, rows, node, source)?;
    let out = require_f64_column(values, out, rows, node, source)?;
    let mask = require_bool_column(values, where_mask, rows, node, source)?;
    let output = (0..rows)
        .map(|row| match mask.bool_at(row) {
            Some(true) => match (
                numeric_at(values, numerator, row, node, source)?,
                numeric_at(values, denominator, row, node, source)?,
            ) {
                (Some(left), Some(right)) => Ok(Some(binary(left, right, BinaryFloatOp::Divide))),
                _ => Ok(None),
            },
            Some(false) | None => Ok(out.f64_at(row)),
        })
        .collect::<Result<Vec<_>, VectorCoreError>>()?;
    Ok(column(output))
}

fn opening_range<'batch>(
    node: &ProgramNode,
    name: &str,
    arguments: &Map<String, Value>,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    if arguments.len() != 1 {
        return Err(error(node, source, "opening range arguments are invalid"));
    }
    let cutoff_hour = arguments
        .get("cutoff_hour")
        .and_then(Value::as_u64)
        .and_then(|value| u8::try_from(value).ok())
        .ok_or_else(|| error(node, source, "opening range cutoff_hour is invalid"))?;
    let [timestamps, high, low] = three_inputs(node, source)?;
    let timestamps = collect_present_timestamps(values, timestamps, rows, node, source)?;
    let high = collect_present_f64(values, high, rows, node, source)?;
    let low = collect_present_f64(values, low, rows, node, source)?;
    let entry = match state.opening_ranges.entry(node.id.clone()) {
        std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
        std::collections::btree_map::Entry::Vacant(entry) => entry.insert(OpeningRangeState {
            cutoff_hour,
            stream: UtcOpeningRangeStream::new(cutoff_hour)
                .map_err(|failure| contextual(node, source, &failure))?,
        }),
    };
    if entry.cutoff_hour != cutoff_hour {
        return Err(error(
            node,
            source,
            "opening range cutoff changed during execution",
        ));
    }
    let output = entry
        .stream
        .execute(&timestamps, &high, &low)
        .map_err(|failure| contextual(node, source, &failure))?;
    let selected = if name == "opening-range-high" {
        output.high
    } else {
        output.low
    };
    Ok(column(selected.into_iter().map(Some).collect()))
}

fn inside_bar<'batch>(
    node: &ProgramNode,
    name: &str,
    values: &BTreeMap<String, NodeValue<'batch>>,
    rows: usize,
    state: &mut ArrayCallState,
    source: Option<&SourceLocation>,
) -> Result<NodeValue<'batch>, VectorCoreError> {
    require_output(node, "f64-column", source)?;
    let [timestamps, high, low] = three_inputs(node, source)?;
    let timestamps = collect_present_timestamps(values, timestamps, rows, node, source)?;
    let high = collect_present_f64(values, high, rows, node, source)?;
    let low = collect_present_f64(values, low, rows, node, source)?;
    let output = state
        .inside_bars
        .entry(node.id.clone())
        .or_default()
        .execute(&timestamps, &high, &low)
        .map_err(|failure| contextual(node, source, &failure))?;
    let selected = match name {
        "inside-bar-ready" => output.ready,
        "inside-bar-mother-high" => output.mother_high,
        "inside-bar-mother-low" => output.mother_low,
        _ => return Err(unsupported(node, source)),
    };
    Ok(column(selected.into_iter().map(Some).collect()))
}

fn require_f64_column<'values, 'batch>(
    values: &'values BTreeMap<String, NodeValue<'batch>>,
    input: &str,
    rows: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<&'values RuntimeColumn<'batch>, VectorCoreError> {
    match resolve_value(values, input).map_err(|failure| contextual(node, source, &failure))? {
        NodeValue::Column(column) if column.value_type() == ValueType::F64 => {
            require_length(column, rows, node, source)?;
            Ok(column)
        }
        _ => Err(error(node, source, "array input is not a Float64 column")),
    }
}

fn require_bool_column<'values, 'batch>(
    values: &'values BTreeMap<String, NodeValue<'batch>>,
    input: &str,
    rows: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<&'values RuntimeColumn<'batch>, VectorCoreError> {
    match resolve_value(values, input).map_err(|failure| contextual(node, source, &failure))? {
        NodeValue::Column(column) if column.value_type() == ValueType::Bool => {
            require_length(column, rows, node, source)?;
            Ok(column)
        }
        _ => Err(error(node, source, "array mask is not a Boolean column")),
    }
}

fn require_length(
    column: &RuntimeColumn<'_>,
    rows: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<(), VectorCoreError> {
    let actual = match column {
        RuntimeColumn::Borrowed(column) => column.len(),
        RuntimeColumn::Owned(column) => column.len(),
    };
    if actual == rows {
        Ok(())
    } else {
        Err(error(
            node,
            source,
            format!("array input has {actual} rows; expected {rows}"),
        ))
    }
}

fn collect_present_f64(
    values: &BTreeMap<String, NodeValue<'_>>,
    input: &str,
    rows: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<Vec<f64>, VectorCoreError> {
    require_f64_column(values, input, rows, node, source)?;
    (0..rows)
        .map(|row| {
            numeric_at(values, input, row, node, source)?.ok_or_else(|| {
                error(
                    node,
                    source,
                    "stateful native array input contains an Arrow null",
                )
            })
        })
        .collect()
}

fn collect_present_timestamps(
    values: &BTreeMap<String, NodeValue<'_>>,
    input: &str,
    rows: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<Vec<i64>, VectorCoreError> {
    let column = match resolve_value(values, input).map_err(|e| contextual(node, source, &e))? {
        NodeValue::Column(column) if column.value_type() == ValueType::TimestampMs => column,
        _ => {
            return Err(error(
                node,
                source,
                "native array input is not a timestamp column",
            ))
        }
    };
    require_length(column, rows, node, source)?;
    (0..rows)
        .map(|row| {
            column.timestamp_ms_at(row).ok_or_else(|| {
                error(
                    node,
                    source,
                    "stateful native timestamp input contains an Arrow null",
                )
            })
        })
        .collect()
}

fn numeric_at(
    values: &BTreeMap<String, NodeValue<'_>>,
    input: &str,
    row: usize,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<Option<f64>, VectorCoreError> {
    match resolve_value(values, input).map_err(|failure| contextual(node, source, &failure))? {
        NodeValue::Null => Ok(None),
        NodeValue::Integer(value) => Ok(Some(i64_as_f64(*value))),
        NodeValue::Float(value) => Ok(Some(*value)),
        NodeValue::Column(column) if column.value_type() == ValueType::I64 => {
            Ok(column.i64_at(row).map(i64_as_f64))
        }
        NodeValue::Column(column) if column.value_type() == ValueType::F64 => {
            Ok(column.f64_at(row))
        }
        _ => Err(error(node, source, "array value is not numeric")),
    }
}

fn numeric_scalar(
    values: &BTreeMap<String, NodeValue<'_>>,
    input: &str,
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<Option<f64>, VectorCoreError> {
    match resolve_value(values, input).map_err(|failure| contextual(node, source, &failure))? {
        NodeValue::Null => Ok(None),
        NodeValue::Integer(value) => Ok(Some(i64_as_f64(*value))),
        NodeValue::Float(value) => Ok(Some(*value)),
        _ => Err(error(node, source, "array fill is not a numeric scalar")),
    }
}

fn require_output(
    node: &ProgramNode,
    expected: &str,
    source: Option<&SourceLocation>,
) -> Result<(), VectorCoreError> {
    if node.value_type == expected {
        Ok(())
    } else {
        Err(error(
            node,
            source,
            format!(
                "array output type is {}; expected {expected}",
                node.value_type
            ),
        ))
    }
}

fn require_scalar_output(
    node: &ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<(), VectorCoreError> {
    if matches!(node.value_type.as_str(), "dynamic" | "f64-scalar") {
        Ok(())
    } else {
        Err(error(
            node,
            source,
            format!(
                "numpy scalar output type is {}; expected dynamic or f64-scalar",
                node.value_type
            ),
        ))
    }
}

fn arguments<'node>(
    node: &'node ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<&'node Map<String, Value>, VectorCoreError> {
    if node.parameters.len() != 3 {
        return Err(error(node, source, "array-call parameters are not exact"));
    }
    node.parameters
        .get("arguments")
        .and_then(Value::as_object)
        .ok_or_else(|| error(node, source, "array-call arguments are not an object"))
}

fn parameter_string<'node>(
    node: &'node ProgramNode,
    name: &str,
    source: Option<&SourceLocation>,
) -> Result<&'node str, VectorCoreError> {
    node.parameters
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| error(node, source, format!("array-call {name} is not a string")))
}

fn one_input<'node>(
    node: &'node ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<&'node str, VectorCoreError> {
    match node.inputs.as_slice() {
        [input] => Ok(input),
        _ => Err(error(node, source, "array-call requires one input")),
    }
}

fn two_inputs<'node>(
    node: &'node ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<[&'node str; 2], VectorCoreError> {
    match node.inputs.as_slice() {
        [left, right] => Ok([left, right]),
        _ => Err(error(node, source, "array-call requires two inputs")),
    }
}

fn three_inputs<'node>(
    node: &'node ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<[&'node str; 3], VectorCoreError> {
    match node.inputs.as_slice() {
        [first, second, third] => Ok([first, second, third]),
        _ => Err(error(node, source, "array-call requires three inputs")),
    }
}

fn four_inputs<'node>(
    node: &'node ProgramNode,
    source: Option<&SourceLocation>,
) -> Result<[&'node str; 4], VectorCoreError> {
    match node.inputs.as_slice() {
        [first, second, third, fourth] => Ok([first, second, third, fourth]),
        _ => Err(error(node, source, "array-call requires four inputs")),
    }
}

fn column<'batch>(values: Vec<Option<f64>>) -> NodeValue<'batch> {
    NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(values)))
}

fn unsupported(node: &ProgramNode, source: Option<&SourceLocation>) -> VectorCoreError {
    VectorCoreError::UnsupportedOpcode {
        opcode: node.op.clone(),
        location: location(source),
    }
}

fn error(
    node: &ProgramNode,
    source: Option<&SourceLocation>,
    message: impl Into<String>,
) -> VectorCoreError {
    VectorCoreError::Execution {
        node: node.id.clone(),
        message: format!("{} at {}", message.into(), location(source)),
    }
}

fn contextual(
    node: &ProgramNode,
    source: Option<&SourceLocation>,
    failure: &VectorCoreError,
) -> VectorCoreError {
    error(node, source, failure.to_string())
}

fn location(source: Option<&SourceLocation>) -> String {
    source.map_or_else(
        || "strategy.py:?:?".to_owned(),
        |source| format!("{}:{}:{}", source.path, source.line, source.column),
    )
}

fn numpy_nan_to_num(value: f64) -> f64 {
    if value.is_nan() {
        0.0
    } else if value == f64::INFINITY {
        f64::MAX
    } else if value == f64::NEG_INFINITY {
        f64::MIN
    } else {
        value
    }
}

#[allow(clippy::cast_precision_loss)]
fn i64_as_f64(value: i64) -> f64 {
    value as f64
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::program::Lookback;

    fn node(name: &str, value_type: &str, inputs: &[&str], arguments: &Value) -> ProgramNode {
        ProgramNode {
            id: format!("array-{name}"),
            function: "f1".to_owned(),
            source_order: 1,
            op: "array-call".to_owned(),
            value_type: value_type.to_owned(),
            inputs: inputs.iter().map(ToString::to_string).collect(),
            parameters: json!({"family":"numpy","name":name,"arguments":arguments})
                .as_object()
                .expect("parameters")
                .clone(),
            lookback: Lookback {
                kind: "finite".to_owned(),
                candles: Some(0),
                expression: None,
                causal: true,
            },
        }
    }

    fn native_node(name: &str, inputs: &[&str], arguments: &Value) -> ProgramNode {
        let mut node = node(name, "f64-column", inputs, arguments);
        node.parameters.insert("family".to_owned(), json!("native"));
        node
    }

    fn source() -> SourceLocation {
        SourceLocation {
            path: "strategy.py".to_owned(),
            line: 42,
            column: 8,
            end_line: 42,
            end_column: 20,
        }
    }

    fn owned_f64(values: Vec<Option<f64>>) -> NodeValue<'static> {
        NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(values)))
    }

    fn output(value: &NodeValue<'_>) -> Vec<Option<f64>> {
        let NodeValue::Column(column) = value else {
            panic!("expected column")
        };
        (0..match column {
            RuntimeColumn::Borrowed(column) => column.len(),
            RuntimeColumn::Owned(column) => column.len(),
        })
            .map(|row| column.f64_at(row))
            .collect()
    }

    #[test]
    fn numpy_extremes_preserve_null_nan_and_right_signed_zero() {
        let values = BTreeMap::from([
            (
                "left".to_owned(),
                owned_f64(vec![Some(0.0), Some(-0.0), Some(f64::NAN), None]),
            ),
            (
                "right".to_owned(),
                owned_f64(vec![Some(-0.0), Some(0.0), Some(1.0), Some(2.0)]),
            ),
        ]);
        for name in ["maximum", "minimum"] {
            let actual = execute_array_call(
                &node(name, "f64-column", &["left", "right"], &json!({})),
                &values,
                4,
                &mut ArrayCallState::default(),
                Some(&source()),
            )
            .expect("valid extreme");
            let actual = output(&actual);
            assert_eq!(actual[0].expect("zero").to_bits(), (-0.0_f64).to_bits());
            assert_eq!(actual[1].expect("zero").to_bits(), 0.0_f64.to_bits());
            assert!(actual[2].expect("NaN is not null").is_nan());
            assert_eq!(actual[3], None);
        }
    }

    #[test]
    fn numpy_nan_to_num_and_divide_keep_null_separate() {
        let values = BTreeMap::from([
            (
                "input".to_owned(),
                owned_f64(vec![None, Some(f64::NAN), Some(f64::INFINITY)]),
            ),
            (
                "denominator".to_owned(),
                owned_f64(vec![Some(2.0), Some(0.0), None]),
            ),
            (
                "out".to_owned(),
                owned_f64(vec![Some(-1.0), Some(-2.0), None]),
            ),
            (
                "where".to_owned(),
                NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::boolean(vec![
                    Some(false),
                    Some(true),
                    None,
                ]))),
            ),
        ]);
        let clean = execute_array_call(
            &node("nan_to_num", "f64-column", &["input"], &json!({})),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid nan_to_num");
        assert_eq!(output(&clean), vec![None, Some(0.0), Some(f64::MAX)]);

        let divided = execute_array_call(
            &node(
                "divide",
                "f64-column",
                &["input", "denominator", "out", "where"],
                &json!({}),
            ),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid divide");
        let divided = output(&divided);
        assert_eq!(divided[0], Some(-1.0));
        assert!(divided[1].expect("NaN is not null").is_nan());
        assert_eq!(divided[2], None);
    }

    #[test]
    fn numpy_unary_fill_zero_and_absolute_difference_cover_latest_surface() {
        let values = BTreeMap::from([
            (
                "input".to_owned(),
                owned_f64(vec![Some(-4.0), None, Some(-0.0)]),
            ),
            ("fill".to_owned(), NodeValue::Float(f64::NAN)),
            ("scalar".to_owned(), NodeValue::Float(-1.0)),
        ]);
        let absolute = execute_array_call(
            &node("abs", "f64-column", &["input"], &json!({})),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid abs");
        let absolute = output(&absolute);
        assert_eq!(absolute[0], Some(4.0));
        assert_eq!(absolute[1], None);
        assert_eq!(absolute[2].expect("zero").to_bits(), 0.0_f64.to_bits());

        let filled = execute_array_call(
            &node("full_like", "f64-column", &["input", "fill"], &json!({})),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid full_like");
        assert!(output(&filled)
            .iter()
            .all(|value| value.is_some_and(f64::is_nan)));

        let zeroed = execute_array_call(
            &node("zeros_like", "f64-column", &["input"], &json!({})),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid zeros_like");
        assert_eq!(output(&zeroed), vec![Some(0.0); 3]);

        let missing = BTreeMap::from([
            (
                "input".to_owned(),
                owned_f64(vec![None, Some(f64::NAN), Some(f64::INFINITY), Some(-0.0)]),
            ),
            ("fill".to_owned(), NodeValue::Float(50.0)),
        ]);
        let filled_missing = execute_array_call(
            &node("fill-missing", "f64-column", &["input", "fill"], &json!({})),
            &missing,
            4,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid fill-missing");
        let filled_missing = output(&filled_missing);
        assert_eq!(
            filled_missing[..3],
            [Some(50.0), Some(50.0), Some(f64::INFINITY)]
        );
        assert_eq!(
            filled_missing[3].expect("negative zero").to_bits(),
            (-0.0_f64).to_bits()
        );

        let square_root = execute_array_call(
            &node("sqrt", "dynamic", &["scalar"], &json!({})),
            &values,
            3,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect("valid scalar sqrt");
        assert!(matches!(square_root, NodeValue::Float(value) if value.is_nan()));

        let difference_node = node("absolute-difference", "f64-column", &["diff"], &json!({}));
        let mut state = ArrayCallState::default();
        let first = BTreeMap::from([("diff".to_owned(), owned_f64(vec![Some(1.0), Some(4.0)]))]);
        let first = execute_array_call(&difference_node, &first, 2, &mut state, Some(&source()))
            .expect("first difference chunk");
        let first = output(&first);
        assert!(first[0].is_some_and(f64::is_nan));
        assert_eq!(first[1], Some(3.0));
        let second = BTreeMap::from([("diff".to_owned(), owned_f64(vec![Some(-2.0)]))]);
        let second = execute_array_call(&difference_node, &second, 1, &mut state, Some(&source()))
            .expect("second difference chunk");
        assert_eq!(output(&second), vec![Some(6.0)]);
    }

    #[test]
    fn stateful_native_calls_cross_batch_boundaries() {
        const HOUR_MS: i64 = 3_600_000;
        let mut state = ArrayCallState::default();
        let opening = native_node(
            "opening-range-high",
            &["date", "high", "low"],
            &json!({"cutoff_hour":2}),
        );
        let day = 1_700_000_000_000_i64.div_euclid(86_400_000) * 86_400_000;
        let first = BTreeMap::from([
            (
                "date".to_owned(),
                NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::timestamp_ms(vec![
                    Some(day),
                    Some(day + HOUR_MS),
                ]))),
            ),
            ("high".to_owned(), owned_f64(vec![Some(10.0), Some(12.0)])),
            ("low".to_owned(), owned_f64(vec![Some(5.0), Some(4.0)])),
        ]);
        let first = execute_array_call(&opening, &first, 2, &mut state, Some(&source()))
            .expect("first opening chunk");
        assert!(output(&first)
            .iter()
            .all(|value| value.is_some_and(f64::is_nan)));

        let second = BTreeMap::from([
            (
                "date".to_owned(),
                NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::timestamp_ms(vec![Some(
                    day + 2 * HOUR_MS,
                )]))),
            ),
            ("high".to_owned(), owned_f64(vec![Some(99.0)])),
            ("low".to_owned(), owned_f64(vec![Some(-1.0)])),
        ]);
        let second = execute_array_call(&opening, &second, 1, &mut state, Some(&source()))
            .expect("second opening chunk");
        assert_eq!(output(&second), vec![Some(12.0)]);
        assert_eq!(state.retained(), 1);
    }

    #[test]
    fn inside_bar_dispatch_selects_each_native_projection() {
        const HOUR_MS: i64 = 3_600_000;
        let timestamps = [0, HOUR_MS, 2 * HOUR_MS];
        let values = BTreeMap::from([
            (
                "date".to_owned(),
                NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::timestamp_ms(
                    timestamps.into_iter().map(Some).collect(),
                ))),
            ),
            (
                "high".to_owned(),
                owned_f64(vec![Some(10.0), Some(9.0), Some(99.0)]),
            ),
            (
                "low".to_owned(),
                owned_f64(vec![Some(0.0), Some(1.0), Some(-99.0)]),
            ),
        ]);
        for (name, last) in [
            ("inside-bar-ready", 1.0),
            ("inside-bar-mother-high", 10.0),
            ("inside-bar-mother-low", 0.0),
        ] {
            let actual = execute_array_call(
                &native_node(name, &["date", "high", "low"], &json!({})),
                &values,
                3,
                &mut ArrayCallState::default(),
                Some(&source()),
            )
            .expect("valid inside-bar projection");
            assert_eq!(output(&actual)[2], Some(last));
        }
    }

    #[test]
    fn unsupported_contract_reports_source_location() {
        let error = execute_array_call(
            &node("imaginary", "f64-column", &["input"], &json!({})),
            &BTreeMap::new(),
            0,
            &mut ArrayCallState::default(),
            Some(&source()),
        )
        .expect_err("unsupported array call");
        assert_eq!(
            error,
            VectorCoreError::UnsupportedOpcode {
                opcode: "array-call".to_owned(),
                location: "strategy.py:42:8".to_owned(),
            }
        );
    }
}
