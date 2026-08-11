//! Per-batch runtime values and stateful plan execution.

use std::collections::BTreeMap;

use crate::batch::BatchView;
use crate::column::{ColumnView, OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::kernels::{rolling_stream, TalibStream};
use crate::program::ProgramNode;

use super::operations::{
    collect_numeric, execute_binary, execute_compare, execute_logical, execute_select,
    execute_unary, literal_value, node_error, numeric_at, single_input, string_parameter,
    to_owned_column, unsigned_parameter,
};
use super::VectorEngine;

#[derive(Debug)]
pub(super) enum RuntimeColumn<'batch> {
    Borrowed(ColumnView<'batch>),
    Owned(OwnedColumn),
}

impl RuntimeColumn<'_> {
    pub(super) fn value_type(&self) -> ValueType {
        match self {
            Self::Borrowed(column) => column.value_type(),
            Self::Owned(column) => column.as_view().value_type(),
        }
    }

    pub(super) fn f64_at(&self, row: usize) -> Option<f64> {
        match self {
            Self::Borrowed(column) => column.f64_at(row),
            Self::Owned(column) => column.as_view().f64_at(row),
        }
    }

    pub(super) fn bool_at(&self, row: usize) -> Option<bool> {
        match self {
            Self::Borrowed(column) => column.bool_at(row),
            Self::Owned(column) => column.as_view().bool_at(row),
        }
    }

    pub(super) fn i64_at(&self, row: usize) -> Option<i64> {
        match self {
            Self::Borrowed(column) => column.i64_at(row),
            Self::Owned(column) => column.as_view().i64_at(row),
        }
    }

    pub(super) fn text_at(&self, row: usize) -> Option<&str> {
        match self {
            Self::Borrowed(column) => column.text_at(row),
            Self::Owned(column) => column.as_view().text_at(row),
        }
    }

    pub(super) fn timestamp_ms_at(&self, row: usize) -> Option<i64> {
        match self {
            Self::Borrowed(column) => column.timestamp_ms_at(row),
            Self::Owned(column) => column.as_view().timestamp_ms_at(row),
        }
    }
}

#[derive(Debug)]
pub(super) enum NodeValue<'batch> {
    DataFrame,
    Metadata,
    Unbound,
    Null,
    Bool(bool),
    Integer(i64),
    Float(f64),
    Text(String),
    Json,
    Column(RuntimeColumn<'batch>),
    Alias(String),
}

pub(super) type RuntimeBatch<'batch> = (
    BTreeMap<String, NodeValue<'batch>>,
    BTreeMap<String, String>,
);

impl VectorEngine<'_> {
    pub(super) fn execute_nodes<'batch>(
        &mut self,
        batch: &BatchView<'batch>,
    ) -> Result<RuntimeBatch<'batch>, VectorCoreError> {
        let nodes = self.plan.nodes.clone();
        let mut values = BTreeMap::new();
        let mut written_columns = BTreeMap::new();
        for node in nodes {
            let value = self.execute_node(node, batch, &values, &mut written_columns)?;
            values.insert(node.id.clone(), value);
        }
        Ok((values, written_columns))
    }

    fn execute_node<'batch>(
        &mut self,
        node: &ProgramNode,
        batch: &BatchView<'batch>,
        values: &BTreeMap<String, NodeValue<'batch>>,
        written_columns: &mut BTreeMap<String, String>,
    ) -> Result<NodeValue<'batch>, VectorCoreError> {
        match node.op.as_str() {
            "parameter" => Ok(parameter_value(node)),
            "literal" => literal_value(node),
            "column-read" => {
                let column = string_parameter(node, "column")?;
                written_columns.get(column).map_or_else(
                    || {
                        batch
                            .column(column)
                            .map(|column| NodeValue::Column(RuntimeColumn::Borrowed(column)))
                            .ok_or_else(|| VectorCoreError::MissingColumn(column.to_owned()))
                    },
                    |value_node| Ok(NodeValue::Alias(value_node.clone())),
                )
            }
            "column-write" => {
                let column = string_parameter(node, "column")?;
                let value_node = node.inputs.get(1).ok_or_else(|| {
                    node_error(node, "column-write requires dataframe and value inputs")
                })?;
                super::operations::resolve_value(values, value_node)?;
                written_columns.insert(column.to_owned(), value_node.clone());
                Ok(NodeValue::DataFrame)
            }
            "cast" | "return" => Ok(NodeValue::Alias(single_input(node)?.to_owned())),
            "shift" => self.execute_shift(node, values, batch.len()),
            "binary" => execute_binary(node, values, batch.len()),
            "compare" => execute_compare(node, values, batch.len()),
            "logical" => execute_logical(node, values, batch.len()),
            "unary" => execute_unary(node, values, batch.len()),
            "select" => execute_select(node, values, batch.len()),
            "indicator-call" => self.execute_indicator(node, values, batch.len()),
            "window" => self.execute_window(node, values, batch.len()),
            _ => Err(self.unsupported(node)),
        }
    }

    fn execute_shift<'batch>(
        &mut self,
        node: &ProgramNode,
        values: &BTreeMap<String, NodeValue<'batch>>,
        rows: usize,
    ) -> Result<NodeValue<'batch>, VectorCoreError> {
        let input = single_input(node)?;
        let periods = unsigned_parameter(node, "periods")?;
        if periods == 0 {
            return Err(node_error(node, "shift periods must be positive"));
        }
        let source = collect_numeric(values, input, rows)?;
        let state = self
            .shift_states
            .entry(node.id.clone())
            .or_insert(super::ShiftState::new(periods)?);
        if state.lag() != periods {
            return Err(node_error(
                node,
                "shift state bound changed during execution",
            ));
        }
        let output = source
            .into_iter()
            .map(|value| {
                let ready = state.len() == periods;
                let shifted = state.push(value);
                if ready {
                    shifted
                } else {
                    Some(crate::float::canonicalize(f64::NAN))
                }
            })
            .collect();
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            output,
        ))))
    }

    fn execute_indicator<'batch>(
        &mut self,
        node: &ProgramNode,
        values: &BTreeMap<String, NodeValue<'batch>>,
        rows: usize,
    ) -> Result<NodeValue<'batch>, VectorCoreError> {
        let family = string_parameter(node, "family")?;
        if !matches!(family, "ta" | "talib") {
            return Err(self.unsupported(node));
        }
        let name = string_parameter(node, "name")?;
        let arguments = node
            .parameters
            .get("arguments")
            .and_then(serde_json::Value::as_object)
            .ok_or_else(|| node_error(node, "indicator-call arguments must be an object"))?;
        let owned_inputs = numeric_inputs(node, values, rows)?;
        let input_slices = owned_inputs.iter().map(Vec::as_slice).collect::<Vec<_>>();
        let state = match self.indicator_states.entry(node.id.clone()) {
            std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(TalibStream::new(name, arguments)?)
            }
        };
        let output = state.execute(&input_slices)?;
        let selected = select_indicator_output(node, &output)?;
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            selected.iter().copied().map(Some).collect(),
        ))))
    }

    fn execute_window<'batch>(
        &mut self,
        node: &ProgramNode,
        values: &BTreeMap<String, NodeValue<'batch>>,
        rows: usize,
    ) -> Result<NodeValue<'batch>, VectorCoreError> {
        if string_parameter(node, "kind")? != "rolling" {
            return Err(self.unsupported(node));
        }
        let reducer = string_parameter(node, "reducer")?;
        let input = single_input(node)?;
        let column = (0..rows)
            .map(|row| {
                numeric_at(values, input, row)?
                    .ok_or_else(|| node_error(node, "pandas rolling input contains an Arrow null"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let state = match self.rolling_states.entry(node.id.clone()) {
            std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(rolling_stream(reducer, &node.parameters)?)
            }
        };
        Ok(NodeValue::Column(RuntimeColumn::Owned(OwnedColumn::f64(
            state.execute(&column).into_iter().map(Some).collect(),
        ))))
    }

    pub(super) fn collect_outputs(
        &self,
        values: &BTreeMap<String, NodeValue<'_>>,
        written_columns: &BTreeMap<String, String>,
        rows: usize,
    ) -> Result<BTreeMap<String, OwnedColumn>, VectorCoreError> {
        self.plan
            .requested_outputs
            .iter()
            .map(|output| {
                let value_node =
                    written_columns
                        .get(output)
                        .ok_or_else(|| VectorCoreError::Execution {
                            node: output.clone(),
                            message: "requested output was not written in this plan".to_owned(),
                        })?;
                Ok((
                    output.clone(),
                    to_owned_column(super::operations::resolve_value(values, value_node)?, rows)?,
                ))
            })
            .collect()
    }
}

fn numeric_inputs(
    node: &ProgramNode,
    values: &BTreeMap<String, NodeValue<'_>>,
    rows: usize,
) -> Result<Vec<Vec<f64>>, VectorCoreError> {
    node.inputs
        .iter()
        .map(|input| {
            (0..rows)
                .map(|row| {
                    numeric_at(values, input, row)?
                        .ok_or_else(|| node_error(node, "TA-Lib input contains an Arrow null"))
                })
                .collect()
        })
        .collect()
}

fn select_indicator_output<'output>(
    node: &ProgramNode,
    output: &'output crate::kernels::KernelOutput,
) -> Result<&'output [f64], VectorCoreError> {
    if let Some(name) = node
        .parameters
        .get("output")
        .and_then(serde_json::Value::as_str)
    {
        return output
            .column(name)
            .ok_or_else(|| node_error(node, format!("indicator has no output named {name}")));
    }
    if output.columns().len() == 1 {
        Ok(&output.columns()[0])
    } else {
        Err(node_error(
            node,
            "multi-output indicator requires an explicit output name",
        ))
    }
}

fn parameter_value(node: &ProgramNode) -> NodeValue<'static> {
    match node.value_type.as_str() {
        "dataframe" => NodeValue::DataFrame,
        "metadata" => NodeValue::Metadata,
        _ => NodeValue::Unbound,
    }
}
