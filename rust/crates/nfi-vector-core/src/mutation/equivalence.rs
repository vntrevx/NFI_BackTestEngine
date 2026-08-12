//! Structural proof that Tag contains the exact Signal decision program.

use std::collections::{BTreeMap, BTreeSet};

use super::model::{MutationProgram, SIGNAL_PROGRAM_VERSION, TAG_PROGRAM_VERSION};
use crate::program::{ProgramFunction, ProgramNode, SourceLocation};
use crate::VectorCoreError;

const TAG_COLUMNS: [&str; 2] = ["enter_tag", "exit_tag"];

#[derive(Debug, PartialEq)]
struct DecisionProjection {
    functions: Vec<ProgramFunction>,
    nodes: Vec<ProgramNode>,
    source_locations: Vec<SourceLocation>,
}

/// Prove that removing Tag-only writes leaves the exact Signal DAG.
///
/// Node identifiers and source-order offsets are canonicalized after removing
/// tag formatting/appends. Every retained dependency edge, opcode, parameter,
/// lookback, function boundary, and source span must then match exactly.
///
/// # Errors
///
/// Returns fail-closed when the programs have different identities, inputs,
/// entrypoints, or projected decision graphs.
pub fn prove_signal_tag_decision_equivalence(
    signal: &MutationProgram,
    tag: &MutationProgram,
) -> Result<(), VectorCoreError> {
    if signal.schema_version != SIGNAL_PROGRAM_VERSION
        || tag.schema_version != TAG_PROGRAM_VERSION
        || signal.compile_context != tag.compile_context
        || signal.entrypoints != tag.entrypoints
        || signal.required_input_columns != tag.required_input_columns
    {
        return Err(invalid("Signal and Tag decision identities differ"));
    }
    if project(signal)? != project(tag)? {
        return Err(invalid(
            "Tag decision projection is not structurally exact to Signal",
        ));
    }
    Ok(())
}

fn project(program: &MutationProgram) -> Result<DecisionProjection, VectorCoreError> {
    let removed = program
        .nodes
        .iter()
        .filter(|node| is_tag_only(node))
        .map(|node| node.id.as_str())
        .collect::<BTreeSet<_>>();
    let nodes_by_id = program
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<BTreeMap<_, _>>();
    let live = live_decision_nodes(program, &removed, &nodes_by_id)?;
    let retained = program
        .nodes
        .iter()
        .filter(|node| live.contains(node.id.as_str()))
        .collect::<Vec<_>>();
    let canonical_ids = retained
        .iter()
        .enumerate()
        .map(|(index, node)| (node.id.as_str(), format!("n{}", index + 1)))
        .collect::<BTreeMap<_, _>>();
    let mut source_orders = BTreeMap::<String, u64>::new();
    let mut nodes = Vec::with_capacity(retained.len());
    let mut source_locations = Vec::with_capacity(retained.len());
    for original in retained {
        let mut node = original.clone();
        node.id.clone_from(&canonical_ids[original.id.as_str()]);
        node.inputs = original
            .inputs
            .iter()
            .map(|input| resolve_input(input, &removed, &canonical_ids, &nodes_by_id))
            .collect::<Result<Vec<_>, _>>()?;
        let source_order = source_orders.entry(node.function.clone()).or_default();
        node.source_order = *source_order;
        *source_order = source_order
            .checked_add(1)
            .ok_or_else(|| invalid("projected mutation source order is too large"))?;
        let mut location = program
            .source_map
            .get(&original.id)
            .cloned()
            .ok_or_else(|| invalid(format!("missing source location for {}", original.id)))?;
        location.path.clear();
        nodes.push(node);
        source_locations.push(location);
    }
    let functions = program
        .functions
        .iter()
        .map(|function| project_function(function, &removed, &live, &canonical_ids, &nodes_by_id))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(DecisionProjection {
        functions,
        nodes,
        source_locations,
    })
}

/// Find the observable decision graph after bypassing Tag-only dataframe writes.
///
/// Tag source may repeat a Signal condition solely to assign a string literal.
/// Removing only the write would leave that condition as an unreachable orphan
/// and make equivalent programs appear different. Start from every function's
/// return and ABI parameters, then walk dependencies backwards so the proof
/// compares exactly the executable decision graph.
fn live_decision_nodes<'a>(
    program: &'a MutationProgram,
    removed: &BTreeSet<&'a str>,
    nodes_by_id: &BTreeMap<&'a str, &'a ProgramNode>,
) -> Result<BTreeSet<&'a str>, VectorCoreError> {
    let mut pending = Vec::<&str>::new();
    for function in &program.functions {
        pending.push(function.return_node.as_str());
        pending.extend(
            function
                .parameters
                .iter()
                .map(|parameter| parameter.node.as_str()),
        );
    }
    let mut live = BTreeSet::new();
    while let Some(candidate) = pending.pop() {
        let resolved = resolve_original_input(candidate, removed, nodes_by_id)?;
        if !live.insert(resolved) {
            continue;
        }
        let node = nodes_by_id
            .get(resolved)
            .ok_or_else(|| invalid(format!("projected node {resolved} is missing")))?;
        pending.extend(node.inputs.iter().map(String::as_str));
    }
    Ok(live)
}

fn project_function(
    function: &ProgramFunction,
    removed: &BTreeSet<&str>,
    live: &BTreeSet<&str>,
    canonical_ids: &BTreeMap<&str, String>,
    nodes_by_id: &BTreeMap<&str, &ProgramNode>,
) -> Result<ProgramFunction, VectorCoreError> {
    let mut projected = function.clone();
    projected.node_ids = function
        .node_ids
        .iter()
        .filter(|id| live.contains(id.as_str()))
        .map(|id| canonical_ids[id.as_str()].clone())
        .collect();
    projected.return_node =
        resolve_input(&function.return_node, removed, canonical_ids, nodes_by_id)?;
    for parameter in &mut projected.parameters {
        parameter.node = canonical_ids
            .get(parameter.node.as_str())
            .cloned()
            .ok_or_else(|| invalid("Tag-only projection removed a function parameter"))?;
    }
    Ok(projected)
}

fn resolve_original_input<'a>(
    input: &'a str,
    removed: &BTreeSet<&str>,
    nodes_by_id: &BTreeMap<&'a str, &'a ProgramNode>,
) -> Result<&'a str, VectorCoreError> {
    let mut current = input;
    for _ in 0..=nodes_by_id.len() {
        if !removed.contains(current) {
            return Ok(current);
        }
        let node = nodes_by_id
            .get(current)
            .ok_or_else(|| invalid(format!("projected input {current} is missing")))?;
        if node.op != "frame-write" || !writes_tag(node) {
            return Err(invalid(format!(
                "retained decision depends on Tag-only node {current}"
            )));
        }
        current = node
            .inputs
            .first()
            .map(String::as_str)
            .ok_or_else(|| invalid("Tag frame-write has no dataframe predecessor"))?;
    }
    Err(invalid(format!(
        "cannot resolve projected mutation input {input}"
    )))
}

fn resolve_input(
    input: &str,
    removed: &BTreeSet<&str>,
    canonical_ids: &BTreeMap<&str, String>,
    nodes_by_id: &BTreeMap<&str, &ProgramNode>,
) -> Result<String, VectorCoreError> {
    let resolved = resolve_original_input(input, removed, nodes_by_id)?;
    canonical_ids
        .get(resolved)
        .cloned()
        .ok_or_else(|| invalid(format!("projected input {resolved} is not live")))
}

fn is_tag_only(node: &ProgramNode) -> bool {
    matches!(node.op.as_str(), "format-string" | "masked-string-append") || writes_tag(node)
}

fn writes_tag(node: &ProgramNode) -> bool {
    node.op == "frame-write"
        && node
            .parameters
            .get("columns")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|columns| {
                columns.iter().any(|column| {
                    column
                        .as_str()
                        .is_some_and(|name| TAG_COLUMNS.contains(&name))
                })
            })
}

fn invalid(message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::InvalidProgram(message.into())
}
