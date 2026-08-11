use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{
    invalid_program, IndicatorProgram, Lookback, ProgramFunction, ProgramNode,
    INDICATOR_PROGRAM_VERSION,
};
use crate::VectorCoreError;

impl IndicatorProgram {
    /// Deserialize and validate a complete `indicator-program-v1` document.
    ///
    /// # Errors
    ///
    /// Returns [`VectorCoreError::InvalidProgram`] when JSON cannot be decoded
    /// or the decoded document violates the indicator-program-v1 contract.
    pub fn from_json(input: &str) -> Result<Self, VectorCoreError> {
        let program: Self = serde_json::from_str(input)
            .map_err(|error| invalid_program(format!("invalid JSON program: {error}")))?;
        program.validate()?;
        Ok(program)
    }

    /// Validate schema-level constraints plus DAG and causal invariants.
    ///
    /// # Errors
    ///
    /// Returns [`VectorCoreError::InvalidProgram`] when the document violates
    /// the indicator-program-v1 structural or causal contract.
    pub fn validate(&self) -> Result<(), VectorCoreError> {
        self.validate_document_metadata()?;
        let positions = self.validate_nodes()?;
        let functions = self.validate_function_declarations()?;
        self.validate_function_ownership(&positions)?;
        self.validate_node_operations(&functions)?;
        self.validate_source_map()?;
        self.validate_inventories()?;
        self.validate_program_lookback()?;
        self.validate_fingerprint()
    }

    fn validate_document_metadata(&self) -> Result<(), VectorCoreError> {
        if self.schema_version != INDICATOR_PROGRAM_VERSION {
            return Err(invalid_program(format!(
                "unsupported schema_version: {}",
                self.schema_version
            )));
        }
        if self.source.path.is_empty() || self.selected_class.is_empty() {
            return Err(invalid_program(
                "source path and selected class must be non-empty",
            ));
        }
        if !is_sha256(&self.source.sha256) || !is_sha256(&self.fingerprint) {
            return Err(invalid_program(
                "source and fingerprint hashes must be lowercase SHA-256",
            ));
        }
        if self.functions.is_empty() || self.nodes.is_empty() {
            return Err(invalid_program("functions and nodes must be non-empty"));
        }
        Ok(())
    }

    fn validate_fingerprint(&self) -> Result<(), VectorCoreError> {
        let identity = serde_json::to_value(self)
            .map_err(|error| invalid_program(format!("cannot encode program identity: {error}")))?;
        let actual = canonical_fingerprint(&identity)?;
        if actual != self.fingerprint {
            return Err(invalid_program("fingerprint differs from program content"));
        }
        Ok(())
    }

    fn validate_inventories(&self) -> Result<(), VectorCoreError> {
        validate_sorted_unique(&self.required_input_columns, "required input columns")?;
        validate_sorted_unique(&self.produced_columns, "produced columns")?;
        validate_sorted_unique(&self.opcodes, "opcode inventory")?;
        let opcodes = self
            .nodes
            .iter()
            .map(|node| node.op.clone())
            .collect::<BTreeSet<_>>();
        if self.opcodes != opcodes.into_iter().collect::<Vec<_>>() {
            return Err(invalid_program("opcode inventory differs from nodes"));
        }
        let informative = self
            .nodes
            .iter()
            .filter(|node| node.op == "informative-merge")
            .map(|node| node.id.clone())
            .collect::<Vec<_>>();
        if self.informative_nodes != informative || has_duplicates(&self.informative_nodes) {
            return Err(invalid_program(
                "informative node inventory differs from nodes",
            ));
        }
        for column in &self.produced_columns {
            if self.producer_for(column).is_none() {
                return Err(invalid_program(format!(
                    "produced column has no column-write node: {column}"
                )));
            }
        }
        Ok(())
    }

    fn validate_nodes(&self) -> Result<HashMap<&str, usize>, VectorCoreError> {
        for (index, node) in self.nodes.iter().enumerate() {
            if node.id != format!("n{}", index + 1) {
                return Err(invalid_program("node IDs are not canonical"));
            }
            if !is_known_opcode(&node.op) {
                return Err(invalid_program(format!(
                    "unknown opcode at {}: {}",
                    node.id, node.op
                )));
            }
            if !is_known_value_type(&node.value_type) {
                return Err(invalid_program(format!(
                    "unknown value type at {}: {}",
                    node.id, node.value_type
                )));
            }
            validate_lookback(&node.lookback, &format!("node {}", node.id))?;
        }
        let positions = self
            .nodes
            .iter()
            .enumerate()
            .map(|(index, node)| (node.id.as_str(), index))
            .collect::<HashMap<_, _>>();
        for (index, node) in self.nodes.iter().enumerate() {
            for input in &node.inputs {
                if positions
                    .get(input.as_str())
                    .is_none_or(|input_index| *input_index >= index)
                {
                    return Err(invalid_program(format!(
                        "node {} has a non-prior input {}",
                        node.id, input
                    )));
                }
            }
        }
        Ok(positions)
    }

    fn validate_function_declarations(
        &self,
    ) -> Result<HashMap<&str, &ProgramFunction>, VectorCoreError> {
        let mut functions = HashMap::with_capacity(self.functions.len());
        for (index, function) in self.functions.iter().enumerate() {
            if function.id != format!("f{}", index + 1) {
                return Err(invalid_program("function IDs are not canonical"));
            }
            if function.source_name.is_empty()
                || !matches!(function.kind.as_str(), "entrypoint" | "helper")
            {
                return Err(invalid_program(format!(
                    "invalid function declaration: {}",
                    function.id
                )));
            }
            if functions.insert(function.id.as_str(), function).is_some() {
                return Err(invalid_program(format!(
                    "duplicate function ID: {}",
                    function.id
                )));
            }
        }
        if !functions.contains_key(self.entrypoint.as_str()) {
            return Err(invalid_program("entrypoint is missing"));
        }
        Ok(functions)
    }

    fn validate_function_ownership(
        &self,
        positions: &HashMap<&str, usize>,
    ) -> Result<(), VectorCoreError> {
        let mut owned_nodes = HashSet::with_capacity(self.nodes.len());
        for function in &self.functions {
            if function.node_ids.is_empty() {
                return Err(invalid_program(format!(
                    "function {} owns no nodes",
                    function.id
                )));
            }
            self.validate_function_parameters(function, positions)?;
            for (source_order, node_id) in function.node_ids.iter().enumerate() {
                let source_order = u64::try_from(source_order).map_err(|_| {
                    invalid_program(format!(
                        "function {} has too many nodes to represent source order",
                        function.id
                    ))
                })?;
                let node = self.node_at(positions, node_id).ok_or_else(|| {
                    invalid_program(format!(
                        "function {} owns an unknown node {}",
                        function.id, node_id
                    ))
                })?;
                if node.function != function.id || node.source_order != source_order {
                    return Err(invalid_program(format!(
                        "function {} node ownership differs at {}",
                        function.id, node_id
                    )));
                }
                if !owned_nodes.insert(node_id.as_str()) {
                    return Err(invalid_program(format!(
                        "node {node_id} has multiple function owners"
                    )));
                }
            }
            if !function.node_ids.contains(&function.return_node) {
                return Err(invalid_program(format!(
                    "function {} return node is external",
                    function.id
                )));
            }
        }
        if owned_nodes.len() != self.nodes.len() {
            return Err(invalid_program("function node ownership is incomplete"));
        }
        Ok(())
    }

    fn validate_function_parameters(
        &self,
        function: &ProgramFunction,
        positions: &HashMap<&str, usize>,
    ) -> Result<(), VectorCoreError> {
        let mut parameter_names = HashSet::new();
        for parameter in &function.parameters {
            if parameter.name.is_empty()
                || !is_known_value_type(&parameter.value_type)
                || !parameter_names.insert(parameter.name.as_str())
            {
                return Err(invalid_program(format!(
                    "invalid parameter declaration in function {}",
                    function.id
                )));
            }
            let node = self.node_at(positions, &parameter.node).ok_or_else(|| {
                invalid_program(format!(
                    "function {} parameter {} references an unknown node",
                    function.id, parameter.name
                ))
            })?;
            if node.function != function.id
                || node.op != "parameter"
                || node.value_type != parameter.value_type
                || node.parameters.get("name").and_then(Value::as_str)
                    != Some(parameter.name.as_str())
            {
                return Err(invalid_program(format!(
                    "function {} parameter {} does not match its node",
                    function.id, parameter.name
                )));
            }
        }
        Ok(())
    }

    fn validate_node_operations(
        &self,
        functions: &HashMap<&str, &ProgramFunction>,
    ) -> Result<(), VectorCoreError> {
        for node in &self.nodes {
            if !functions.contains_key(node.function.as_str()) {
                return Err(invalid_program(format!(
                    "node {} has an unknown function",
                    node.id
                )));
            }
            if node.op == "function-call" {
                Self::validate_function_call(node, functions)?;
            }
            if matches!(node.op.as_str(), "column-read" | "column-write")
                && node
                    .parameters
                    .get("column")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                return Err(invalid_program(format!(
                    "{} node {} has no column parameter",
                    node.op, node.id
                )));
            }
            if node.op == "column-write" && node.inputs.len() < 2 {
                return Err(invalid_program(format!(
                    "column-write node {} lacks a dataframe and value input",
                    node.id
                )));
            }
        }
        Ok(())
    }

    fn validate_function_call(
        node: &ProgramNode,
        functions: &HashMap<&str, &ProgramFunction>,
    ) -> Result<(), VectorCoreError> {
        let callee = node
            .parameters
            .get("function")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                invalid_program(format!(
                    "function-call node {} has no function parameter",
                    node.id
                ))
            })?;
        let callee = functions.get(callee).ok_or_else(|| {
            invalid_program(format!(
                "function-call node {} references unknown function",
                node.id
            ))
        })?;
        if node.inputs.len() != callee.parameters.len() {
            return Err(invalid_program(format!(
                "function-call node {} has the wrong arity",
                node.id
            )));
        }
        Ok(())
    }

    fn validate_source_map(&self) -> Result<(), VectorCoreError> {
        let expected: BTreeSet<&str> = self.nodes.iter().map(|node| node.id.as_str()).collect();
        let actual: BTreeSet<&str> = self.source_map.keys().map(String::as_str).collect();
        if actual != expected {
            return Err(invalid_program("source map does not cover every node"));
        }
        if self.source_map.values().any(|location| {
            location.path != "strategy.py" || location.line == 0 || location.end_line == 0
        }) {
            return Err(invalid_program("source map contains an invalid location"));
        }
        Ok(())
    }

    fn validate_program_lookback(&self) -> Result<(), VectorCoreError> {
        validate_lookback(&self.max_lookback, "program")?;
        if self.max_lookback != merge_lookbacks(self.nodes.iter().map(|node| &node.lookback)) {
            return Err(invalid_program("aggregate lookback differs from nodes"));
        }
        Ok(())
    }
}

pub(crate) fn canonical_fingerprint(program: &Value) -> Result<String, VectorCoreError> {
    let mut identity = program.clone();
    let identity = identity
        .as_object_mut()
        .ok_or_else(|| invalid_program("program identity is not an object"))?;
    identity.remove("fingerprint");
    identity
        .get_mut("source")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| invalid_program("program source identity is not an object"))?
        .remove("path");
    let encoded = serde_json::to_vec(identity)
        .map_err(|error| invalid_program(format!("cannot serialize program identity: {error}")))?;
    Ok(format!("{:x}", Sha256::digest(encoded)))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn is_known_opcode(value: &str) -> bool {
    matches!(
        value,
        "parameter"
            | "literal"
            | "column-read"
            | "metadata-read"
            | "column-write"
            | "binary"
            | "compare"
            | "logical"
            | "unary"
            | "select"
            | "indicator-call"
            | "array-call"
            | "scalar-call"
            | "cast"
            | "window"
            | "shift"
            | "fill"
            | "informative-merge"
            | "function-call"
            | "instrumentation"
            | "return"
    )
}

fn is_known_value_type(value: &str) -> bool {
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
    ) {
        return Err(invalid_program(format!(
            "{context} has an unknown lookback kind"
        )));
    }
    if lookback.expression.as_ref().is_some_and(String::is_empty) {
        return Err(invalid_program(format!(
            "{context} has an empty lookback expression"
        )));
    }
    if !lookback.causal {
        return Err(invalid_program(format!(
            "{context} has a non-causal lookback"
        )));
    }
    Ok(())
}

fn validate_sorted_unique(values: &[String], description: &str) -> Result<(), VectorCoreError> {
    if values.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err(invalid_program(format!(
            "{description} are not sorted and unique"
        )));
    }
    Ok(())
}

fn has_duplicates(values: &[String]) -> bool {
    values.iter().collect::<HashSet<_>>().len() != values.len()
}

fn merge_lookbacks<'a>(lookbacks: impl Iterator<Item = &'a Lookback>) -> Lookback {
    let lookbacks = lookbacks.collect::<Vec<_>>();
    if lookbacks.is_empty() {
        return Lookback {
            kind: "finite".to_owned(),
            candles: Some(0),
            expression: None,
            causal: true,
        };
    }
    let causal = lookbacks.iter().all(|lookback| lookback.causal);
    if lookbacks
        .iter()
        .all(|lookback| lookback.kind == "finite" && lookback.candles.is_some())
    {
        return Lookback {
            kind: "finite".to_owned(),
            candles: lookbacks
                .iter()
                .filter_map(|lookback| lookback.candles)
                .max(),
            expression: None,
            causal,
        };
    }
    let kinds = lookbacks
        .iter()
        .map(|lookback| lookback.kind.as_str())
        .collect::<BTreeSet<_>>();
    let kind = if kinds.len() == 1 {
        (*kinds.first().expect("non-empty lookbacks")).to_owned()
    } else {
        "mixed".to_owned()
    };
    Lookback {
        kind,
        candles: None,
        expression: Some(kinds.into_iter().collect::<Vec<_>>().join("+")),
        causal,
    }
}
