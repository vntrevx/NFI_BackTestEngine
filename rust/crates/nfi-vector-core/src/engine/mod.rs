//! Batch-local execution of the safe generic indicator-program substrate.

mod operations;
mod runtime;

#[cfg(test)]
mod tests;

use std::collections::BTreeMap;

use serde_json::Value;

use self::operations::column_type;
use self::runtime::{NodeValue, RuntimeColumn};
use crate::batch::{BatchView, ColumnRequest};
use crate::error::VectorCoreError;
use crate::kernels::{RollingStream, TalibStream};
use crate::program::{ExecutionPlan, IndicatorProgram, ProgramNode};
use crate::sink::{BatchSink, OutputBatch};
use crate::state::ShiftState;

/// Bounded live-value accounting across a streaming execution.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct EngineProfile {
    pub batches: usize,
    pub rows: usize,
    pub peak_projected_columns: usize,
    pub peak_intermediate_columns: usize,
    pub peak_output_columns: usize,
    pub peak_live_values: usize,
    pub retained_state_values: usize,
    pub peak_state_values: usize,
}

/// A program-bound executor whose only cross-batch memory is explicit state.
#[derive(Debug)]
pub struct VectorEngine<'program> {
    program: &'program IndicatorProgram,
    plan: ExecutionPlan<'program>,
    input_requests: Vec<ColumnRequest>,
    shift_states: BTreeMap<String, ShiftState>,
    indicator_states: BTreeMap<String, TalibStream>,
    rolling_states: BTreeMap<String, RollingStream>,
    profile: EngineProfile,
}

impl<'program> VectorEngine<'program> {
    /// Compile an output-specific execution plan without inspecting input data.
    ///
    /// # Errors
    ///
    /// Returns a program or output error when the causal plan cannot be built.
    pub fn new(
        program: &'program IndicatorProgram,
        requested_outputs: &[String],
    ) -> Result<Self, VectorCoreError> {
        let plan = program.execution_plan(requested_outputs)?;
        let mut input_requests = Vec::with_capacity(plan.required_input_columns.len());
        for column in &plan.required_input_columns {
            let mut required_type = None;
            for node in &plan.nodes {
                if node.op == "column-read"
                    && node.parameters.get("column").and_then(Value::as_str)
                        == Some(column.as_str())
                {
                    let value_type = column_type(&node.value_type).ok_or_else(|| {
                        VectorCoreError::InvalidProgram(format!(
                            "input column {} has non-column value type {}",
                            column, node.value_type
                        ))
                    })?;
                    if required_type.is_some_and(|current| current != value_type) {
                        return Err(VectorCoreError::InvalidProgram(format!(
                            "input column {column} is read with inconsistent types"
                        )));
                    }
                    required_type = Some(value_type);
                }
            }
            input_requests.push(ColumnRequest::new(
                column,
                required_type.ok_or_else(|| {
                    VectorCoreError::InvalidProgram(format!(
                        "execution plan has no read for input column {column}"
                    ))
                })?,
            ));
        }
        Ok(Self {
            program,
            plan,
            input_requests,
            shift_states: BTreeMap::new(),
            indicator_states: BTreeMap::new(),
            rolling_states: BTreeMap::new(),
            profile: EngineProfile::default(),
        })
    }

    #[must_use]
    pub fn input_requests(&self) -> &[ColumnRequest] {
        &self.input_requests
    }

    #[must_use]
    pub const fn profile(&self) -> EngineProfile {
        self.profile
    }

    /// Execute one projected record batch and return only requested final columns.
    ///
    /// # Errors
    ///
    /// Returns a missing-input, invalid-node, state, or unsupported-opcode error
    /// without producing a partial final batch.
    pub fn execute_batch(&mut self, batch: &BatchView<'_>) -> Result<OutputBatch, VectorCoreError> {
        for request in &self.input_requests {
            if batch.column(&request.name).is_none() {
                return Err(VectorCoreError::MissingColumn(request.name.clone()));
            }
        }
        let (values, written_columns) = self.execute_nodes(batch)?;
        let output_columns = self.collect_outputs(&values, &written_columns, batch.len())?;
        let intermediate_columns = values
            .values()
            .filter(|value| matches!(value, NodeValue::Column(RuntimeColumn::Owned(_))))
            .count();
        self.update_profile(batch, intermediate_columns, output_columns.len());
        OutputBatch::new(batch.len(), output_columns)
    }

    fn update_profile(
        &mut self,
        batch: &BatchView<'_>,
        intermediate_columns: usize,
        final_columns: usize,
    ) {
        let retained_state_values = self
            .shift_states
            .values()
            .map(|state| state.profile().retained)
            .sum::<usize>()
            .saturating_add(
                self.indicator_states
                    .values()
                    .map(TalibStream::retained)
                    .sum::<usize>(),
            )
            .saturating_add(
                self.rolling_states
                    .values()
                    .map(RollingStream::retained)
                    .sum::<usize>(),
            );
        let peak_state_values = self
            .shift_states
            .values()
            .map(|state| state.profile().peak)
            .sum::<usize>()
            .saturating_add(
                self.indicator_states
                    .values()
                    .map(TalibStream::retained)
                    .sum::<usize>(),
            )
            .saturating_add(
                self.rolling_states
                    .values()
                    .map(RollingStream::retained)
                    .sum::<usize>(),
            );
        let projected_columns = batch.profile().projected_columns;
        let batch_live_values = batch.len().saturating_mul(
            projected_columns
                .saturating_add(intermediate_columns)
                .saturating_add(final_columns),
        );
        self.profile.batches = self.profile.batches.saturating_add(1);
        self.profile.rows = self.profile.rows.saturating_add(batch.len());
        self.profile.peak_projected_columns =
            self.profile.peak_projected_columns.max(projected_columns);
        self.profile.peak_intermediate_columns = self
            .profile
            .peak_intermediate_columns
            .max(intermediate_columns);
        self.profile.peak_output_columns = self.profile.peak_output_columns.max(final_columns);
        self.profile.retained_state_values = retained_state_values;
        self.profile.peak_state_values = self.profile.peak_state_values.max(peak_state_values);
        self.profile.peak_live_values = self
            .profile
            .peak_live_values
            .max(batch_live_values.saturating_add(retained_state_values));
    }

    /// Execute and immediately transfer final buffers to a caller-owned sink.
    ///
    /// # Errors
    ///
    /// Returns any execution error or the sink's output error.
    pub fn execute_to_sink(
        &mut self,
        batch: &BatchView<'_>,
        sink: &mut impl BatchSink,
    ) -> Result<(), VectorCoreError> {
        sink.consume(self.execute_batch(batch)?)
    }

    fn unsupported(&self, node: &ProgramNode) -> VectorCoreError {
        let location = self.program.source_map.get(&node.id).map_or_else(
            || "strategy.py:?:?".to_owned(),
            |source| format!("{}:{}:{}", source.path, source.line, source.column),
        );
        VectorCoreError::UnsupportedOpcode {
            opcode: node.op.clone(),
            location,
        }
    }
}
