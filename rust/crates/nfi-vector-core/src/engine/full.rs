//! Recursive, identity-aware execution of complete indicator programs.
//!
//! The streaming [`super::VectorEngine`] intentionally executes an output-specific
//! flat plan over one Arrow batch.  NFI's complete indicator entrypoint is a
//! different contract: helpers receive dynamic dataframe/column arguments, read
//! informative frames with independent row counts, and return mutated frames to
//! their caller.  This module executes that contract without Python while reusing
//! the same exact scalar, array, indicator, rolling, and alignment kernels.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};

use super::array::{execute_array_call, ArrayCallState};
use super::frame::{node_source, FrameRuntime, RuntimeFrame};
use super::operations::{
    execute_binary, execute_compare, execute_logical, execute_select, execute_unary, literal_value,
    resolve_value, single_input, string_parameter, to_owned_column, unsigned_parameter,
};
use super::runtime::{NodeValue, RuntimeColumn};
use crate::alignment::{FrameCatalog, FrameIdentity, NumericFrame, SourceLocation};
use crate::column::{OwnedColumn, ValueType};
use crate::error::VectorCoreError;
use crate::kernels::{
    rolling_stream, ChaikinMoneyFlowStream, LegacyChaikinMoneyFlowStream, RollingStream,
    SafePercentChangeStream, TalibStream,
};
use crate::program::{IndicatorProgram, ProgramFunction, ProgramNode};
use crate::state::ShiftState;

const MAX_CALL_DEPTH: usize = 64;

type StatelessOperation = for<'batch> fn(
    &ProgramNode,
    &BTreeMap<String, NodeValue<'batch>>,
    usize,
) -> Result<NodeValue<'batch>, VectorCoreError>;

/// A typed dataframe returned by complete indicator-program execution.
#[derive(Clone, Debug)]
pub struct FullFrameOutput {
    identity: FrameIdentity,
    timestamps_ms: Vec<i64>,
    columns: BTreeMap<String, OwnedColumn>,
}

impl FullFrameOutput {
    /// Exact pair/timeframe identity of the returned dataframe.
    #[must_use]
    pub const fn identity(&self) -> &FrameIdentity {
        &self.identity
    }

    /// Ordered candle timestamps retained by the returned dataframe.
    #[must_use]
    pub fn timestamps_ms(&self) -> &[i64] {
        &self.timestamps_ms
    }

    /// Requested typed columns in deterministic name order.
    #[must_use]
    pub const fn columns(&self) -> &BTreeMap<String, OwnedColumn> {
        &self.columns
    }

    /// Number of rows in every returned column.
    #[must_use]
    pub fn len(&self) -> usize {
        self.timestamps_ms.len()
    }

    /// Whether the returned dataframe has no rows.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.timestamps_ms.is_empty()
    }
}

/// Program-bound executor for a complete, recursively-called indicator entrypoint.
///
/// Stateful kernels are keyed by the complete call-site path.  Therefore two
/// invocations of the same helper over different dataframe identities cannot
/// share shift, rolling, TA, or native-kernel history accidentally.
#[derive(Debug)]
pub struct FullIndicatorEngine<'program> {
    program: &'program IndicatorProgram,
    functions: BTreeMap<String, &'program ProgramFunction>,
    nodes: BTreeMap<String, &'program ProgramNode>,
    shift_states: BTreeMap<String, ShiftState>,
    indicator_states: BTreeMap<String, TalibStream>,
    rolling_states: BTreeMap<String, RollingStream>,
    array_states: BTreeMap<String, ArrayCallState>,
    chaikin_states: BTreeMap<String, (usize, ChaikinMoneyFlowStream)>,
    legacy_chaikin_states: BTreeMap<String, (usize, LegacyChaikinMoneyFlowStream)>,
    percent_change_states: BTreeMap<String, SafePercentChangeStream>,
}

impl<'program> FullIndicatorEngine<'program> {
    /// Validate and bind one complete indicator program.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error when the serialized contract is not
    /// canonical or its entrypoint cannot be resolved.
    pub fn new(program: &'program IndicatorProgram) -> Result<Self, VectorCoreError> {
        program.validate()?;
        let functions = program
            .functions
            .iter()
            .map(|function| (function.id.clone(), function))
            .collect::<BTreeMap<_, _>>();
        if !functions.contains_key(&program.entrypoint) {
            return Err(VectorCoreError::InvalidProgram(
                "complete indicator entrypoint is absent".to_owned(),
            ));
        }
        let nodes = program
            .nodes
            .iter()
            .map(|node| (node.id.clone(), node))
            .collect();
        Ok(Self {
            program,
            functions,
            nodes,
            shift_states: BTreeMap::new(),
            indicator_states: BTreeMap::new(),
            rolling_states: BTreeMap::new(),
            array_states: BTreeMap::new(),
            chaikin_states: BTreeMap::new(),
            legacy_chaikin_states: BTreeMap::new(),
            percent_change_states: BTreeMap::new(),
        })
    }

    /// Execute the program entrypoint against one base frame and immutable
    /// informative-frame catalog.
    ///
    /// `metadata` is intentionally string-only: it is the exact static compiler
    /// contract used for pair routing.  Only `requested_outputs` are materialized
    /// in the public result, while every dataframe mutation still executes in
    /// source order.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for missing frames/columns, row-shape drift,
    /// unsupported exact semantics, or a runtime type mismatch.
    pub fn execute<'catalog>(
        &mut self,
        base: &'catalog NumericFrame,
        catalog: &'catalog FrameCatalog,
        metadata: &'catalog BTreeMap<String, String>,
        requested_outputs: &[String],
    ) -> Result<FullFrameOutput, VectorCoreError> {
        base.validate()?;
        self.clear_execution_state();
        let mut seen = BTreeSet::new();
        for output in requested_outputs {
            if output.is_empty() || !seen.insert(output.as_str()) {
                return Err(VectorCoreError::InvalidOutput(
                    "requested complete-frame outputs must be non-empty and unique".to_owned(),
                ));
            }
        }
        let runtime = FrameRuntime::new(catalog, metadata);
        let entrypoint = self.program.entrypoint.clone();
        let result = self.execute_function(
            &runtime,
            &entrypoint,
            vec![
                BoundValue::Frame(RuntimeFrame::borrowed(base)),
                BoundValue::Metadata,
            ],
            &entrypoint,
            0,
        )?;
        let BoundValue::Frame(frame) = result else {
            return Err(VectorCoreError::InvalidOutput(
                "complete indicator entrypoint did not return a dataframe".to_owned(),
            ));
        };
        materialize_output(&frame, requested_outputs)
    }

    fn clear_execution_state(&mut self) {
        self.shift_states.clear();
        self.indicator_states.clear();
        self.rolling_states.clear();
        self.array_states.clear();
        self.chaikin_states.clear();
        self.legacy_chaikin_states.clear();
        self.percent_change_states.clear();
    }

    fn execute_function<'catalog>(
        &mut self,
        runtime: &FrameRuntime<'catalog>,
        function_id: &str,
        arguments: Vec<BoundValue<'catalog>>,
        call_path: &str,
        depth: usize,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        if depth > MAX_CALL_DEPTH {
            return Err(VectorCoreError::InvalidState(format!(
                "indicator helper call depth exceeds {MAX_CALL_DEPTH} at {call_path}"
            )));
        }
        let function = self.function(function_id)?;
        if arguments.len() != function.parameters.len() {
            return Err(VectorCoreError::InvalidProgram(format!(
                "function {} received {} arguments; expected {}",
                function.id,
                arguments.len(),
                function.parameters.len()
            )));
        }
        let mut scope = FunctionScope::default();
        let mut remaining_uses = self.function_input_uses(function)?;
        for (parameter, argument) in function.parameters.iter().zip(arguments) {
            if !argument.matches_type(&parameter.value_type) {
                return Err(VectorCoreError::InvalidProgram(format!(
                    "function {} parameter {} cannot bind runtime type {} as {}",
                    function.id,
                    parameter.name,
                    argument.type_name(),
                    parameter.value_type
                )));
            }
            scope.insert(parameter.node.clone(), argument);
        }

        for node_id in &function.node_ids {
            let node = self.node(node_id)?;
            if node.op == "parameter" {
                if !scope.contains(&node.id) {
                    return Err(VectorCoreError::InvalidProgram(format!(
                        "function {} has an unbound parameter node {}",
                        function.id, node.id
                    )));
                }
                continue;
            }
            let value = self.execute_node(runtime, node, &scope, call_path, depth)?;
            if !value.matches_type(&node.value_type) {
                let source = node_source(self.program, node)?;
                return Err(source.error(format!(
                    "opcode {} returned runtime type {}; expected {}",
                    node.op,
                    value.type_name(),
                    node.value_type
                )));
            }
            scope.insert(node.id.clone(), value);
            scope.release_consumed_inputs(
                &node.inputs,
                &mut remaining_uses,
                &function.return_node,
            )?;
        }
        scope.bound(&function.return_node)
    }

    fn execute_node<'catalog>(
        &mut self,
        runtime: &FrameRuntime<'catalog>,
        node: &ProgramNode,
        scope: &FunctionScope<'catalog>,
        call_path: &str,
        depth: usize,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        let source = node_source(self.program, node)?;
        if is_frame_opcode(&node.op) {
            return execute_frame_opcode(runtime, node, scope, &source);
        }
        match node.op.as_str() {
            "literal" => literal_value(node)
                .map(BoundValue::Runtime)
                .map_err(|error| located(&source, error)),
            "function-call" => {
                let callee =
                    string_parameter(node, "function").map_err(|error| located(&source, error))?;
                let arguments = node
                    .inputs
                    .iter()
                    .map(|input| scope.bound(input))
                    .collect::<Result<Vec<_>, _>>()?;
                let nested_path = format!("{call_path}/{}:{callee}", node.id);
                self.execute_function(runtime, callee, arguments, &nested_path, depth + 1)
            }
            "return" => scope.bound(single_input(node).map_err(|error| located(&source, error))?),
            "cast" => execute_cast(node, scope, &source),
            "shift" => self.execute_shift(node, scope, call_path, &source),
            "binary" => Self::execute_stateless(node, scope, &source, execute_binary),
            "compare" => Self::execute_stateless(node, scope, &source, execute_compare),
            "logical" => Self::execute_stateless(node, scope, &source, execute_logical),
            "unary" => Self::execute_stateless(node, scope, &source, execute_unary),
            "select" => Self::execute_stateless(node, scope, &source, execute_select),
            "indicator-call" => self.execute_indicator(node, scope, call_path, &source),
            "window" => self.execute_window(node, scope, call_path, &source),
            "array-call" => {
                let rows = scope.rows_for(node, &source)?;
                let state = self.array_states.entry(call_path.to_owned()).or_default();
                execute_array_call(
                    node,
                    &scope.values,
                    rows,
                    state,
                    self.program.source_map.get(&node.id),
                )
                .map(BoundValue::Runtime)
            }
            "instrumentation" => execute_instrumentation(node, &source),
            other => Err(VectorCoreError::UnsupportedOpcode {
                opcode: other.to_owned(),
                location: format!("{}:{}:{}", source.path, source.line, source.column),
            }),
        }
    }

    fn execute_stateless<'catalog>(
        node: &ProgramNode,
        scope: &FunctionScope<'catalog>,
        source: &SourceLocation,
        operation: StatelessOperation,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        let rows = scope.rows_for(node, source)?;
        operation(node, &scope.values, rows)
            .map(BoundValue::Runtime)
            .map_err(|error| located(source, error))
    }

    fn execute_shift<'catalog>(
        &mut self,
        node: &ProgramNode,
        scope: &FunctionScope<'catalog>,
        call_path: &str,
        source: &SourceLocation,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        let input = single_input(node).map_err(|error| located(source, error))?;
        let periods =
            unsigned_parameter(node, "periods").map_err(|error| located(source, error))?;
        if periods == 0 {
            return Err(source.error("shift periods must be positive"));
        }
        let rows = scope.rows_for(node, source)?;
        let input = scope.numeric_input(input, source)?;
        let key = state_key(call_path, &node.id);
        let state = self
            .shift_states
            .entry(key)
            .or_insert(ShiftState::new(periods)?);
        if state.lag() != periods {
            return Err(source.error("shift state period changed between executions"));
        }
        let output = (0..rows)
            .map(|row| input.at(row))
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
        Ok(BoundValue::Runtime(NodeValue::Column(
            RuntimeColumn::Owned(OwnedColumn::f64(output)),
        )))
    }

    fn execute_indicator<'catalog>(
        &mut self,
        node: &ProgramNode,
        scope: &FunctionScope<'catalog>,
        call_path: &str,
        source: &SourceLocation,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        let family = string_parameter(node, "family").map_err(|error| located(source, error))?;
        let name = string_parameter(node, "name").map_err(|error| located(source, error))?;
        let arguments = node
            .parameters
            .get("arguments")
            .and_then(Value::as_object)
            .ok_or_else(|| source.error("indicator-call arguments must be an object"))?;
        let rows = scope.rows_for(node, source)?;
        let inputs = scope.present_numeric_inputs(node, rows, source)?;
        let slices = inputs
            .iter()
            .map(PresentNumeric::as_slice)
            .collect::<Vec<_>>();
        let key = state_key(call_path, &node.id);
        let output = match (family, name) {
            ("ta" | "talib", _) => {
                let state = match self.indicator_states.entry(key) {
                    std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
                    std::collections::btree_map::Entry::Vacant(entry) => entry.insert(
                        TalibStream::new(name, arguments)
                            .map_err(|error| located(source, error))?,
                    ),
                };
                let output = state
                    .execute(&slices)
                    .map_err(|error| located(source, error))?;
                select_indicator_output(node, &output, source)?.to_vec()
            }
            ("native", "safe-percent-change") if arguments.is_empty() && slices.len() == 1 => self
                .percent_change_states
                .entry(key)
                .or_default()
                .execute(slices[0]),
            ("native", "chaikin-money-flow") if slices.len() == 4 => {
                let period = bounded_argument(arguments, "timeperiod", source)?;
                let state = match self.chaikin_states.entry(key) {
                    std::collections::btree_map::Entry::Occupied(entry) => {
                        if entry.get().0 != period {
                            return Err(source
                                .error("Chaikin money-flow period changed between executions"));
                        }
                        &mut entry.into_mut().1
                    }
                    std::collections::btree_map::Entry::Vacant(entry) => {
                        &mut entry
                            .insert((
                                period,
                                ChaikinMoneyFlowStream::new(period)
                                    .map_err(|error| located(source, error))?,
                            ))
                            .1
                    }
                };
                state
                    .execute(slices[0], slices[1], slices[2], slices[3])
                    .map_err(|error| located(source, error))?
            }
            ("native", "chaikin-money-flow-legacy") if slices.len() == 4 => {
                let period = bounded_argument(arguments, "timeperiod", source)?;
                let state = match self.legacy_chaikin_states.entry(key) {
                    std::collections::btree_map::Entry::Occupied(entry) => {
                        if entry.get().0 != period {
                            return Err(source.error(
                                "legacy Chaikin money-flow period changed between executions",
                            ));
                        }
                        &mut entry.into_mut().1
                    }
                    std::collections::btree_map::Entry::Vacant(entry) => {
                        &mut entry
                            .insert((
                                period,
                                LegacyChaikinMoneyFlowStream::new(period)
                                    .map_err(|error| located(source, error))?,
                            ))
                            .1
                    }
                };
                state
                    .execute(slices[0], slices[1], slices[2], slices[3])
                    .map_err(|error| located(source, error))?
            }
            _ => {
                return Err(VectorCoreError::UnsupportedOpcode {
                    opcode: node.op.clone(),
                    location: format!("{}:{}:{}", source.path, source.line, source.column),
                });
            }
        };
        Ok(BoundValue::Runtime(NodeValue::Column(
            RuntimeColumn::Owned(OwnedColumn::f64(output.into_iter().map(Some).collect())),
        )))
    }

    fn execute_window<'catalog>(
        &mut self,
        node: &ProgramNode,
        scope: &FunctionScope<'catalog>,
        call_path: &str,
        source: &SourceLocation,
    ) -> Result<BoundValue<'catalog>, VectorCoreError> {
        if node.parameters.get("kind").and_then(Value::as_str) != Some("rolling") {
            return Err(VectorCoreError::UnsupportedOpcode {
                opcode: node.op.clone(),
                location: format!("{}:{}:{}", source.path, source.line, source.column),
            });
        }
        let reducer = string_parameter(node, "reducer").map_err(|error| located(source, error))?;
        let input = single_input(node).map_err(|error| located(source, error))?;
        let rows = scope.rows_for(node, source)?;
        let values = scope.present_numeric_input(
            input,
            rows,
            "rolling input contains an Arrow null",
            source,
        )?;
        let key = state_key(call_path, &node.id);
        let state = match self.rolling_states.entry(key) {
            std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
            std::collections::btree_map::Entry::Vacant(entry) => entry.insert(
                rolling_stream(reducer, &node.parameters)
                    .map_err(|error| located(source, error))?,
            ),
        };
        Ok(BoundValue::Runtime(NodeValue::Column(
            RuntimeColumn::Owned(OwnedColumn::f64(
                state
                    .execute(values.as_slice())
                    .into_iter()
                    .map(Some)
                    .collect(),
            )),
        )))
    }

    fn function(&self, id: &str) -> Result<&'program ProgramFunction, VectorCoreError> {
        self.functions.get(id).copied().ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!("unknown complete function {id}"))
        })
    }

    fn function_input_uses(
        &self,
        function: &ProgramFunction,
    ) -> Result<BTreeMap<String, usize>, VectorCoreError> {
        let mut uses = function
            .node_ids
            .iter()
            .map(|id| (id.clone(), 0_usize))
            .collect::<BTreeMap<_, _>>();
        for node_id in &function.node_ids {
            for input in &self.node(node_id)?.inputs {
                let count = uses.get_mut(input).ok_or_else(|| {
                    VectorCoreError::InvalidProgram(format!(
                        "function {} node {node_id} references non-local input {input}",
                        function.id
                    ))
                })?;
                *count = count.checked_add(1).ok_or_else(|| {
                    VectorCoreError::InvalidProgram(format!(
                        "function {} input use count is too large",
                        function.id
                    ))
                })?;
            }
        }
        Ok(uses)
    }

    fn node(&self, id: &str) -> Result<&'program ProgramNode, VectorCoreError> {
        self.nodes
            .get(id)
            .copied()
            .ok_or_else(|| VectorCoreError::InvalidProgram(format!("unknown complete node {id}")))
    }
}

#[derive(Debug)]
enum BoundValue<'catalog> {
    Frame(RuntimeFrame<'catalog>),
    Metadata,
    Runtime(NodeValue<'catalog>),
}

impl BoundValue<'_> {
    fn type_name(&self) -> &'static str {
        match self {
            Self::Frame(_) => "dataframe",
            Self::Metadata => "metadata",
            Self::Runtime(NodeValue::Null) => "null",
            Self::Runtime(NodeValue::Bool(_)) => "bool-scalar",
            Self::Runtime(NodeValue::Integer(_)) => "int-scalar",
            Self::Runtime(NodeValue::Float(_)) => "f64-scalar",
            Self::Runtime(NodeValue::Text(_)) => "string-scalar",
            Self::Runtime(NodeValue::Column(column)) => match column.value_type() {
                ValueType::F64 => "f64-column",
                ValueType::I64 => "int-column",
                ValueType::Bool => "bool-column",
                ValueType::Text => "string-column",
                ValueType::TimestampMs => "timestamp-column",
            },
            Self::Runtime(NodeValue::Json) => "json",
            Self::Runtime(NodeValue::DataFrame) => "dataframe-marker",
            Self::Runtime(NodeValue::Metadata) => "metadata-marker",
            Self::Runtime(NodeValue::Unbound) => "unbound",
            Self::Runtime(NodeValue::Alias(_)) => "alias",
        }
    }

    fn matches_type(&self, expected: &str) -> bool {
        expected == "dynamic"
            && matches!(
                self,
                Self::Runtime(
                    NodeValue::Null
                        | NodeValue::Bool(_)
                        | NodeValue::Integer(_)
                        | NodeValue::Float(_)
                        | NodeValue::Text(_)
                        | NodeValue::Column(_)
                        | NodeValue::Json
                )
            )
            || self.type_name() == expected
    }
}

#[derive(Debug, Default)]
struct FunctionScope<'catalog> {
    values: BTreeMap<String, NodeValue<'catalog>>,
    frames: BTreeMap<String, RuntimeFrame<'catalog>>,
    metadata: BTreeSet<String>,
}

impl<'catalog> FunctionScope<'catalog> {
    fn insert(&mut self, id: String, value: BoundValue<'catalog>) {
        match value {
            BoundValue::Frame(frame) => {
                self.frames.insert(id.clone(), frame);
                self.values.insert(id, NodeValue::DataFrame);
            }
            BoundValue::Metadata => {
                self.metadata.insert(id.clone());
                self.values.insert(id, NodeValue::Metadata);
            }
            BoundValue::Runtime(value) => {
                self.values.insert(id, value);
            }
        }
    }

    fn contains(&self, id: &str) -> bool {
        self.values.contains_key(id)
    }

    fn release_consumed_inputs(
        &mut self,
        inputs: &[String],
        remaining_uses: &mut BTreeMap<String, usize>,
        return_node: &str,
    ) -> Result<(), VectorCoreError> {
        for input in inputs {
            let remaining = remaining_uses.get_mut(input).ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!(
                    "function input {input} has no liveness record"
                ))
            })?;
            *remaining = remaining.checked_sub(1).ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!(
                    "function input {input} was consumed too many times"
                ))
            })?;
            if *remaining == 0 && input != return_node {
                self.values.remove(input);
                self.frames.remove(input);
                self.metadata.remove(input);
            }
        }
        Ok(())
    }

    fn runtime(&self, id: &str) -> Result<&NodeValue<'catalog>, VectorCoreError> {
        resolve_value(&self.values, id)
    }

    fn row_count(&self) -> Result<usize, VectorCoreError> {
        self.frames
            .values()
            .next()
            .map(RuntimeFrame::len)
            .ok_or_else(|| VectorCoreError::Execution {
                node: "cast".to_owned(),
                message: "string-array cast has no dataframe row domain".to_owned(),
            })
    }

    fn frame(&self, id: &str) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let mut current = id;
        for _ in 0..=self.values.len() {
            if let Some(frame) = self.frames.get(current) {
                return Ok(frame.clone());
            }
            match self.values.get(current) {
                Some(NodeValue::Alias(next)) => current = next,
                Some(_) => break,
                None => {
                    return Err(VectorCoreError::Execution {
                        node: id.to_owned(),
                        message: format!("input node {current} has no runtime value"),
                    });
                }
            }
        }
        Err(VectorCoreError::Execution {
            node: id.to_owned(),
            message: "runtime value is not a dataframe".to_owned(),
        })
    }

    fn require_metadata(&self, id: &str, source: &SourceLocation) -> Result<(), VectorCoreError> {
        let mut current = id;
        for _ in 0..=self.values.len() {
            if self.metadata.contains(current) {
                return Ok(());
            }
            match self.values.get(current) {
                Some(NodeValue::Alias(next)) => current = next,
                Some(_) => break,
                None => return Err(source.error(format!("metadata input {current} is absent"))),
            }
        }
        Err(source.error("runtime value is not metadata"))
    }

    fn bound(&self, id: &str) -> Result<BoundValue<'catalog>, VectorCoreError> {
        if let Ok(frame) = self.frame(id) {
            return Ok(BoundValue::Frame(frame));
        }
        let mut current = id;
        for _ in 0..=self.values.len() {
            if self.metadata.contains(current) {
                return Ok(BoundValue::Metadata);
            }
            match self.values.get(current) {
                Some(NodeValue::Alias(next)) => current = next,
                Some(value) => return clone_runtime(value).map(BoundValue::Runtime),
                None => break,
            }
        }
        Err(VectorCoreError::Execution {
            node: id.to_owned(),
            message: "runtime value cannot be bound".to_owned(),
        })
    }

    fn single_frame(
        &self,
        node: &ProgramNode,
        source: &SourceLocation,
    ) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
        let input = node
            .inputs
            .first()
            .filter(|_| node.inputs.len() == 1)
            .ok_or_else(|| source.error(format!("{} requires one dataframe input", node.op)))?;
        self.frame(input).map_err(|error| located(source, error))
    }

    fn two_frames(
        &self,
        node: &ProgramNode,
        source: &SourceLocation,
    ) -> Result<[RuntimeFrame<'catalog>; 2], VectorCoreError> {
        let [left, right] = node.inputs.as_slice() else {
            return Err(source.error(format!("{} requires two dataframe inputs", node.op)));
        };
        Ok([
            self.frame(left).map_err(|error| located(source, error))?,
            self.frame(right).map_err(|error| located(source, error))?,
        ])
    }

    fn rows_for(
        &self,
        node: &ProgramNode,
        source: &SourceLocation,
    ) -> Result<usize, VectorCoreError> {
        let mut rows = None;
        for input in &node.inputs {
            if let Ok(frame) = self.frame(input) {
                merge_rows(&mut rows, frame.len(), node, source)?;
                continue;
            }
            if let Ok(NodeValue::Column(column)) = self.runtime(input) {
                merge_rows(&mut rows, runtime_column_len(column), node, source)?;
            }
        }
        if node.value_type.ends_with("-column") {
            rows.ok_or_else(|| source.error(format!("{} has no row-bearing input", node.op)))
        } else {
            Ok(rows.unwrap_or(1))
        }
    }

    fn numeric_input<'scope>(
        &'scope self,
        input: &str,
        source: &SourceLocation,
    ) -> Result<NumericInput<'scope, 'catalog>, VectorCoreError> {
        match self
            .runtime(input)
            .map_err(|error| located(source, error))?
        {
            NodeValue::Null => Ok(NumericInput::Null),
            NodeValue::Integer(value) => {
                #[allow(clippy::cast_precision_loss)]
                let value = *value as f64;
                Ok(NumericInput::Scalar(value))
            }
            NodeValue::Float(value) => Ok(NumericInput::Scalar(*value)),
            NodeValue::Column(column) if column.value_type() == ValueType::I64 => {
                Ok(NumericInput::I64(column))
            }
            NodeValue::Column(column) if column.value_type() == ValueType::F64 => {
                Ok(NumericInput::F64(column))
            }
            _ => Err(source.error(format!("node {input} is not numeric"))),
        }
    }

    fn present_numeric_input<'scope>(
        &'scope self,
        input: &str,
        rows: usize,
        null_error: &str,
        source: &SourceLocation,
    ) -> Result<PresentNumeric<'scope>, VectorCoreError> {
        let input = self.numeric_input(input, source)?;
        if let NumericInput::F64(column) = input {
            if let Some(values) = column.present_f64_slice() {
                if values.len() == rows {
                    return Ok(PresentNumeric::Borrowed(values));
                }
            }
        }
        let values = (0..rows)
            .map(|row| input.at(row).ok_or_else(|| source.error(null_error)))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(PresentNumeric::Owned(values))
    }

    fn present_numeric_inputs<'scope>(
        &'scope self,
        node: &ProgramNode,
        rows: usize,
        source: &SourceLocation,
    ) -> Result<Vec<PresentNumeric<'scope>>, VectorCoreError> {
        node.inputs
            .iter()
            .map(|input| {
                self.present_numeric_input(
                    input,
                    rows,
                    "indicator input contains an Arrow null",
                    source,
                )
            })
            .collect()
    }
}

#[derive(Clone, Copy, Debug)]
enum NumericInput<'scope, 'catalog> {
    Null,
    Scalar(f64),
    I64(&'scope RuntimeColumn<'catalog>),
    F64(&'scope RuntimeColumn<'catalog>),
}

impl NumericInput<'_, '_> {
    fn at(self, row: usize) -> Option<f64> {
        match self {
            Self::Null => None,
            Self::Scalar(value) => Some(value),
            Self::I64(column) =>
            {
                #[allow(clippy::cast_precision_loss)]
                column.i64_at(row).map(|value| value as f64)
            }
            Self::F64(column) => column.f64_at(row),
        }
    }
}

#[derive(Debug)]
enum PresentNumeric<'scope> {
    Borrowed(&'scope [f64]),
    Owned(Vec<f64>),
}

impl PresentNumeric<'_> {
    fn as_slice(&self) -> &[f64] {
        match self {
            Self::Borrowed(values) => values,
            Self::Owned(values) => values,
        }
    }
}

fn is_frame_opcode(opcode: &str) -> bool {
    matches!(
        opcode,
        "metadata-read"
            | "frame-source"
            | "frame-nonempty"
            | "frame-project"
            | "frame-drop-if-present"
            | "informative-merge"
            | "fill"
            | "column-read"
            | "column-write"
            | "row-index"
    )
}

fn execute_frame_opcode<'catalog>(
    runtime: &FrameRuntime<'catalog>,
    node: &ProgramNode,
    scope: &FunctionScope<'catalog>,
    source: &SourceLocation,
) -> Result<BoundValue<'catalog>, VectorCoreError> {
    match node.op.as_str() {
        "metadata-read" => {
            let input = single_input(node).map_err(|error| located(source, error))?;
            scope.require_metadata(input, source)?;
            runtime
                .metadata_read(node, source)
                .map(|value| BoundValue::Runtime(NodeValue::Text(value)))
        }
        "frame-source" => runtime.frame_source(node, source).map(BoundValue::Frame),
        "frame-nonempty" => {
            let frame = scope.single_frame(node, source)?;
            FrameRuntime::frame_nonempty(&frame, source).map(BoundValue::Frame)
        }
        "frame-project" => {
            let frame = scope.single_frame(node, source)?;
            FrameRuntime::frame_project(node, &frame, source).map(BoundValue::Frame)
        }
        "frame-drop-if-present" => {
            let frame = scope.single_frame(node, source)?;
            FrameRuntime::frame_drop_if_present(node, &frame, source).map(BoundValue::Frame)
        }
        "informative-merge" => {
            let [base, informative] = scope.two_frames(node, source)?;
            FrameRuntime::informative_merge(node, &base, &informative, source)
                .map(BoundValue::Frame)
        }
        "fill" => {
            let frame = scope.single_frame(node, source)?;
            forward_fill_frame(frame, node, source).map(BoundValue::Frame)
        }
        "column-read" => execute_column_read(node, scope, source),
        "column-write" => execute_column_write(node, scope, source),
        "row-index" => {
            let frame = scope.single_frame(node, source)?;
            let values = (0..frame.len())
                .map(|row| {
                    i64::try_from(row)
                        .map(Some)
                        .map_err(|_| source.error("dataframe row index exceeds i64 range"))
                })
                .collect::<Result<Vec<_>, _>>()?;
            Ok(BoundValue::Runtime(NodeValue::Column(
                RuntimeColumn::Owned(OwnedColumn::i64(values)),
            )))
        }
        _ => Err(VectorCoreError::InvalidState(format!(
            "non-frame opcode {} reached the frame dispatcher",
            node.op
        ))),
    }
}

fn execute_column_read<'catalog>(
    node: &ProgramNode,
    scope: &FunctionScope<'catalog>,
    source: &SourceLocation,
) -> Result<BoundValue<'catalog>, VectorCoreError> {
    let frame = scope.frame(
        node.inputs
            .first()
            .ok_or_else(|| source.error("column-read requires a dataframe input"))?,
    )?;
    let column = string_parameter(node, "column").map_err(|error| located(source, error))?;
    frame
        .owned_column(column)
        .map(|column| BoundValue::Runtime(NodeValue::Column(RuntimeColumn::Owned(column))))
        .ok_or_else(|| {
            source.error(format!(
                "frame {} {} has no visible column {column:?}",
                frame.identity().pair,
                frame.identity().timeframe.as_str()
            ))
        })
}

fn execute_column_write<'catalog>(
    node: &ProgramNode,
    scope: &FunctionScope<'catalog>,
    source: &SourceLocation,
) -> Result<BoundValue<'catalog>, VectorCoreError> {
    let dataframe = node
        .inputs
        .first()
        .ok_or_else(|| source.error("column-write requires a dataframe input"))?;
    let value = node
        .inputs
        .get(1)
        .ok_or_else(|| source.error("column-write requires a value input"))?;
    let frame = scope.frame(dataframe)?;
    let value = scope.runtime(value)?;
    let column = to_owned_column(value, frame.len()).map_err(|error| located(source, error))?;
    let name = string_parameter(node, "column").map_err(|error| located(source, error))?;
    let collision_reject = match node.parameters.get("collision") {
        None => false,
        Some(Value::String(value)) if value == "reject" => true,
        Some(_) => {
            return Err(source.error("column-write collision must be absent or exactly reject"));
        }
    };
    frame
        .with_column(name, column, collision_reject, source)
        .map(BoundValue::Frame)
}

fn execute_cast<'catalog>(
    node: &ProgramNode,
    scope: &FunctionScope<'catalog>,
    source: &SourceLocation,
) -> Result<BoundValue<'catalog>, VectorCoreError> {
    let input = single_input(node).map_err(|error| located(source, error))?;
    let target = string_parameter(node, "target").map_err(|error| located(source, error))?;
    if matches!(target, "array" | "series") {
        validate_identity_cast(node, target, source)?;
        return scope.bound(input);
    }
    if target == "string-array" && node.value_type == "string-column" {
        if node.parameters.len() != 1 {
            return Err(source.error("string-array cast parameters are not exact"));
        }
        let value = scope
            .runtime(input)
            .map_err(|error| located(source, error))?;
        let rows = scope.row_count()?;
        let output = (0..rows)
            .map(|row| string_cast_at(value, row, source))
            .collect::<Result<Vec<_>, _>>()?;
        return Ok(BoundValue::Runtime(NodeValue::Column(
            RuntimeColumn::Owned(OwnedColumn::text(output)),
        )));
    }
    if target != "float" || node.value_type != "f64-column" {
        return Err(VectorCoreError::UnsupportedOpcode {
            opcode: node.op.clone(),
            location: format!("{}:{}:{}", source.path, source.line, source.column),
        });
    }
    validate_float_cast_parameters(node, source)?;
    let value = scope
        .runtime(input)
        .map_err(|error| located(source, error))?;
    let NodeValue::Column(column) = value else {
        return Err(source.error("float cast input is not a column"));
    };
    let rows = runtime_column_len(column);
    let output = match column.value_type() {
        ValueType::F64 => (0..rows).map(|row| column.f64_at(row)).collect(),
        ValueType::I64 => (0..rows)
            .map(|row| {
                #[allow(clippy::cast_precision_loss)]
                column.i64_at(row).map(|value| value as f64)
            })
            .collect(),
        ValueType::Bool => (0..rows)
            .map(|row| column.bool_at(row).map(f64::from))
            .collect(),
        _ => return Err(source.error("float cast input has an unsupported physical type")),
    };
    Ok(BoundValue::Runtime(NodeValue::Column(
        RuntimeColumn::Owned(OwnedColumn::f64(output)),
    )))
}

fn string_cast_at(
    value: &NodeValue<'_>,
    row: usize,
    source: &SourceLocation,
) -> Result<Option<String>, VectorCoreError> {
    match value {
        NodeValue::Null => Ok(None),
        NodeValue::Bool(value) => Ok(Some(if *value { "True" } else { "False" }.to_owned())),
        NodeValue::Integer(value) => Ok(Some(value.to_string())),
        NodeValue::Float(value) => Ok(float_string(*value)),
        NodeValue::Text(value) => Ok(Some(value.clone())),
        NodeValue::Column(column) => match column.value_type() {
            ValueType::F64 => Ok(column.f64_at(row).and_then(float_string)),
            ValueType::I64 => Ok(column.i64_at(row).map(|value| value.to_string())),
            ValueType::Bool => Ok(column
                .bool_at(row)
                .map(|value| if value { "True" } else { "False" }.to_owned())),
            ValueType::Text => Ok(column.text_at(row).map(str::to_owned)),
            ValueType::TimestampMs => Err(source.error("timestamp cannot cast to string array")),
        },
        NodeValue::DataFrame
        | NodeValue::Metadata
        | NodeValue::Unbound
        | NodeValue::Json
        | NodeValue::Alias(_) => Err(source.error("value cannot cast to string array")),
    }
}

fn float_string(value: f64) -> Option<String> {
    if value.is_nan() {
        return None;
    }
    if value == f64::INFINITY {
        return Some("inf".to_owned());
    }
    if value == f64::NEG_INFINITY {
        return Some("-inf".to_owned());
    }
    let mut rendered = value.to_string();
    if value.fract() == 0.0 && !rendered.contains('e') && !rendered.contains('E') {
        rendered.push_str(".0");
    }
    Some(rendered)
}

fn validate_float_cast_parameters(
    node: &ProgramNode,
    source: &SourceLocation,
) -> Result<(), VectorCoreError> {
    match (node.parameters.len(), node.parameters.get("arguments")) {
        (1, None) | (2, Some(Value::Null)) => Ok(()),
        _ => Err(source.error("float cast parameters are not exact")),
    }
}

fn validate_identity_cast(
    node: &ProgramNode,
    target: &str,
    source: &SourceLocation,
) -> Result<(), VectorCoreError> {
    match (
        target,
        node.parameters.len(),
        node.parameters.get("arguments"),
    ) {
        ("series", 1, None) | ("series", 2, Some(Value::Null)) => Ok(()),
        ("array", 2, Some(Value::Object(arguments)))
            if arguments.is_empty()
                || (arguments.len() == 1
                    && arguments.get("copy").and_then(Value::as_bool) == Some(false)) =>
        {
            Ok(())
        }
        _ => Err(source.error(format!("{target} cast arguments are not exact"))),
    }
}

fn execute_instrumentation<'catalog>(
    node: &ProgramNode,
    source: &SourceLocation,
) -> Result<BoundValue<'catalog>, VectorCoreError> {
    if node.parameters.len() != 1 {
        return Err(source.error("instrumentation parameters are not exact"));
    }
    match (
        node.parameters.get("name").and_then(Value::as_str),
        node.value_type.as_str(),
    ) {
        (Some("time.perf_counter"), "f64-scalar") => Ok(BoundValue::Runtime(NodeValue::Float(0.0))),
        (Some("log.debug"), "null") => Ok(BoundValue::Runtime(NodeValue::Null)),
        _ => Err(VectorCoreError::UnsupportedOpcode {
            opcode: node.op.clone(),
            location: format!("{}:{}:{}", source.path, source.line, source.column),
        }),
    }
}

fn forward_fill_frame<'catalog>(
    mut frame: RuntimeFrame<'catalog>,
    node: &ProgramNode,
    source: &SourceLocation,
) -> Result<RuntimeFrame<'catalog>, VectorCoreError> {
    if node.parameters.get("direction").and_then(Value::as_str) != Some("forward")
        || node.parameters.len() != 1
    {
        return Err(source.error("fill supports only exact forward direction"));
    }
    let names = frame.column_names().map(str::to_owned).collect::<Vec<_>>();
    for name in names {
        let column = frame
            .owned_column(&name)
            .ok_or_else(|| source.error(format!("visible fill column {name:?} is absent")))?;
        let filled = forward_fill_column(&column);
        frame = frame.with_column(name, filled, false, source)?;
    }
    Ok(frame)
}

fn forward_fill_column(column: &OwnedColumn) -> OwnedColumn {
    let view = column.as_view();
    match view.value_type() {
        ValueType::F64 => {
            let mut last = None;
            OwnedColumn::f64(
                (0..view.len())
                    .map(|row| match view.f64_at(row) {
                        Some(value) if !value.is_nan() => {
                            last = Some(value);
                            Some(value)
                        }
                        Some(value) => last.or(Some(value)),
                        None => last,
                    })
                    .collect(),
            )
        }
        ValueType::I64 => {
            let mut last = None;
            OwnedColumn::i64(
                (0..view.len())
                    .map(|row| {
                        if let Some(value) = view.i64_at(row) {
                            last = Some(value);
                        }
                        last
                    })
                    .collect(),
            )
        }
        ValueType::Bool => {
            let mut last = None;
            OwnedColumn::boolean(
                (0..view.len())
                    .map(|row| {
                        if let Some(value) = view.bool_at(row) {
                            last = Some(value);
                        }
                        last
                    })
                    .collect(),
            )
        }
        ValueType::Text => {
            let mut last = None;
            OwnedColumn::text(
                (0..view.len())
                    .map(|row| {
                        if let Some(value) = view.text_at(row) {
                            last = Some(value.to_owned());
                        }
                        last.clone()
                    })
                    .collect(),
            )
        }
        ValueType::TimestampMs => {
            let mut last = None;
            OwnedColumn::timestamp_ms(
                (0..view.len())
                    .map(|row| {
                        if let Some(value) = view.timestamp_ms_at(row) {
                            last = Some(value);
                        }
                        last
                    })
                    .collect(),
            )
        }
    }
}

fn materialize_output(
    frame: &RuntimeFrame<'_>,
    requested_outputs: &[String],
) -> Result<FullFrameOutput, VectorCoreError> {
    let mut columns = BTreeMap::new();
    for name in requested_outputs {
        let column = frame
            .owned_column(name)
            .ok_or_else(|| VectorCoreError::MissingOutput(name.clone()))?;
        if column.len() != frame.len() {
            return Err(VectorCoreError::ColumnLength {
                column: name.clone(),
                actual: column.len(),
                expected: frame.len(),
            });
        }
        columns.insert(name.clone(), column);
    }
    let dates = frame
        .owned_column("date")
        .ok_or_else(|| VectorCoreError::MissingColumn("date".to_owned()))?;
    let dates = dates.as_view();
    let timestamps_ms = (0..frame.len())
        .map(|row| {
            dates.timestamp_ms_at(row).ok_or_else(|| {
                VectorCoreError::InvalidOutput(format!(
                    "returned dataframe date is null at row {row}"
                ))
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(FullFrameOutput {
        identity: frame.identity().clone(),
        timestamps_ms,
        columns,
    })
}

fn clone_runtime<'catalog>(
    value: &NodeValue<'catalog>,
) -> Result<NodeValue<'catalog>, VectorCoreError> {
    Ok(match value {
        NodeValue::Null => NodeValue::Null,
        NodeValue::Bool(value) => NodeValue::Bool(*value),
        NodeValue::Integer(value) => NodeValue::Integer(*value),
        NodeValue::Float(value) => NodeValue::Float(*value),
        NodeValue::Text(value) => NodeValue::Text(value.clone()),
        NodeValue::Json => NodeValue::Json,
        NodeValue::Column(RuntimeColumn::Owned(column)) => {
            NodeValue::Column(RuntimeColumn::Owned(column.clone()))
        }
        NodeValue::Column(RuntimeColumn::Borrowed(column)) => {
            NodeValue::Column(RuntimeColumn::Borrowed(*column))
        }
        NodeValue::DataFrame | NodeValue::Metadata | NodeValue::Unbound | NodeValue::Alias(_) => {
            return Err(VectorCoreError::InvalidState(
                "unresolved runtime marker cannot cross a function boundary".to_owned(),
            ));
        }
    })
}

fn merge_rows(
    rows: &mut Option<usize>,
    actual: usize,
    node: &ProgramNode,
    source: &SourceLocation,
) -> Result<(), VectorCoreError> {
    if rows.is_some_and(|expected| expected != actual) {
        return Err(source.error(format!(
            "{} input row counts differ at node {}",
            node.op, node.id
        )));
    }
    *rows = Some(actual);
    Ok(())
}

fn runtime_column_len(column: &RuntimeColumn<'_>) -> usize {
    match column {
        RuntimeColumn::Borrowed(column) => column.len(),
        RuntimeColumn::Owned(column) => column.len(),
    }
}

fn select_indicator_output<'output>(
    node: &ProgramNode,
    output: &'output crate::kernels::KernelOutput,
    source: &SourceLocation,
) -> Result<&'output [f64], VectorCoreError> {
    if let Some(name) = node.parameters.get("output").and_then(Value::as_str) {
        return output
            .column(name)
            .ok_or_else(|| source.error(format!("indicator has no output named {name}")));
    }
    if output.columns().len() == 1 {
        Ok(&output.columns()[0])
    } else {
        Err(source.error("multi-output indicator requires an explicit output name"))
    }
}

fn bounded_argument(
    arguments: &Map<String, Value>,
    name: &str,
    source: &SourceLocation,
) -> Result<usize, VectorCoreError> {
    arguments
        .get(name)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or_else(|| {
            source.error(format!(
                "indicator argument {name:?} is not bounded integer"
            ))
        })
}

fn state_key(call_path: &str, node_id: &str) -> String {
    format!("{call_path}/{node_id}")
}

fn located(source: &SourceLocation, error: VectorCoreError) -> VectorCoreError {
    match error {
        VectorCoreError::UnsupportedOpcode { opcode, .. } => VectorCoreError::UnsupportedOpcode {
            opcode,
            location: format!("{}:{}:{}", source.path, source.line, source.column),
        },
        VectorCoreError::Execution { message, .. } => source.error(message),
        other => source.error(other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::alignment::Timeframe;

    fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
    }

    fn frame(
        pair: &str,
        timeframe: &str,
        timestamps_ms: Vec<i64>,
        close: Vec<Option<f64>>,
    ) -> NumericFrame {
        NumericFrame {
            identity: identity(pair, timeframe),
            timestamps_ms,
            columns: BTreeMap::from([("close".to_owned(), close)]),
        }
    }

    fn program() -> IndicatorProgram {
        let zero = || json!({"kind":"finite","candles":0,"expression":null,"causal":true});
        let one = || json!({"kind":"finite","candles":1,"expression":null,"causal":true});
        let recursive =
            || json!({"kind":"recursive","candles":null,"expression":"helper","causal":true});
        let source =
            || json!({"path":"strategy.py","line":42,"column":8,"end_line":42,"end_column":24});
        let mut encoded = json!({
            "schema_version":"indicator-program-v1",
            "source":{"path":"NestedFrames.py","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            "selected_class":"NestedFrames",
            "entrypoint":"f1",
            "functions":[
                {"id":"f1","source_name":"populate_indicators","kind":"entrypoint","parameters":[
                    {"name":"df","node":"n1","value_type":"dataframe"},
                    {"name":"metadata","node":"n2","value_type":"metadata"}
                ],"node_ids":["n1","n2","n12","n13","n14","n15","n16","n17"],"return_node":"n17"},
                {"id":"f2","source_name":"informative","kind":"helper","parameters":[
                    {"name":"metadata","node":"n6","value_type":"metadata"}
                ],"node_ids":["n6","n7","n8","n9","n10","n11"],"return_node":"n11"},
                {"id":"f3","source_name":"lag","kind":"helper","parameters":[
                    {"name":"values","node":"n3","value_type":"dynamic"}
                ],"node_ids":["n3","n4","n5"],"return_node":"n5"}
            ],
            "nodes":[
                {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"df"},"lookback":zero()},
                {"id":"n2","function":"f1","source_order":1,"op":"parameter","value_type":"metadata","inputs":[],"parameters":{"name":"metadata"},"lookback":zero()},
                {"id":"n3","function":"f3","source_order":0,"op":"parameter","value_type":"dynamic","inputs":[],"parameters":{"name":"values"},"lookback":zero()},
                {"id":"n4","function":"f3","source_order":1,"op":"shift","value_type":"f64-column","inputs":["n3"],"parameters":{"periods":1},"lookback":one()},
                {"id":"n5","function":"f3","source_order":2,"op":"return","value_type":"f64-column","inputs":["n4"],"parameters":{},"lookback":one()},
                {"id":"n6","function":"f2","source_order":0,"op":"parameter","value_type":"metadata","inputs":[],"parameters":{"name":"metadata"},"lookback":zero()},
                {"id":"n7","function":"f2","source_order":1,"op":"frame-source","value_type":"dataframe","inputs":[],"parameters":{"pair":{"kind":"metadata","key":"pair"},"timeframe":"15m"},"lookback":zero()},
                {"id":"n8","function":"f2","source_order":2,"op":"column-read","value_type":"f64-column","inputs":["n7"],"parameters":{"column":"close"},"lookback":zero()},
                {"id":"n9","function":"f2","source_order":3,"op":"function-call","value_type":"f64-column","inputs":["n8"],"parameters":{"function":"f3"},"lookback":recursive()},
                {"id":"n10","function":"f2","source_order":4,"op":"column-write","value_type":"dataframe","inputs":["n7","n9"],"parameters":{"column":"lagged","collision":"reject"},"lookback":recursive()},
                {"id":"n11","function":"f2","source_order":5,"op":"return","value_type":"dataframe","inputs":["n10"],"parameters":{},"lookback":recursive()},
                {"id":"n12","function":"f1","source_order":2,"op":"function-call","value_type":"dataframe","inputs":["n2"],"parameters":{"function":"f2"},"lookback":recursive()},
                {"id":"n13","function":"f1","source_order":3,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"close"},"lookback":zero()},
                {"id":"n14","function":"f1","source_order":4,"op":"function-call","value_type":"f64-column","inputs":["n13"],"parameters":{"function":"f3"},"lookback":recursive()},
                {"id":"n15","function":"f1","source_order":5,"op":"column-write","value_type":"dataframe","inputs":["n1","n14"],"parameters":{"column":"base_lagged","collision":"reject"},"lookback":recursive()},
                {"id":"n16","function":"f1","source_order":6,"op":"informative-merge","value_type":"dataframe","inputs":["n15","n12"],"parameters":{"base_timeframe":"5m","informative_timeframe":"15m","ffill":false,"append_timeframe":true,"date_column":"date","suffix":null},"lookback":recursive()},
                {"id":"n17","function":"f1","source_order":7,"op":"return","value_type":"dataframe","inputs":["n16"],"parameters":{},"lookback":recursive()}
            ],
            "required_input_columns":["close"],
            "produced_columns":["base_lagged","lagged"],
            "informative_nodes":["n16"],
            "opcodes":["column-read","column-write","frame-source","function-call","informative-merge","parameter","return","shift"],
            "max_lookback":{"kind":"mixed","candles":null,"expression":"finite+recursive","causal":true},
            "source_map":{},
            "fingerprint":""
        });
        for index in 1..=17 {
            encoded["source_map"][format!("n{index}")] = source();
        }
        encoded["fingerprint"] = Value::String(
            crate::program::validation::canonical_fingerprint(&encoded)
                .expect("canonical fingerprint"),
        );
        IndicatorProgram::from_json(&encoded.to_string()).expect("valid recursive program")
    }

    #[test]
    fn float_cast_accepts_the_compiler_shape_and_rejects_dynamic_arguments() {
        let mut node: ProgramNode = serde_json::from_value(json!({
            "id":"n1",
            "function":"f1",
            "source_order":0,
            "op":"cast",
            "value_type":"f64-column",
            "inputs":["n0"],
            "parameters":{"target":"float"},
            "lookback":{"kind":"finite","candles":0,"expression":null,"causal":true}
        }))
        .expect("cast node");
        let source = SourceLocation::new("cast", "strategy.py", 7, 4);

        validate_float_cast_parameters(&node, &source).expect("compiler cast shape");
        node.parameters.insert("arguments".to_owned(), Value::Null);
        validate_float_cast_parameters(&node, &source).expect("legacy null arguments");
        node.parameters
            .insert("arguments".to_owned(), json!({"copy": false}));
        assert!(validate_float_cast_parameters(&node, &source).is_err());
    }

    #[test]
    fn series_cast_accepts_the_compiler_shape_and_rejects_options() {
        let mut node: ProgramNode = serde_json::from_value(json!({
            "id":"n1",
            "function":"f1",
            "source_order":0,
            "op":"cast",
            "value_type":"f64-column",
            "inputs":["n0"],
            "parameters":{"target":"series"},
            "lookback":{"kind":"finite","candles":0,"expression":null,"causal":true}
        }))
        .expect("cast node");
        let source = SourceLocation::new("cast", "strategy.py", 7, 4);

        validate_identity_cast(&node, "series", &source).expect("compiler series shape");
        node.parameters.insert("arguments".to_owned(), Value::Null);
        validate_identity_cast(&node, "series", &source).expect("legacy null arguments");
        node.parameters
            .insert("arguments".to_owned(), json!({"copy": false}));
        assert!(validate_identity_cast(&node, "series", &source).is_err());
    }

    #[test]
    fn nested_dynamic_helpers_keep_state_and_row_counts_isolated_by_call_path() {
        let base = frame(
            "ETH/USDT",
            "5m",
            vec![0, 600_000, 900_000, 1_500_000],
            vec![Some(10.0), Some(20.0), Some(30.0), Some(40.0)],
        );
        let informative = frame(
            "ETH/USDT",
            "15m",
            vec![0, 900_000],
            vec![Some(1.0), Some(2.0)],
        );
        let catalog =
            FrameCatalog::new([(informative.identity.clone(), informative)]).expect("catalog");
        let metadata = BTreeMap::from([("pair".to_owned(), "ETH/USDT".to_owned())]);
        let program = program();
        let mut engine = FullIndicatorEngine::new(&program).expect("full engine");
        let output = engine
            .execute(
                &base,
                &catalog,
                &metadata,
                &["base_lagged".to_owned(), "lagged_15m".to_owned()],
            )
            .expect("complete execution");

        assert_eq!(output.identity(), &base.identity);
        assert_eq!(output.len(), 4);
        let base_lagged = output.columns()["base_lagged"].as_view();
        assert!(base_lagged.f64_at(0).expect("present NaN").is_nan());
        assert_eq!(base_lagged.f64_at(1), Some(10.0));
        assert_eq!(base_lagged.f64_at(2), Some(20.0));
        assert_eq!(base_lagged.f64_at(3), Some(30.0));

        let informative_lagged = output.columns()["lagged_15m"].as_view();
        assert_eq!(informative_lagged.len(), 4);
        assert!(informative_lagged
            .f64_at(1)
            .expect("informative leading NaN")
            .is_nan());
        assert_eq!(informative_lagged.f64_at(3), Some(1.0));
    }

    #[test]
    fn requested_typed_output_keeps_arrow_null_distinct_from_nan() {
        let base = frame(
            "ETH/USDT",
            "5m",
            vec![0, 600_000, 900_000, 1_500_000],
            vec![Some(10.0), Some(20.0), Some(30.0), Some(40.0)],
        );
        let informative = frame("ETH/USDT", "15m", vec![0, 900_000], vec![None, Some(2.0)]);
        let catalog =
            FrameCatalog::new([(informative.identity.clone(), informative)]).expect("catalog");
        let metadata = BTreeMap::from([("pair".to_owned(), "ETH/USDT".to_owned())]);
        let program = program();
        let output = FullIndicatorEngine::new(&program)
            .expect("full engine")
            .execute(&base, &catalog, &metadata, &["lagged_15m".to_owned()])
            .expect("complete execution");
        let values = output.columns()["lagged_15m"].as_view();
        assert!(values.f64_at(1).expect("present NaN").is_nan());
        assert_eq!(values.f64_at(3), None);
    }

    #[test]
    fn missing_informative_frame_fails_at_the_compiled_source_location() {
        let base = frame("ETH/USDT", "5m", vec![0], vec![Some(10.0)]);
        let metadata = BTreeMap::from([("pair".to_owned(), "ETH/USDT".to_owned())]);
        let program = program();
        let error = FullIndicatorEngine::new(&program)
            .expect("full engine")
            .execute(
                &base,
                &FrameCatalog::default(),
                &metadata,
                &["base_lagged".to_owned()],
            )
            .expect_err("missing informative frame");
        assert!(matches!(
            error,
            VectorCoreError::Execution { node, message }
                if node == "n7"
                    && message.starts_with("strategy.py:42:8:")
                    && message.contains("ETH/USDT 15m")
        ));
    }
}
