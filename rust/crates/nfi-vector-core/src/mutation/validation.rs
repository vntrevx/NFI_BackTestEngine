use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::{json, Value};

use super::model::{MutationProgram, SIGNAL_PROGRAM_VERSION, TAG_PROGRAM_VERSION};
use crate::program::{Lookback, ProgramFunction, ProgramNode};
use crate::VectorCoreError;

const SIGNAL_COLUMNS: [&str; 4] = ["enter_long", "enter_short", "exit_long", "exit_short"];
const TAG_COLUMNS: [&str; 2] = ["enter_tag", "exit_tag"];

impl MutationProgram {
    /// Parse and independently validate a Signal or Tag mutation program.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error for any structural, semantic, or
    /// fingerprint mismatch.
    pub fn from_json(input: &str) -> Result<Self, VectorCoreError> {
        let program: Self = serde_json::from_str(input)
            .map_err(|error| invalid(format!("invalid JSON mutation program: {error}")))?;
        program.validate()?;
        Ok(program)
    }

    /// Validate the complete source-ordered mutation contract.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error without accepting a partial contract.
    pub fn validate(&self) -> Result<(), VectorCoreError> {
        self.validate_metadata()?;
        let positions = self.validate_nodes()?;
        let functions = self.validate_functions(&positions)?;
        self.validate_writes(&functions)?;
        self.validate_inventories()?;
        self.validate_source_map()?;
        self.validate_lookback()?;
        self.validate_schema_contract()?;
        self.validate_fingerprint()
    }

    fn validate_metadata(&self) -> Result<(), VectorCoreError> {
        if !matches!(
            self.schema_version.as_str(),
            SIGNAL_PROGRAM_VERSION | TAG_PROGRAM_VERSION
        ) {
            return Err(invalid(format!(
                "unsupported mutation schema_version: {}",
                self.schema_version
            )));
        }
        if self.source.path.is_empty()
            || self.selected_class.is_empty()
            || !is_sha256(&self.source.sha256)
            || !is_sha256(&self.fingerprint)
        {
            return Err(invalid("mutation source identity is invalid"));
        }
        if self.compile_context.run_mode != "backtest"
            || !matches!(
                self.compile_context.trading_mode.as_str(),
                "spot" | "futures"
            )
        {
            return Err(invalid("mutation compile context is invalid"));
        }
        if self.entrypoints
            != [
                super::MutationEntrypoint {
                    phase: "entry".to_owned(),
                    function: "f1".to_owned(),
                },
                super::MutationEntrypoint {
                    phase: "exit".to_owned(),
                    function: "f2".to_owned(),
                },
            ]
        {
            return Err(invalid("mutation entrypoints are not canonical"));
        }
        Ok(())
    }

    fn validate_nodes(&self) -> Result<HashMap<&str, usize>, VectorCoreError> {
        if self.nodes.is_empty() {
            return Err(invalid("mutation program has no nodes"));
        }
        for (index, node) in self.nodes.iter().enumerate() {
            if node.id != format!("n{}", index + 1)
                || !known_opcode(&node.op)
                || !known_value_type(&node.value_type)
            {
                return Err(invalid(format!("invalid mutation node: {}", node.id)));
            }
            validate_lookback(&node.lookback, &node.id)?;
        }
        let positions = self.node_positions();
        for (index, node) in self.nodes.iter().enumerate() {
            for input in &node.inputs {
                if positions
                    .get(input.as_str())
                    .is_none_or(|input_index| *input_index >= index)
                {
                    return Err(invalid(format!(
                        "mutation node {} has a non-prior input {input}",
                        node.id
                    )));
                }
            }
        }
        Ok(positions)
    }

    fn validate_functions<'a>(
        &'a self,
        positions: &HashMap<&str, usize>,
    ) -> Result<HashMap<&'a str, &'a ProgramFunction>, VectorCoreError> {
        if self.functions.len() < 2 {
            return Err(invalid("mutation program has fewer than two functions"));
        }
        let mut functions = HashMap::new();
        let mut owned = HashSet::new();
        for (index, function) in self.functions.iter().enumerate() {
            if function.id != format!("f{}", index + 1)
                || function.source_name.is_empty()
                || !matches!(
                    function.kind.as_str(),
                    "entrypoint-entry" | "entrypoint-exit" | "helper"
                )
                || functions.insert(function.id.as_str(), function).is_some()
            {
                return Err(invalid(format!(
                    "invalid mutation function: {}",
                    function.id
                )));
            }
            if function.node_ids.is_empty() || !function.node_ids.contains(&function.return_node) {
                return Err(invalid(format!(
                    "mutation function {} has an invalid node inventory",
                    function.id
                )));
            }
            let mut parameter_names = HashSet::new();
            for parameter in &function.parameters {
                let node = self.node(&parameter.node).ok_or_else(|| {
                    invalid(format!(
                        "mutation parameter node is missing: {}",
                        parameter.node
                    ))
                })?;
                if parameter.name.is_empty()
                    || !known_value_type(&parameter.value_type)
                    || !parameter_names.insert(parameter.name.as_str())
                    || node.function != function.id
                    || node.op != "parameter"
                    || node.value_type != parameter.value_type
                    || node.parameters.get("name").and_then(Value::as_str)
                        != Some(parameter.name.as_str())
                {
                    return Err(invalid(format!(
                        "mutation function {} has an invalid parameter",
                        function.id
                    )));
                }
            }
            for (source_order, node_id) in function.node_ids.iter().enumerate() {
                let position = positions.get(node_id.as_str()).ok_or_else(|| {
                    invalid(format!("mutation function owns missing node {node_id}"))
                })?;
                let node = &self.nodes[*position];
                if node.function != function.id
                    || node.source_order != u64::try_from(source_order).unwrap_or(u64::MAX)
                    || !owned.insert(node_id.as_str())
                {
                    return Err(invalid(format!(
                        "mutation function {} node ownership differs",
                        function.id
                    )));
                }
            }
        }
        if owned.len() != self.nodes.len() {
            return Err(invalid("mutation function node ownership is incomplete"));
        }
        let entry = functions.get("f1").expect("canonical functions contain f1");
        let exit = functions.get("f2").expect("canonical functions contain f2");
        if entry.source_name != "populate_entry_trend"
            || entry.kind != "entrypoint-entry"
            || exit.source_name != "populate_exit_trend"
            || exit.kind != "entrypoint-exit"
        {
            return Err(invalid("mutation entrypoint identities differ"));
        }
        Ok(functions)
    }

    fn validate_writes(
        &self,
        functions: &HashMap<&str, &ProgramFunction>,
    ) -> Result<(), VectorCoreError> {
        for node in &self.nodes {
            if !functions.contains_key(node.function.as_str()) {
                return Err(invalid(format!(
                    "mutation node {} has unknown owner",
                    node.id
                )));
            }
            if node.op == "frame-write" {
                self.validate_frame_write(node)?;
            }
            if node.op == "format-string" {
                self.validate_format_string(node)?;
            }
            if node.op == "literal" {
                Self::validate_literal(node)?;
            }
            if node.op == "array-call" {
                self.validate_array_call(node)?;
            }
            if node.op == "string-split-index" {
                self.validate_string_split_index(node)?;
            }
            if node.op == "metadata-read" {
                self.validate_metadata_read(node)?;
            }
            if node.op == "membership" {
                self.validate_membership(node)?;
            }
            if node.op == "masked-string-append" {
                self.validate_masked_string_append(node)?;
            }
            if node.op == "function-call" {
                return Err(invalid(format!(
                    "mutation helper execution is not yet exact at {}",
                    node.id
                )));
            }
        }
        Ok(())
    }

    fn validate_metadata_read(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let key = node.parameters.get("key").and_then(Value::as_str);
        let valid = node.parameters.len() == 1
            && key.is_some_and(|key| !key.is_empty())
            && node.value_type == "string-scalar"
            && node.inputs.len() == 1
            && self
                .node(&node.inputs[0])
                .is_some_and(|input| input.value_type == "metadata");
        if !valid {
            return Err(invalid(format!(
                "metadata-read {} contract is invalid",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_string_split_index(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let method = required_string(&node.parameters, "method", node)?;
        let separator = required_string(&node.parameters, "separator", node)?;
        let index = node.parameters.get("index").and_then(Value::as_i64);
        let valid = node.parameters.len() == 3
            && matches!(method, "partition" | "split" | "rsplit")
            && !separator.is_empty()
            && index.is_some()
            && node.value_type == "string-scalar"
            && node.inputs.len() == 1
            && self
                .node(&node.inputs[0])
                .is_some_and(|input| input.value_type == "string-scalar");
        if !valid {
            return Err(invalid(format!(
                "string-split-index {} contract is invalid",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_membership(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let values = node.parameters.get("values").and_then(Value::as_array);
        let negated = node.parameters.get("negated").and_then(Value::as_bool);
        let input_type = node
            .inputs
            .first()
            .and_then(|input| self.node(input))
            .map(|input| input.value_type.as_str());
        let values_are_scalar = values.is_some_and(|values| {
            values.iter().all(|value| {
                value.is_null() || value.is_boolean() || value.is_number() || value.is_string()
            })
        });
        let valid = node.parameters.len() == 2
            && node.inputs.len() == 1
            && negated.is_some()
            && values_are_scalar
            && matches!(
                (input_type, node.value_type.as_str()),
                (Some("string-scalar"), "bool-scalar") | (Some("string-column"), "bool-column")
            );
        if !valid {
            return Err(invalid(format!(
                "membership {} contract is invalid",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_masked_string_append(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let input_types = node
            .inputs
            .iter()
            .map(|input| self.node(input).map(|input| input.value_type.as_str()))
            .collect::<Option<Vec<_>>>();
        let valid = node.parameters.is_empty()
            && node.value_type == "string-column"
            && input_types.as_deref().is_some_and(|inputs| {
                matches!(
                    inputs,
                    [
                        "string-column",
                        "bool-scalar" | "bool-column",
                        "string-scalar"
                    ]
                )
            });
        if !valid {
            return Err(invalid(format!(
                "masked-string-append {} contract is invalid",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_literal(node: &ProgramNode) -> Result<(), VectorCoreError> {
        let has_value = node.parameters.contains_key("value");
        let has_special = node.parameters.contains_key("special");
        if has_value == has_special || node.parameters.len() != 1 {
            return Err(invalid(format!(
                "literal {} must have exactly one ordinary or special value",
                node.id
            )));
        }
        if has_special {
            let special = node.parameters.get("special").and_then(Value::as_str);
            if node.value_type != "f64-scalar"
                || !matches!(special, Some("nan" | "+infinity" | "-infinity"))
            {
                return Err(invalid(format!(
                    "literal {} special float contract is invalid",
                    node.id
                )));
            }
        }
        Ok(())
    }

    fn validate_array_call(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let parameters = &node.parameters;
        let family = required_string(parameters, "family", node)?;
        let name = required_string(parameters, "name", node)?;
        let arguments = parameters
            .get("arguments")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid(format!("array-call {} has no arguments map", node.id)))?;
        if parameters.len() != 3
            || family != "numpy"
            || !numpy_array_arguments_are_supported(name, &node.value_type, arguments)
        {
            return Err(invalid(format!(
                "array-call {} contract is invalid",
                node.id
            )));
        }
        let input_types = node
            .inputs
            .iter()
            .map(|input| {
                self.node(input)
                    .map(|input| input.value_type.as_str())
                    .ok_or_else(|| invalid(format!("array-call {} input is missing", node.id)))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let valid = match name {
            "full" => {
                input_types.len() == 2
                    && input_types[0] == "int-scalar"
                    && matches!(
                        (node.value_type.as_str(), input_types[1]),
                        ("bool-column", "bool-scalar")
                            | ("int-column", "int-scalar")
                            | ("f64-column", "f64-scalar")
                            | ("string-column", "string-scalar")
                    )
            }
            "full_like" => {
                node.value_type == "f64-column"
                    && matches!(
                        input_types.as_slice(),
                        ["f64-column", "f64-scalar" | "int-scalar"]
                    )
            }
            "divide" => {
                node.value_type == "f64-column"
                    && input_types.as_slice()
                        == ["f64-column", "f64-column", "f64-column", "bool-column"]
            }
            "isnan" => node.value_type == "bool-column" && input_types.as_slice() == ["f64-column"],
            _ => false,
        };
        if !valid {
            return Err(invalid(format!(
                "array-call {} signature is unsupported",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_frame_write(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let parameters = &node.parameters;
        let rows = required_string(parameters, "rows", node)?;
        let mode = required_string(parameters, "mode", node)?;
        let assignment = required_string(parameters, "assignment", node)?;
        let columns = parameters
            .get("columns")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid(format!("frame-write {} has no columns", node.id)))?;
        let columns = columns
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|column| !column.is_empty())
                    .ok_or_else(|| invalid(format!("frame-write {} column is invalid", node.id)))
            })
            .collect::<Result<Vec<_>, _>>()?;
        if columns.is_empty()
            || columns.iter().collect::<HashSet<_>>().len() != columns.len()
            || !matches!(rows, "all" | "mask")
            || !matches!(mode, "column" | "loc")
            || !matches!(
                assignment,
                "column-values" | "scalar-broadcast" | "string-append"
            )
            || (mode == "column" && (rows != "all" || columns.len() != 1))
        {
            return Err(invalid(format!(
                "frame-write {} contract is invalid",
                node.id
            )));
        }
        let entry = node.function == "f1";
        let allowed = if entry {
            ["enter_long", "enter_short", "enter_tag"]
        } else if node.function == "f2" {
            ["exit_long", "exit_short", "exit_tag"]
        } else {
            return Err(invalid("dataframe writes inside helpers are unsupported"));
        };
        if columns.iter().any(|column| !allowed.contains(column)) {
            return Err(invalid(format!(
                "frame-write {} crosses its source phase",
                node.id
            )));
        }
        if self.schema_version == SIGNAL_PROGRAM_VERSION
            && columns.iter().any(|column| TAG_COLUMNS.contains(column))
        {
            return Err(invalid("signal program writes a tag column"));
        }
        if assignment == "string-append"
            && (columns.len() != 1 || !TAG_COLUMNS.contains(&columns[0]))
        {
            return Err(invalid(format!(
                "frame-write {} append target is invalid",
                node.id
            )));
        }
        let value_count = if matches!(assignment, "scalar-broadcast" | "string-append") {
            1
        } else {
            columns.len()
        };
        let expected_inputs = 1 + usize::from(rows == "mask") + value_count;
        if node.inputs.len() != expected_inputs
            || self
                .node(&node.inputs[0])
                .is_none_or(|input| input.value_type != "dataframe")
        {
            return Err(invalid(format!(
                "frame-write {} input contract differs",
                node.id
            )));
        }
        if rows == "mask"
            && self.node(&node.inputs[1]).is_none_or(|input| {
                !matches!(
                    input.value_type.as_str(),
                    "bool-scalar" | "bool-column" | "f64-column"
                )
            })
        {
            return Err(invalid(format!(
                "frame-write {} mask type differs",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_format_string(&self, node: &ProgramNode) -> Result<(), VectorCoreError> {
        let segments = node
            .parameters
            .get("segments")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid(format!("format-string {} has no segments", node.id)))?;
        if node.parameters.len() != 1
            || node.value_type != "string-scalar"
            || segments.len() != node.inputs.len() + 1
            || segments.iter().any(|segment| !segment.is_string())
            || node.inputs.iter().any(|input| {
                self.node(input).is_none_or(|value| {
                    !matches!(
                        value.value_type.as_str(),
                        "bool-scalar" | "int-scalar" | "string-scalar"
                    )
                })
            })
        {
            return Err(invalid(format!(
                "format-string {} contract is invalid",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_inventories(&self) -> Result<(), VectorCoreError> {
        sorted_unique(&self.required_input_columns, "required input columns")?;
        sorted_unique(&self.opcodes, "opcodes")?;
        let mutation_nodes = self
            .nodes
            .iter()
            .filter(|node| node.op == "frame-write")
            .map(|node| node.id.clone())
            .collect::<Vec<_>>();
        if self.mutation_nodes != mutation_nodes
            || self.opcodes
                != self
                    .nodes
                    .iter()
                    .map(|node| node.op.clone())
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect::<Vec<_>>()
        {
            return Err(invalid("mutation program inventories differ from nodes"));
        }
        Ok(())
    }

    fn validate_source_map(&self) -> Result<(), VectorCoreError> {
        if self.source_map.keys().collect::<BTreeSet<_>>()
            != self
                .nodes
                .iter()
                .map(|node| &node.id)
                .collect::<BTreeSet<_>>()
            || self.source_map.values().any(|location| {
                location.path != "strategy.py" || location.line == 0 || location.end_line == 0
            })
        {
            return Err(invalid("mutation source map differs from nodes"));
        }
        Ok(())
    }

    fn validate_lookback(&self) -> Result<(), VectorCoreError> {
        validate_lookback(&self.max_lookback, "program")?;
        if self.max_lookback != merge_lookbacks(self.nodes.iter().map(|node| &node.lookback)) {
            return Err(invalid("mutation aggregate lookback differs"));
        }
        Ok(())
    }

    fn validate_schema_contract(&self) -> Result<(), VectorCoreError> {
        if self.schema_version == SIGNAL_PROGRAM_VERSION {
            if self
                .contract
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>()
                != BTreeSet::from(["signal_outputs"])
            {
                return Err(invalid("signal mutation extension fields differ"));
            }
            let expected = self.expected_signal_outputs();
            if self.contract.get("signal_outputs") != Some(&Value::Array(expected)) {
                return Err(invalid("signal output inventory differs"));
            }
            return Ok(());
        }
        if self
            .contract
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            != BTreeSet::from(["route_contract", "tag_mutation_nodes", "tag_outputs"])
        {
            return Err(invalid("tag mutation extension fields differ"));
        }
        let tag_mutations = self
            .nodes
            .iter()
            .filter(|node| {
                node.op == "frame-write"
                    && node.parameters["columns"]
                        .as_array()
                        .is_some_and(|columns| {
                            columns
                                .iter()
                                .filter_map(Value::as_str)
                                .any(|column| TAG_COLUMNS.contains(&column))
                        })
            })
            .map(|node| Value::String(node.id.clone()))
            .collect::<Vec<_>>();
        if self.contract.get("tag_mutation_nodes") != Some(&Value::Array(tag_mutations))
            || self.contract.get("tag_outputs") != Some(&Value::Array(self.expected_tag_outputs()))
            || self.contract.get("route_contract")
                != Some(&json!({
                    "canonicalization":"python-str-split",
                    "original_storage":"preserve-exact",
                    "trailing_whitespace":"preserve"
                }))
        {
            return Err(invalid("tag output or route contract differs"));
        }
        Ok(())
    }

    fn expected_signal_outputs(&self) -> Vec<Value> {
        SIGNAL_COLUMNS
            .iter()
            .filter_map(|column| {
                self.final_write(column).map(|node| {
                    let entry = column.starts_with("enter_");
                    json!({
                        "column":column,
                        "phase":if entry {"entry"} else {"exit"},
                        "side":if column.ends_with("long") {"long"} else {"short"},
                        "final_mutation":node.id
                    })
                })
            })
            .collect()
    }

    fn expected_tag_outputs(&self) -> Vec<Value> {
        TAG_COLUMNS
            .iter()
            .map(|column| {
                json!({
                    "column":column,
                    "phase":if *column == "enter_tag" {"entry"} else {"exit"},
                    "wrapper_initializer":"",
                    "final_mutation":self.final_write(column).map(|node| node.id.clone())
                })
            })
            .collect()
    }

    fn final_write(&self, column: &str) -> Option<&ProgramNode> {
        self.nodes.iter().rev().find(|node| {
            node.op == "frame-write"
                && node.parameters["columns"]
                    .as_array()
                    .is_some_and(|columns| columns.iter().any(|value| value == column))
        })
    }

    fn validate_fingerprint(&self) -> Result<(), VectorCoreError> {
        let value = serde_json::to_value(self)
            .map_err(|error| invalid(format!("cannot encode mutation program: {error}")))?;
        let actual = crate::program::validation::canonical_fingerprint(&value)?;
        if actual != self.fingerprint {
            return Err(invalid("mutation fingerprint differs"));
        }
        Ok(())
    }
}

pub(crate) fn numpy_array_arguments_are_supported(
    name: &str,
    value_type: &str,
    arguments: &serde_json::Map<String, Value>,
) -> bool {
    match name {
        "full" => {
            arguments.is_empty()
                || (value_type == "string-column"
                    && arguments.len() == 1
                    && arguments.get("dtype").and_then(Value::as_str) == Some("object"))
        }
        "full_like" | "divide" | "isnan" => arguments.is_empty(),
        _ => false,
    }
}

fn required_string<'a>(
    parameters: &'a serde_json::Map<String, Value>,
    name: &str,
    node: &ProgramNode,
) -> Result<&'a str, VectorCoreError> {
    parameters
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("mutation node {} lacks {name}", node.id)))
}

fn known_opcode(value: &str) -> bool {
    matches!(
        value,
        "parameter"
            | "literal"
            | "row-count"
            | "string-split-index"
            | "column-read"
            | "metadata-read"
            | "frame-write"
            | "binary"
            | "compare"
            | "membership"
            | "masked-string-append"
            | "logical"
            | "unary"
            | "select"
            | "array-call"
            | "scalar-call"
            | "cast"
            | "shift"
            | "function-call"
            | "format-string"
            | "instrumentation"
            | "return"
    )
}

fn known_value_type(value: &str) -> bool {
    matches!(
        value,
        "dataframe"
            | "metadata"
            | "dynamic"
            | "null"
            | "bool-scalar"
            | "int-scalar"
            | "f64-scalar"
            | "string-scalar"
            | "json-scalar"
            | "bool-column"
            | "int-column"
            | "f64-column"
            | "string-column"
            | "timestamp-column"
    )
}

fn validate_lookback(lookback: &Lookback, context: &str) -> Result<(), VectorCoreError> {
    if !matches!(
        lookback.kind.as_str(),
        "finite" | "recursive" | "library-defined" | "function-defined" | "mixed"
    ) || !lookback.causal
        || lookback.expression.as_ref().is_some_and(String::is_empty)
    {
        return Err(invalid(format!("{context} lookback is invalid")));
    }
    Ok(())
}

fn merge_lookbacks<'a>(lookbacks: impl Iterator<Item = &'a Lookback>) -> Lookback {
    let values = lookbacks.collect::<Vec<_>>();
    if values
        .iter()
        .all(|lookback| lookback.kind == "finite" && lookback.candles.is_some())
    {
        return Lookback {
            kind: "finite".to_owned(),
            candles: values.iter().filter_map(|lookback| lookback.candles).max(),
            expression: None,
            causal: true,
        };
    }
    let kinds = values
        .iter()
        .map(|lookback| lookback.kind.as_str())
        .collect::<BTreeSet<_>>();
    Lookback {
        kind: if kinds.len() == 1 {
            (*kinds.first().unwrap_or(&"finite")).to_owned()
        } else {
            "mixed".to_owned()
        },
        candles: None,
        expression: Some(kinds.into_iter().collect::<Vec<_>>().join("+")),
        causal: values.iter().all(|lookback| lookback.causal),
    }
}

fn sorted_unique(values: &[String], description: &str) -> Result<(), VectorCoreError> {
    if values.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err(invalid(format!(
            "mutation {description} are not sorted and unique"
        )));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn invalid(message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::InvalidProgram(message.into())
}
