use std::collections::{HashMap, HashSet};

use serde_json::Value;

use super::{invalid_program, ExecutionPlan, IndicatorProgram, ProgramFunction, ProgramNode};
use crate::VectorCoreError;

impl IndicatorProgram {
    /// Build a deterministic reverse-reachability plan for final dataframe columns.
    ///
    /// # Errors
    ///
    /// Returns [`VectorCoreError::InvalidProgram`] for an invalid program and
    /// [`VectorCoreError::MissingOutput`] for an output it does not produce.
    pub fn execution_plan<'a>(
        &'a self,
        requested_outputs: &[String],
    ) -> Result<ExecutionPlan<'a>, VectorCoreError> {
        self.validate()?;
        let reachable = self.reachable_node_ids(requested_outputs)?;
        let required_input_columns = self.reachable_input_columns(&reachable);
        Ok(ExecutionPlan {
            requested_outputs: requested_outputs.to_vec(),
            required_input_columns,
            nodes: self
                .nodes
                .iter()
                .filter(|node| reachable.contains(node.id.as_str()))
                .collect(),
        })
    }

    fn reachable_node_ids<'a>(
        &'a self,
        requested_outputs: &[String],
    ) -> Result<HashSet<&'a str>, VectorCoreError> {
        let functions: HashMap<&str, &ProgramFunction> = self
            .functions
            .iter()
            .map(|function| (function.id.as_str(), function))
            .collect();
        let nodes: HashMap<&str, &ProgramNode> = self
            .nodes
            .iter()
            .map(|node| (node.id.as_str(), node))
            .collect();
        let mut reachable = HashSet::new();
        let mut pending = self.requested_producers(requested_outputs)?;
        while let Some(node_id) = pending.pop() {
            if !reachable.insert(node_id) {
                continue;
            }
            let node = nodes
                .get(node_id)
                .ok_or_else(|| invalid_program(format!("reachable node is absent: {node_id}")))?;
            self.enqueue_dependencies(node, &functions, &mut pending)?;
        }
        Ok(reachable)
    }

    fn requested_producers<'a>(
        &'a self,
        requested_outputs: &[String],
    ) -> Result<Vec<&'a str>, VectorCoreError> {
        let mut pending = Vec::with_capacity(requested_outputs.len());
        for output in requested_outputs {
            if !self.produced_columns.iter().any(|column| column == output) {
                return Err(VectorCoreError::MissingOutput(output.clone()));
            }
            let producer = self
                .producer_for(output)
                .ok_or_else(|| invalid_program(format!("produced column has no node: {output}")))?;
            pending.push(producer.id.as_str());
        }
        Ok(pending)
    }

    fn enqueue_dependencies<'a>(
        &'a self,
        node: &'a ProgramNode,
        functions: &HashMap<&'a str, &'a ProgramFunction>,
        pending: &mut Vec<&'a str>,
    ) -> Result<(), VectorCoreError> {
        match node.op.as_str() {
            "column-write" => {
                let value_input = node.inputs.last().ok_or_else(|| {
                    invalid_program(format!("column-write node {} has no value input", node.id))
                })?;
                pending.push(value_input);
            }
            "column-read" => self.enqueue_read_dependency(node, pending)?,
            "function-call" => Self::enqueue_call_dependencies(node, functions, pending)?,
            _ => pending.extend(node.inputs.iter().map(String::as_str)),
        }
        Ok(())
    }

    fn enqueue_read_dependency<'a>(
        &'a self,
        node: &'a ProgramNode,
        pending: &mut Vec<&'a str>,
    ) -> Result<(), VectorCoreError> {
        let column = node
            .parameters
            .get("column")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                invalid_program(format!(
                    "column-read node {} has no column parameter",
                    node.id
                ))
            })?;
        if self
            .produced_columns
            .iter()
            .any(|produced| produced == column)
        {
            let producer = self.producer_before(&node.id, column).ok_or_else(|| {
                invalid_program(format!(
                    "column-read node {} has no prior producer for {column}",
                    node.id
                ))
            })?;
            pending.push(producer.id.as_str());
        }
        Ok(())
    }

    fn enqueue_call_dependencies<'a>(
        node: &'a ProgramNode,
        functions: &HashMap<&'a str, &'a ProgramFunction>,
        pending: &mut Vec<&'a str>,
    ) -> Result<(), VectorCoreError> {
        pending.extend(node.inputs.iter().map(String::as_str));
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
        let function = functions.get(callee).ok_or_else(|| {
            invalid_program(format!(
                "function-call node {} references unknown function",
                node.id
            ))
        })?;
        pending.push(function.return_node.as_str());
        Ok(())
    }

    fn reachable_input_columns(&self, reachable: &HashSet<&str>) -> Vec<String> {
        self.required_input_columns
            .iter()
            .filter(|column| {
                self.nodes.iter().any(|node| {
                    reachable.contains(node.id.as_str())
                        && node.op == "column-read"
                        && node.parameters.get("column").and_then(Value::as_str)
                            == Some(column.as_str())
                })
            })
            .cloned()
            .collect()
    }
}
