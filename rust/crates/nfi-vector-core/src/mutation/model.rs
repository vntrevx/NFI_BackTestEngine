use std::collections::{BTreeMap, HashMap};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::program::{Lookback, ProgramFunction, ProgramNode, ProgramSource, SourceLocation};

pub const SIGNAL_PROGRAM_VERSION: &str = "signal-program-v1";
pub const TAG_PROGRAM_VERSION: &str = "tag-program-v1";

/// Compile-time runtime identity sealed by a mutation program.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MutationCompileContext {
    pub run_mode: String,
    pub trading_mode: String,
}

/// One source-ordered entry or exit phase.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MutationEntrypoint {
    pub phase: String,
    pub function: String,
}

/// Lossless shared representation of the Signal and Tag contracts.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct MutationProgram {
    pub schema_version: String,
    pub source: ProgramSource,
    pub selected_class: String,
    pub compile_context: MutationCompileContext,
    pub entrypoints: Vec<MutationEntrypoint>,
    pub functions: Vec<ProgramFunction>,
    pub nodes: Vec<ProgramNode>,
    pub required_input_columns: Vec<String>,
    pub mutation_nodes: Vec<String>,
    pub opcodes: Vec<String>,
    pub max_lookback: Lookback,
    pub source_map: BTreeMap<String, SourceLocation>,
    pub fingerprint: String,
    #[serde(flatten)]
    pub contract: BTreeMap<String, Value>,
}

impl MutationProgram {
    #[must_use]
    pub fn is_tag_program(&self) -> bool {
        self.schema_version == TAG_PROGRAM_VERSION
    }

    pub(crate) fn node_positions(&self) -> HashMap<&str, usize> {
        self.nodes
            .iter()
            .enumerate()
            .map(|(index, node)| (node.id.as_str(), index))
            .collect()
    }

    pub(crate) fn node(&self, id: &str) -> Option<&ProgramNode> {
        id.strip_prefix('n')
            .and_then(|value| value.parse::<usize>().ok())
            .and_then(|index| index.checked_sub(1))
            .and_then(|index| self.nodes.get(index))
            .filter(|node| node.id == id)
    }

    pub(crate) fn function(&self, id: &str) -> Option<&ProgramFunction> {
        id.strip_prefix('f')
            .and_then(|value| value.parse::<usize>().ok())
            .and_then(|index| index.checked_sub(1))
            .and_then(|index| self.functions.get(index))
            .filter(|function| function.id == id)
    }
}
