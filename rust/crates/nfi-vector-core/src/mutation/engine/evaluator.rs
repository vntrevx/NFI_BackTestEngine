use std::collections::BTreeMap;

use serde_json::Value as JsonValue;

use super::value::{
    append_text, as_column, assign_masked, mask_values, scalar_text, shift_column, single_input,
    string_parameter, three_inputs, two_inputs, value, RuntimeValue,
};
use super::{MutationEngine, MutationFrame};
use crate::column::OwnedColumn;
use crate::float::{binary, compare, BinaryFloatOp, FloatComparison};
use crate::mutation::validation::numpy_array_arguments_are_supported;
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
    pub fn execute(&self, frame: MutationFrame) -> Result<MutationFrame, VectorCoreError> {
        self.execute_with_metadata(frame, &BTreeMap::new())
    }

    /// Execute entry and exit phases with explicit immutable strategy metadata.
    ///
    /// # Errors
    ///
    /// Returns a source-located error when a compiled metadata read has no
    /// exact string value. Metadata is never inferred from dataframe content.
    pub fn execute_with_metadata(
        &self,
        mut frame: MutationFrame,
        metadata: &BTreeMap<String, String>,
    ) -> Result<MutationFrame, VectorCoreError> {
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
            self.execute_function(&entrypoint.function, &mut frame, metadata)?;
        }
        Ok(frame)
    }

    fn execute_function(
        &self,
        function_id: &str,
        frame: &mut MutationFrame,
        metadata: &BTreeMap<String, String>,
    ) -> Result<(), VectorCoreError> {
        let function = self.program.function(function_id).ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!("mutation function is missing: {function_id}"))
        })?;
        let mut values = BTreeMap::new();
        let mut remaining_uses = self.function_input_uses(function)?;
        for node_id in &function.node_ids {
            let node = self.program.node(node_id).ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!("mutation node is missing: {node_id}"))
            })?;
            let value = self.execute_node(node, &values, frame, metadata)?;
            values.insert(node.id.clone(), value);
            if node.op != "return" {
                release_consumed_inputs(&node.inputs, &mut remaining_uses, &mut values)?;
            }
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

    fn function_input_uses(
        &self,
        function: &crate::program::ProgramFunction,
    ) -> Result<BTreeMap<String, usize>, VectorCoreError> {
        let mut uses = function
            .node_ids
            .iter()
            .map(|id| (id.clone(), 0_usize))
            .collect::<BTreeMap<_, _>>();
        for node_id in &function.node_ids {
            let node = self.program.node(node_id).ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!("mutation node is missing: {node_id}"))
            })?;
            for input in &node.inputs {
                let count = uses.get_mut(input).ok_or_else(|| {
                    VectorCoreError::InvalidProgram(format!(
                        "mutation node {node_id} references non-local input {input}"
                    ))
                })?;
                *count = count.checked_add(1).ok_or_else(|| {
                    VectorCoreError::InvalidProgram(
                        "mutation input use count is too large".to_owned(),
                    )
                })?;
            }
        }
        Ok(uses)
    }

    fn execute_node(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        frame: &mut MutationFrame,
        metadata: &BTreeMap<String, String>,
    ) -> Result<RuntimeValue, VectorCoreError> {
        match node.op.as_str() {
            "parameter" => Ok(match node.value_type.as_str() {
                "dataframe" => RuntimeValue::DataFrame,
                "metadata" => RuntimeValue::Metadata,
                _ => RuntimeValue::Unbound,
            }),
            "literal" => self.literal(node),
            "row-count" => self.row_count(node, values, frame.rows),
            "metadata-read" => self.metadata_read(node, values, metadata),
            "string-split-index" => self.string_split_index(node, values),
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
            "membership" => self.membership(node, values, frame.rows),
            "masked-string-append" => self.masked_string_append(node, values, frame.rows),
            "logical" => self.logical(node, values, frame.rows),
            "unary" => self.unary(node, values, frame.rows),
            "select" => self.select(node, values, frame.rows),
            "array-call" => self.array_call(node, values, frame.rows),
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

    fn metadata_read(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        metadata: &BTreeMap<String, String>,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        if !matches!(value(values, input)?, RuntimeValue::Metadata) {
            return Err(self.error(node, "metadata-read input is not metadata"));
        }
        let key = string_parameter(node, "key")?;
        metadata
            .get(key)
            .cloned()
            .map(RuntimeValue::Text)
            .ok_or_else(|| self.error(node, format!("runtime metadata has no string key {key:?}")))
    }

    fn literal(&self, node: &ProgramNode) -> Result<RuntimeValue, VectorCoreError> {
        if let Some(special) = node.parameters.get("special") {
            if node.parameters.len() != 1 || node.value_type != "f64-scalar" {
                return Err(self.error(node, "special literal contract is invalid"));
            }
            return Ok(RuntimeValue::Float(match special.as_str() {
                Some("nan") => crate::float::canonicalize(f64::NAN),
                Some("+infinity") => f64::INFINITY,
                Some("-infinity") => f64::NEG_INFINITY,
                _ => return Err(self.error(node, "special float literal is unsupported")),
            }));
        }
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

    fn row_count(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        if !matches!(value(values, input)?, RuntimeValue::DataFrame) {
            return Err(self.error(node, "row-count input is not a dataframe"));
        }
        Ok(RuntimeValue::Integer(
            i64::try_from(rows).map_err(|_| self.error(node, "row-count exceeds i64"))?,
        ))
    }

    fn string_split_index(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        let RuntimeValue::Text(source) = value(values, input)? else {
            return Err(self.error(node, "string split input is not scalar text"));
        };
        let method = string_parameter(node, "method")?;
        let separator = string_parameter(node, "separator")?;
        if separator.is_empty() {
            return Err(self.error(node, "string split separator is empty"));
        }
        let index = node
            .parameters
            .get("index")
            .and_then(JsonValue::as_i64)
            .ok_or_else(|| self.error(node, "string split index is not a signed integer"))?;
        let parts = match method {
            "partition" => source.find(separator).map_or_else(
                || vec![source.clone(), String::new(), String::new()],
                |position| {
                    let after = position + separator.len();
                    vec![
                        source[..position].to_owned(),
                        separator.to_owned(),
                        source[after..].to_owned(),
                    ]
                },
            ),
            "split" => source.split(separator).map(str::to_owned).collect(),
            "rsplit" => {
                let mut parts = source
                    .rsplit(separator)
                    .map(str::to_owned)
                    .collect::<Vec<_>>();
                parts.reverse();
                parts
            }
            _ => return Err(self.error(node, "string split method is unsupported")),
        };
        let length = i64::try_from(parts.len())
            .map_err(|_| self.error(node, "string split result length exceeds i64"))?;
        let resolved = if index < 0 {
            length.checked_add(index)
        } else {
            Some(index)
        }
        .and_then(|index| usize::try_from(index).ok())
        .filter(|index| *index < parts.len())
        .ok_or_else(|| self.error(node, "string split result index is outside its result"))?;
        Ok(RuntimeValue::Text(parts[resolved].clone()))
    }

    fn membership(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        let collection = node
            .parameters
            .get("values")
            .and_then(JsonValue::as_array)
            .ok_or_else(|| self.error(node, "membership values are not an array"))?;
        let negated = node
            .parameters
            .get("negated")
            .and_then(JsonValue::as_bool)
            .ok_or_else(|| self.error(node, "membership negation is not Boolean"))?;
        let contains = |item: Option<&str>| {
            let member = collection.iter().any(|candidate| match item {
                Some(item) => candidate.as_str() == Some(item),
                None => candidate.is_null(),
            });
            member != negated
        };
        match value(values, input)? {
            RuntimeValue::Text(item) if node.value_type == "bool-scalar" => {
                Ok(RuntimeValue::Bool(contains(Some(item))))
            }
            RuntimeValue::Column(column)
                if node.value_type == "bool-column"
                    && column.as_view().value_type() == crate::column::ValueType::Text
                    && column.len() == rows =>
            {
                let view = column.as_view();
                Ok(RuntimeValue::Column(OwnedColumn::boolean(
                    (0..rows)
                        .map(|row| Some(contains(view.text_at(row))))
                        .collect(),
                )))
            }
            _ => Err(self.error(node, "membership input or output type is invalid")),
        }
    }

    fn masked_string_append(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [target, mask, suffix] = three_inputs(node)?;
        let RuntimeValue::Column(target) = value(values, target)? else {
            return Err(self.error(node, "masked string append target is not a column"));
        };
        let mask_value = value(values, mask)?;
        let RuntimeValue::Text(suffix) = value(values, suffix)? else {
            return Err(self.error(node, "masked string append suffix is not scalar text"));
        };
        if node.value_type != "string-column"
            || target.as_view().value_type() != crate::column::ValueType::Text
            || target.len() != rows
        {
            return Err(self.error(node, "masked string append target contract is invalid"));
        }
        let target = target.as_view();
        Ok(RuntimeValue::Column(OwnedColumn::text(
            (0..rows)
                .map(|row| match mask_value.bool_at(mask, row)? {
                    Some(true) => target.text_at(row).map_or_else(
                        || Err(self.error(node, "masked string append selected a null target")),
                        |prefix| Ok(Some(format!("{prefix}{suffix}"))),
                    ),
                    Some(false) | None => Ok(target.text_at(row).map(str::to_owned)),
                })
                .collect::<Result<Vec<_>, VectorCoreError>>()?,
        )))
    }

    fn array_call(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let name = string_parameter(node, "name")?;
        let arguments = node
            .parameters
            .get("arguments")
            .and_then(JsonValue::as_object);
        if node.parameters.len() != 3
            || string_parameter(node, "family")? != "numpy"
            || arguments.is_none_or(|arguments| {
                !numpy_array_arguments_are_supported(name, &node.value_type, arguments)
            })
        {
            return Err(self.error(node, "unsupported array-call contract"));
        }
        match name {
            "full" => self.array_full(node, values, rows),
            "full_like" => self.array_full_like(node, values, rows),
            "divide" => self.array_divide_where(node, values, rows),
            "isnan" => self.array_isnan(node, values, rows),
            _ => Err(self.error(node, "unsupported array-call contract")),
        }
    }

    fn array_isnan(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let input = single_input(node)?;
        let input_value = value(values, input)?;
        if node.value_type != "bool-column" {
            return Err(self.error(node, "numpy isnan requires bool-column output"));
        }
        Ok(RuntimeValue::Column(OwnedColumn::boolean(
            (0..rows)
                .map(|row| {
                    input_value
                        .numeric_at(input, row)
                        .map(|value| value.map(f64::is_nan))
                })
                .collect::<Result<Vec<_>, _>>()?,
        )))
    }

    fn array_full(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        if node.inputs.len() != 2 {
            return Err(self.error(node, "numpy full requires two inputs"));
        }
        let size = match value(values, &node.inputs[0])? {
            RuntimeValue::Integer(value) => usize::try_from(*value)
                .map_err(|_| self.error(node, "array-call size is outside usize"))?,
            _ => return Err(self.error(node, "array-call size is not an integer")),
        };
        if size != rows {
            return Err(self.error(node, "array-call size differs from dataframe rows"));
        }
        let fill = value(values, &node.inputs[1])?;
        match (node.value_type.as_str(), fill) {
            ("bool-column", RuntimeValue::Bool(value)) => {
                Ok(RuntimeValue::Column(OwnedColumn::boolean(vec![
                    Some(*value);
                    rows
                ])))
            }
            ("int-column", RuntimeValue::Integer(value)) => {
                Ok(RuntimeValue::Column(OwnedColumn::i64(vec![
                    Some(*value);
                    rows
                ])))
            }
            ("f64-column", RuntimeValue::Float(value)) => {
                Ok(RuntimeValue::Column(OwnedColumn::f64(vec![
                    Some(*value);
                    rows
                ])))
            }
            ("string-column", RuntimeValue::Text(value)) => {
                Ok(RuntimeValue::Column(OwnedColumn::text(vec![
                    Some(
                        value.clone()
                    );
                    rows
                ])))
            }
            _ => Err(self.error(node, "array-call fill value differs from its output type")),
        }
    }

    fn array_full_like(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [template, fill] = two_inputs(node)?;
        let RuntimeValue::Column(template) = value(values, template)? else {
            return Err(self.error(node, "numpy full_like template is not a column"));
        };
        if node.value_type != "f64-column"
            || template.as_view().value_type() != crate::column::ValueType::F64
            || template.len() != rows
        {
            return Err(self.error(node, "numpy full_like requires a Float64 template"));
        }
        let fill = match value(values, fill)? {
            RuntimeValue::Integer(value) => super::value::i64_as_f64(*value),
            RuntimeValue::Float(value) => *value,
            _ => return Err(self.error(node, "numpy full_like fill is not numeric scalar")),
        };
        Ok(RuntimeValue::Column(OwnedColumn::f64(vec![
            Some(fill);
            rows
        ])))
    }

    fn array_divide_where(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [numerator, denominator, out, where_mask] = match node.inputs.as_slice() {
            [numerator, denominator, out, where_mask] => [
                numerator.as_str(),
                denominator.as_str(),
                out.as_str(),
                where_mask.as_str(),
            ],
            _ => return Err(self.error(node, "numpy divide requires x1, x2, out, and where")),
        };
        let numerator_value = value(values, numerator)?;
        let denominator_value = value(values, denominator)?;
        let out_value = value(values, out)?;
        let where_value = value(values, where_mask)?;
        let RuntimeValue::Column(out_column) = out_value else {
            return Err(self.error(node, "numpy divide out is not a column"));
        };
        if node.value_type != "f64-column"
            || out_column.as_view().value_type() != crate::column::ValueType::F64
            || out_column.len() != rows
        {
            return Err(self.error(node, "numpy divide out is not a Float64 result buffer"));
        }

        let out_view = out_column.as_view();
        Ok(RuntimeValue::Column(OwnedColumn::f64(
            (0..rows)
                .map(|row| match where_value.bool_at(where_mask, row)? {
                    Some(true) => {
                        match (
                            numerator_value.numeric_at(numerator, row)?,
                            denominator_value.numeric_at(denominator, row)?,
                        ) {
                            (Some(left), Some(right)) => {
                                Ok(Some(binary(left, right, BinaryFloatOp::Divide)))
                            }
                            _ => Ok(None),
                        }
                    }
                    Some(false) | None => Ok(out_view.f64_at(row)),
                })
                .collect::<Result<Vec<_>, VectorCoreError>>()?,
        )))
    }

    fn binary(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [left, right] = two_inputs(node)?;
        let left_value = value(values, left)?;
        let right_value = value(values, right)?;
        if node.value_type.starts_with("string-") {
            if string_parameter(node, "operator")? != "add" {
                return Err(self.error(node, "string binary operation is not concatenation"));
            }
            if node.value_type == "string-scalar" {
                return Ok(RuntimeValue::Text(format!(
                    "{}{}",
                    left_value.text_at(left, 0)?,
                    right_value.text_at(right, 0)?
                )));
            }
            return Ok(RuntimeValue::Column(OwnedColumn::text(
                (0..rows)
                    .map(|row| {
                        match (
                            left_value.text_at_optional(left, row)?,
                            right_value.text_at_optional(right, row)?,
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
                                left_value.numeric_at(left, row)?,
                                right_value.numeric_at(right, row)?,
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
            match (
                left_value.numeric_at(left, 0)?,
                right_value.numeric_at(right, 0)?,
            ) {
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
        let left_value = value(values, left)?;
        let right_value = value(values, right)?;
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
                    left_value.numeric_at(left, row)?,
                    right_value.numeric_at(right, row)?,
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
        let inputs = node
            .inputs
            .iter()
            .map(|input| Ok((input.as_str(), value(values, input)?)))
            .collect::<Result<Vec<_>, VectorCoreError>>()?;
        let evaluate = |row| -> Result<Option<bool>, VectorCoreError> {
            let mut result = inputs[0].1.bool_at(inputs[0].0, row)?;
            for (input, input_value) in inputs.iter().skip(1) {
                // Pandas' nullable Boolean algebra has two absorbing values:
                // false for AND and true for OR. Once reached, later operands
                // cannot change either the value or its nullability.
                if (operation == "and" && result == Some(false))
                    || (operation == "or" && result == Some(true))
                {
                    break;
                }
                let right = input_value.bool_at(input, row)?;
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
        let input_value = value(values, input)?;
        let operation = string_parameter(node, "operator")?;
        if node.value_type.starts_with("bool-") {
            if !matches!(operation, "not" | "invert") {
                return Err(self.error(node, "unsupported Boolean unary operation"));
            }
            if node.value_type == "bool-column" {
                return Ok(RuntimeValue::Column(OwnedColumn::boolean(
                    (0..rows)
                        .map(|row| {
                            input_value
                                .bool_at(input, row)
                                .map(|value| value.map(|item| !item))
                        })
                        .collect::<Result<Vec<_>, _>>()?,
                )));
            }
            return Ok(input_value
                .bool_at(input, 0)?
                .map_or(RuntimeValue::Null, |value| RuntimeValue::Bool(!value)));
        }
        if !matches!(operation, "negate" | "positive") {
            return Err(self.error(node, "unsupported numeric unary operation"));
        }
        let apply = |value: f64| if operation == "negate" { -value } else { value };
        if node.value_type.ends_with("-column") {
            return Ok(RuntimeValue::Column(OwnedColumn::f64(
                (0..rows)
                    .map(|row| {
                        input_value
                            .numeric_at(input, row)
                            .map(|value| value.map(apply))
                    })
                    .collect::<Result<Vec<_>, _>>()?,
            )));
        }
        Ok(input_value
            .numeric_at(input, 0)?
            .map_or(RuntimeValue::Null, |value| {
                RuntimeValue::Float(apply(value))
            }))
    }

    fn select(
        &self,
        node: &ProgramNode,
        values: &BTreeMap<String, RuntimeValue>,
        rows: usize,
    ) -> Result<RuntimeValue, VectorCoreError> {
        let [condition, truthy, falsey] = three_inputs(node)?;
        let condition_value = value(values, condition)?;
        let truthy_value = value(values, truthy)?;
        let falsey_value = value(values, falsey)?;
        if node.value_type != "f64-column" {
            return Err(self.error(node, "select currently requires f64-column output"));
        }
        Ok(RuntimeValue::Column(OwnedColumn::f64(
            (0..rows)
                .map(|row| match condition_value.bool_at(condition, row)? {
                    Some(true) => truthy_value.numeric_at(truthy, row),
                    Some(false) => falsey_value.numeric_at(falsey, row),
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
        let input_value = value(values, input)?;
        let target = string_parameter(node, "target")?;
        if target == "array" {
            return Ok(input_value.clone());
        }
        match target {
            "int" => Ok(RuntimeValue::Column(OwnedColumn::i64(
                (0..rows)
                    .map(|row| input_value.cast_i64_at(input, row))
                    .collect::<Result<Vec<_>, _>>()?,
            ))),
            "float" => Ok(RuntimeValue::Column(OwnedColumn::f64(
                (0..rows)
                    .map(|row| input_value.cast_f64_at(input, row))
                    .collect::<Result<Vec<_>, _>>()?,
            ))),
            "bool" => Ok(RuntimeValue::Column(OwnedColumn::boolean(
                (0..rows)
                    .map(|row| input_value.cast_bool_at(input, row))
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

fn release_consumed_inputs(
    inputs: &[String],
    remaining_uses: &mut BTreeMap<String, usize>,
    values: &mut BTreeMap<String, RuntimeValue>,
) -> Result<(), VectorCoreError> {
    for input in inputs {
        let remaining = remaining_uses.get_mut(input).ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!(
                "mutation input {input} has no liveness record"
            ))
        })?;
        *remaining = remaining.checked_sub(1).ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!(
                "mutation input {input} was consumed too many times"
            ))
        })?;
        if *remaining == 0 {
            values.remove(input);
        }
    }
    Ok(())
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
