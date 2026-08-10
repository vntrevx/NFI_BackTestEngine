use std::collections::{BTreeMap, HashMap};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The only indicator program format understood by this crate.
pub const INDICATOR_PROGRAM_VERSION: &str = "indicator-program-v1";

/// A validated indicator program emitted by the Python static compiler.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct IndicatorProgram {
    pub schema_version: String,
    pub source: ProgramSource,
    pub selected_class: String,
    pub entrypoint: String,
    pub functions: Vec<ProgramFunction>,
    pub nodes: Vec<ProgramNode>,
    pub required_input_columns: Vec<String>,
    pub produced_columns: Vec<String>,
    pub informative_nodes: Vec<String>,
    pub opcodes: Vec<String>,
    pub max_lookback: Lookback,
    pub source_map: BTreeMap<String, SourceLocation>,
    pub fingerprint: String,
}

/// Immutable source identity embedded in an indicator program.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProgramSource {
    pub path: String,
    pub sha256: String,
}

/// One compiled Python method.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProgramFunction {
    pub id: String,
    pub source_name: String,
    pub kind: String,
    pub parameters: Vec<FunctionParameter>,
    pub node_ids: Vec<String>,
    pub return_node: String,
}

/// A compiled method argument and its backing parameter node.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FunctionParameter {
    pub name: String,
    pub node: String,
    pub value_type: String,
}

/// A generic DAG node. Opcode-specific data remains lossless JSON.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProgramNode {
    pub id: String,
    pub function: String,
    pub source_order: u64,
    pub op: String,
    pub value_type: String,
    pub inputs: Vec<String>,
    pub parameters: Map<String, Value>,
    pub lookback: Lookback,
}

/// Causal history required by a node or complete program.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Lookback {
    pub kind: String,
    pub candles: Option<u64>,
    pub expression: Option<String>,
    pub causal: bool,
}

/// Original Python source coordinates for one program node.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceLocation {
    pub path: String,
    pub line: u64,
    pub column: u64,
    pub end_line: u64,
    pub end_column: u64,
}

/// The deterministic, output-specific subset required by the vector engine.
#[derive(Clone, Debug)]
pub struct ExecutionPlan<'a> {
    /// Requested final dataframe columns, in caller order.
    pub requested_outputs: Vec<String>,
    /// Input dataframe columns needed by the reachable subgraph.
    pub required_input_columns: Vec<String>,
    /// Reachable nodes in original program order.
    pub nodes: Vec<&'a ProgramNode>,
}

impl IndicatorProgram {
    pub(super) fn producer_for(&self, column: &str) -> Option<&ProgramNode> {
        self.nodes.iter().rev().find(|node| {
            node.op == "column-write"
                && node.parameters.get("column").and_then(Value::as_str) == Some(column)
        })
    }

    pub(super) fn producer_before(&self, node_id: &str, column: &str) -> Option<&ProgramNode> {
        let position = self.nodes.iter().position(|node| node.id == node_id)?;
        self.nodes[..position].iter().rev().find(|node| {
            node.op == "column-write"
                && node.parameters.get("column").and_then(Value::as_str) == Some(column)
        })
    }

    pub(super) fn node_at<'a>(
        &'a self,
        positions: &HashMap<&str, usize>,
        node_id: &str,
    ) -> Option<&'a ProgramNode> {
        positions
            .get(node_id)
            .and_then(|position| self.nodes.get(*position))
    }
}
