use std::collections::BTreeMap;

use serde_json::Value as JsonValue;

use super::value::{
    append_text, as_column, assign_masked, bool_at, cast_bool_at, cast_f64_at, cast_i64_at,
    mask_values, numeric_at, scalar_text, shift_column, single_input, string_parameter, text_at,
    text_at_optional, three_inputs, two_inputs, value, RuntimeValue,
};
use super::{MutationEngine, MutationFrame};
use crate::column::OwnedColumn;
use crate::float::{binary, compare, BinaryFloatOp, FloatComparison};
use crate::mutation::MutationProgram;
use crate::program::ProgramNode;
use crate::VectorCoreError;

impl<'program> MutationEngine<'program> {
    /// Bind a validated program without inspecting dataframe values.
    ///
    /// # Errors
    ///
    /// Returns any structural or fingerprint error from the program.
    pub fn new(program: &'program MutationProgram) -> Result<Self, VectorCoreError> {
        program.validate()?;
        Ok(Self { program })
    }

    /// Execute entry then exit phases on an independently owned dataframe.
    ///
    /// # Errors
    ///
    /// Returns a source-located type, mask, opcode, or column error without
    /// returning a partial frame.
    pub fn execute(&self, mut frame: MutationFrame) -> Result<MutationFrame, VectorCoreError> {
        for column in &self.program.required_input_columns {
            if !frame.columns.contains_key(column) {
                return Err(VectorCoreError::MissingColumn(column.clone()));
            }
        }
        for entrypoint in &self.program.entrypoints {
            if self.program.is_tag_program() {
                let tag = if entrypoint.phase == "entry" {
                    "enter_tag"
                } else {
                    "exit_tag"
                };
                frame.write(
                    tag.to_owned(),
                    OwnedColumn::text(vec![Some(String::new()); frame.rows]),
                )?;
            }
            self.execute_function(&entrypoint.function, &mut frame)?;
        }
        Ok(frame)
    }

    fn execute_function(
        &self,
        function_id: &str,
        frame: &mut MutationFrame,
    ) -> Result<(), VectorCoreError> {
        let function = self.program.function(function_id).ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!("mutation function is missing: {function_id}"))
        })?;
        let mut values = BTreeMap::new();
        for node_id in &function.node_ids {
            let node = self.program.node(node_id).ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!("mutation node is missing: {node_id}"))
            })?;
            let value = self.execute_node(node, &values, frame)?;
            values.insert(node.id.clone(), value);
        }
        if !values.contains_key(&function.return_node) {
            return Err(self.error(
                self.program.node(&function.return_node).unwrap_or_else(|| {
                    self.program.node(&function.node_ids[0]).expect("validated")
                }),
                "mutation function did not execute its return node",
            ));
        }
        Ok(())
    }

    fn execute_node(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        frame: &mut MutationFrame,
    ) -> Result<RuntimeValue, VectorCoreError> {
        match node.op.as_str() {
            "parameter" => Ok(match node.value_type.as_str() {
                "dataframe" => RuntimeValue::DataFrame,
                "metadata" => RuntimeValue::Metadata,
                _ => RuntimeValue::Unbound,
            }),
            "literal" => self.literal(node),
            "column-read" => {
                let column = string_parameter(node, "column")?;
                frame
                    .column(column)
                    .cloned()
                    .map(RuntimeValue::Column)
                    .ok_or_else(|| VectorCoreError::MissingColumn(column.to_owned()))
            }
            "binary" => self.binary(node, values, frame.rows),
            "compare" => self.compare(node, values, frame.rows),
            "logical" => self.logical(node, values, frame.rows),
            "unary" => self.unary(node, values, frame.rows),
            "select" => self.select(node, values, frame.rows),
            "cast" => self.cast(node, values, frame.rows),
            "shift" => self.shift(node, values, frame.rows),
            "format-string" => self.format_string(node, values),
            "frame-write" => {
                self.frame_write(node, values, frame)?;
                Ok(RuntimeValue::DataFrame)
            }
            "instrumentation" => Ok(RuntimeValue::Null),
            "return" => Ok(RuntimeValue::Alias(single_input(node)?.to_owned())),
            other => Err(self.unsupported(node, other)),
        }
    }

    fn literal(&self, node: &ProgramNode) -> Result<RuntimeValue, VectorCoreError> {
        let value = node
            .parameters
            .get("value")
            .ok_or_else(|| self.error(node, "literal has no value"))?;
        Ok(match value {
            JsonValue::Null => RuntimeValue::Null,
            JsonValue::Bool(value) => RuntimeValue::Bool(*value),
            JsonValue::Number(value) if value.is_i64() => RuntimeValue::Integer(
                value
                    .as_i64()
                    .ok_or_else(|| self.error(node, "integer literal is outside i64"))?,
            ),
            JsonValue::Number(value) => RuntimeValue::Float(
                value
                    .as_f64()
                    .ok_or_else(|| self.error(node, "float literal is outside f64"))?,
            ),
            JsonValue::String(value) => RuntimeValue::Text(value.clone()),
            _ => return Err(self.error(node, "JSON collection literal is not executable")),
        })
    }

    fn binary(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [left, right] = two_inputs(node)?;
        if node.value_type.starts_with("string-") {
            if string_parameter(node, "operator")? != "add" {
                return Err(self.error(node, "string binary operation is not concatenation"));
            }
            if node.value_type == "string-scalar" {
                return Ok(RuntimeValue::Text(format!(
                    "{}{}",
                    text_at(values, left, 0)?,
                    text_at(values, right, 0)?
                )));
            }
            return Ok(RuntimeValue::Column(OwnedColumn::text(
                (0..rows)
                    .map(|row| {
                        match (
                            text_at_optional(values, left, row)?,
                            text_at_optional(values, right, row)?,
                        ) {
                            (Some(left), Some(right)) => Ok(Some(format!("{left}{right}"))),
                            _ => Ok(None),
                        }
                    })
                    .collect::<Result<Vec<_>, VectorCoreError>>()?,
            )));
        }
        let operation = match string_parameter(node, "operator")? {
            "add" => BinaryFloatOp::Add,
            "subtract" => BinaryFloatOp::Subtract,
            "multiply" => BinaryFloatOp::Multiply,
            "divide" => BinaryFloatOp::Divide,
            "modulo" => BinaryFloatOp::Remainder,
            other => return Err(self.error(node, format!("unsupported binary operator {other}"))),
        };
        if node.value_type.ends_with("-column") {
            return Ok(RuntimeValue::Column(OwnedColumn::f64(
                (0..rows)
                    .map(|row| {
                        Ok(
                            match (
                                numeric_at(values, left, row)?,
                                numeric_at(values, right, row)?,
                            ) {
                                (Some(left), Some(right)) => Some(binary(left, right, operation)),
                                _ => None,
                            },
                        )
                    })
                    .collect::<Result<Vec<_>, VectorCoreError>>()?,
            )));
        }
        Ok(
            match (numeric_at(values, left, 0)?, numeric_at(values, right, 0)?) {
                (Some(left), Some(right)) => RuntimeValue::Float(binary(left, right, operation)),
                _ => RuntimeValue::Null,
            },
        )
    }

    fn compare(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [left, right] = two_inputs(node)?;
        let operation = match string_parameter(node, "operator")? {
            "equal" => FloatComparison::Equal,
            "not-equal" => FloatComparison::NotEqual,
            "less-than" => FloatComparison::Less,
            "less-than-or-equal" => FloatComparison::LessEqual,
            "greater-than" => FloatComparison::Greater,
            "greater-than-or-equal" => FloatComparison::GreaterEqual,
            other => return Err(self.error(node, format!("unsupported comparison {other}"))),
        };
        let evaluate = |row| -> Result<Option<bool>, VectorCoreError> {
            Ok(
                match (
                    numeric_at(values, left, row)?,
                    numeric_at(values, right, row)?,
                ) {
                    (Some(left), Some(right)) => Some(compare(left, right, operation)),
                    _ => None,
                },
            )
        };
        if node.value_type == "bool-column" {
            Ok(RuntimeValue::Column(OwnedColumn::boolean(
                (0..rows).map(evaluate).collect::<Result<Vec<_>, _>>()?,
            )))
        } else {
            Ok(evaluate(0)?.map_or(RuntimeValue::Null, RuntimeValue::Bool))
        }
    }

    fn logical(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let operation = string_parameter(node, "operator")?;
        if node.inputs.is_empty() || !matches!(operation, "and" | "or") {
            return Err(self.error(node, "logical operation has invalid arity or operator"));
        }
        let evaluate = |row| -> Result<Option<bool>, VectorCoreError> {
            let mut result = bool_at(values, &node.inputs[0], row)?;
            for input in node.inputs.iter().skip(1) {
                let right = bool_at(values, input, row)?;
                result = nullable_logical(result, right, operation);
            }
            Ok(result)
        };
        if node.value_type == "bool-column" {
            Ok(RuntimeValue::Column(OwnedColumn::boolean(
                (0..rows).map(evaluate).collect::<Result<Vec<_>, _>>()?,
            )))
        } else {
            Ok(evaluate(0)?.map_or(RuntimeValue::Null, RuntimeValue::Bool))
        }
    }

    fn unary(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        let operation = string_parameter(node, "operator")?;
        if node.value_type.starts_with("bool-") {
            if !matches!(operation, "not" | "invert") {
                return Err(self.error(node, "unsupported Boolean unary operation"));
            }
            if node.value_type == "bool-column" {
                return Ok(RuntimeValue::Column(OwnedColumn::boolean(
                    (0..rows)
                        .map(|row| bool_at(values, input, row).map(|value| value.map(|item| !item)))
                        .collect::<Result<Vec<_>, _>>()?,
                )));
            }
            return Ok(bool_at(values, input, 0)?
                .map_or(RuntimeValue::Null, |value| RuntimeValue::Bool(!value)));
        }
        if !matches!(operation, "negate" | "positive") {
            return Err(self.error(node, "unsupported numeric unary operation"));
        }
        let apply = |value: f64| if operation == "negate" { -value } else { value };
        if node.value_type.ends_with("-column") {
            return Ok(RuntimeValue::Column(OwnedColumn::f64(
                (0..rows)
                    .map(|row| numeric_at(values, input, row).map(|value| value.map(apply)))
                    .collect::<Result<Vec<_>, _>>()?,
            )));
        }
        Ok(
            numeric_at(values, input, 0)?.map_or(RuntimeValue::Null, |value| {
                RuntimeValue::Float(apply(value))
            }),
        )
    }

    fn select(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [condition, truthy, falsey] = three_inputs(node)?;
        if node.value_type != "f64-column" {
            return Err(self.error(node, "select currently requires f64-column output"));
        }
        Ok(RuntimeValue::Column(OwnedColumn::f64(
            (0..rows)
                .map(|row| match bool_at(values, condition, row)? {
                    Some(true) => numeric_at(values, truthy, row),
                    Some(false) => numeric_at(values, falsey, row),
                    None => Ok(None),
                })
                .collect::<Result<Vec<_>, _>>()?,
        )))
    }

    fn cast(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        let target = string_parameter(node, "target")?;
        if target == "array" {
            return Ok(RuntimeValue::Alias(input.to_owned()));
        }
        match target {
            "int" => Ok(RuntimeValue::Column(OwnedColumn::i64(
                (0..rows)
                    .map(|row| cast_i64_at(values, input, row))
                    .collect::<Result<Vec<_>, _>>()?,
            ))),
            "float" => Ok(RuntimeValue::Column(OwnedColumn::f64(
                (0..rows)
                    .map(|row| cast_f64_at(values, input, row))
                    .collect::<Result<Vec<_>, _>>()?,
            ))),
            "bool" => Ok(RuntimeValue::Column(OwnedColumn::boolean(
                (0..rows)
                    .map(|row| cast_bool_at(values, input, row))
                    .collect::<Result<Vec<_>, _>>()?,
            ))),
            _ => Err(self.error(node, format!("unsupported cast target {target}"))),
        }
    }

    fn shift(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = value(values, single_input(node)?)?;
        let periods = node
            .parameters
            .get("periods")
            .and_then(JsonValue::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| self.error(node, "shift periods are invalid"))?;
        let column = as_column(input, rows, None)?;
        Ok(RuntimeValue::Column(shift_column(&column, periods)))
    }

    fn format_string(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let segments = node
            .parameters
            .get("segments")
            .and_then(JsonValue::as_array)
            .ok_or_else(|| self.error(node, "format-string segments are missing"))?;
        let mut result = segments[0].as_str().unwrap_or_default().to_owned();
        for (input, suffix) in node.inputs.iter().zip(segments.iter().skip(1)) {
            result.push_str(&scalar_text(value(values, input)?)?);
            result.push_str(suffix.as_str().unwrap_or_default());
        }
        Ok(RuntimeValue::Text(result))
    }

    fn frame_write(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        frame: &mut MutationFrame,
    ) -> Result<(), VectorCoreError> {
        let rows_mode = string_parameter(node, "rows")?;
        let assignment = string_parameter(node, "assignment")?;
        let columns = node.parameters["columns"]
            .as_array()
            .expect("validated frame-write columns")
            .iter()
            .map(|column| column.as_str().expect("validated column"))
            .collect::<Vec<_>>();
        let mut offset = 1;
        let mask = if rows_mode == "mask" {
            let mask = value(values, &node.inputs[offset])?;
            offset += 1;
            Some(mask_values(mask, frame.rows)?)
        } else {
            None
        };
        let sources = if assignment == "scalar-broadcast" {
            vec![value(values, &node.inputs[offset])?; columns.len()]
        } else {
            node.inputs[offset..]
                .iter()
                .map(|input| value(values, input))
                .collect::<Result<Vec<_>, _>>()?
        };
        for (column, source) in columns.into_iter().zip(sources) {
            let updated = if assignment == "string-append" {
                let existing = frame.column(column).ok_or_else(|| {
                    self.error(node, format!("append target is missing: {column}"))
                })?;
                append_text(existing, source, mask.as_deref(), frame.rows)?
            } else if let Some(mask) = mask.as_deref() {
                let existing = frame.column(column).ok_or_else(|| {
                    self.error(node, format!("masked target is missing: {column}"))
                })?;
                assign_masked(existing, source, mask, frame.rows)?
            } else {
                let preferred = frame
                    .column(column)
                    .map(|value| value.as_view().value_type());
                as_column(source, frame.rows, preferred)?
            };
            frame.write(column.to_owned(), updated)?;
        }
        Ok(())
    }

    fn unsupported(&self, node: &ProgramNode, opcode: &str) -> VectorCoreError {
        let location = self.location(node);
        VectorCoreError::UnsupportedOpcode {
            opcode: opcode.to_owned(),
            location,
        }
    }

    fn error(&self, node: &ProgramNode, message: impl Into<String>) -> VectorCoreError {
        VectorCoreError::Execution {
            node: node.id.clone(),
            message: format!("{}: {}", self.location(node), message.into()),
        }
    }

    fn location(&self, node: &ProgramNode) -> String {
        self.program.source_map.get(&node.id).map_or_else(
            || "strategy.py:?:?".to_owned(),
            |location| format!("{}:{}:{}", location.path, location.line, location.column),
        )
    }
}

fn nullable_logical(left: Option<bool>, right: Option<bool>, operation: &str) -> Option<bool> {
    match operation {
        "and" if left == Some(false) || right == Some(false) => Some(false),
        "and" if left == Some(true) && right == Some(true) => Some(true),
        "or" if left == Some(true) || right == Some(true) => Some(true),
        "or" if left == Some(false) && right == Some(false) => Some(false),
        "and" | "or" => None,
        _ => unreachable!("logical operator was validated"),
    }
}

#[cfg(test)]
mod tests {
    use super::nullable_logical;

    #[test]
    fn nullable_logic_matches_pandas_kleene_truth_tables() {
        assert_eq!(nullable_logical(Some(false), None, "and"), Some(false));
        assert_eq!(nullable_logical(Some(true), None, "and"), None);
        assert_eq!(nullable_logical(Some(true), None, "or"), Some(true));
        assert_eq!(nullable_logical(Some(false), None, "or"), None);
    }
}
