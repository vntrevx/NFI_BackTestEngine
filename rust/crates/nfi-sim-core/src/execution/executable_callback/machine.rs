use std::collections::BTreeMap;

use serde_json::Value;

use super::types::{
    CallbackInvocation, CallbackObservation, CallbackProgramException, CallbackProgramResult,
    CallbackTypedDelta, EXECUTABLE_CALLBACK_TRACE_SCHEMA_VERSION,
};
use super::value::Values;
use crate::domain::{
    callback_value_matches_type as matches_type, validate_executable_callback_program,
    CallbackReturnClass, CallbackType, ExecutableCallbackError as Error, ExecutableCallbackProgram,
};

#[path = "machine/interpreter.rs"]
mod interpreter;
#[path = "machine/support.rs"]
mod support;
use support::{map_fingerprint, runtime_error, source_id, valid_return, RuntimeKind};

pub struct CallbackProgramRuntime {
    registers: BTreeMap<String, Value>,
    register_types: BTreeMap<String, CallbackType>,
    custom_types: BTreeMap<String, Option<CallbackType>>,
    program_fingerprint: String,
}

enum Flow {
    Continue,
    Returned(CallbackReturnClass, Option<Value>),
    Raised(CallbackProgramException),
}

struct Machine<'a> {
    source_id: &'a str,
    callback: &'a str,
    timestamp_ms: i64,
    invocation: &'a CallbackInvocation,
    remaining: usize,
    registers: &'a mut BTreeMap<String, Value>,
    register_types: &'a BTreeMap<String, CallbackType>,
    custom: &'a mut BTreeMap<String, Value>,
    custom_types: &'a BTreeMap<String, Option<CallbackType>>,
    locals: BTreeMap<String, Value>,
    register_deltas: Vec<CallbackTypedDelta>,
    custom_deltas: Vec<CallbackTypedDelta>,
    observations: Vec<CallbackObservation>,
    predicates: Vec<String>,
    current: Option<String>,
    observation_bytes: usize,
}

impl CallbackProgramRuntime {
    /// Construct one strategy-run register bank after complete preflight.
    ///
    /// # Errors
    ///
    /// Returns an error when the program or a register initializer is invalid.
    pub fn new(program: &ExecutableCallbackProgram) -> Result<Self, Error> {
        validate_executable_callback_program(program)?;
        let mut registers = BTreeMap::new();
        let mut register_types = BTreeMap::new();
        let invocation = CallbackInvocation::new("register_initialization", 0, BTreeMap::new());
        for declaration in &program.registers {
            let locals = BTreeMap::new();
            let custom = BTreeMap::new();
            let value = Values {
                invocation: &invocation,
                registers: &registers,
                custom: &custom,
                locals: &locals,
            }
            .evaluate(&declaration.initial)
            .map_err(|_| Error::InvalidExecutableCallbackProgram {
                reason: "register initial expression is invalid".to_owned(),
            })?;
            if !matches_type(&value, &declaration.value_type) {
                return Err(Error::InvalidExecutableCallbackProgram {
                    reason: "register initial value has the wrong type".to_owned(),
                });
            }
            registers.insert(declaration.id.clone(), value);
            register_types.insert(declaration.id.clone(), declaration.value_type.clone());
        }
        let custom_types = program
            .required_custom_state
            .iter()
            .map(|item| (item.key.clone(), item.value_type.clone()))
            .collect();
        Ok(Self {
            registers,
            register_types,
            custom_types,
            program_fingerprint: program.identity.program_fingerprint.clone(),
        })
    }

    #[must_use]
    pub fn registers(&self) -> &BTreeMap<String, Value> {
        &self.registers
    }

    /// Execute one validated callback invocation against the run-scoped state.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid transitions, inputs, runtime faults, or invalid returns.
    pub fn invoke(
        &mut self,
        program: &ExecutableCallbackProgram,
        invocation: &CallbackInvocation,
        custom_state: &mut BTreeMap<String, Value>,
    ) -> Result<CallbackProgramResult, Error> {
        self.validate_invocation(program, invocation)?;
        let source_id = source_id(program, &invocation.callback);
        let entry = program
            .entrypoints
            .get(&invocation.callback)
            .ok_or_else(|| runtime_error(program, invocation, None, RuntimeKind::Transition))?;
        let before = map_fingerprint(&self.registers)?;
        let mut machine = Machine {
            source_id: &source_id,
            callback: &invocation.callback,
            timestamp_ms: invocation.timestamp_ms,
            invocation,
            remaining: entry.max_steps,
            registers: &mut self.registers,
            register_types: &self.register_types,
            custom: custom_state,
            custom_types: &self.custom_types,
            locals: invocation.inputs.clone(),
            register_deltas: Vec::new(),
            custom_deltas: Vec::new(),
            observations: Vec::new(),
            predicates: Vec::new(),
            current: None,
            observation_bytes: 0,
        };
        let flow = machine.execute(&entry.instructions)?;
        let (return_class, return_value, transaction, exception) =
            machine.resolve_flow(flow, &entry.exception_fallback)?;
        if !valid_return(&invocation.callback, return_class, return_value.as_ref()) {
            return Err(machine.error(RuntimeKind::Return));
        }
        let after = map_fingerprint(machine.registers)?;
        let custom_fingerprint = map_fingerprint(machine.custom)?;
        Ok(CallbackProgramResult {
            schema_version: EXECUTABLE_CALLBACK_TRACE_SCHEMA_VERSION,
            callback_contract_fingerprint: program.identity.callback_contract_fingerprint.clone(),
            callback_execution_ir_fingerprint: program
                .identity
                .callback_execution_ir_fingerprint
                .clone(),
            callback_name: invocation.callback.clone(),
            source_id: source_id.clone(),
            program_fingerprint: program.identity.program_fingerprint.clone(),
            return_class,
            return_value,
            transaction,
            exception,
            predicate_ids: machine.predicates,
            register_deltas: machine.register_deltas,
            custom_state_deltas: machine.custom_deltas,
            observations: machine.observations,
            register_before_fingerprint: before,
            register_after_fingerprint: after,
            custom_state_fingerprint: custom_fingerprint,
        })
    }

    fn validate_invocation(
        &self,
        program: &ExecutableCallbackProgram,
        invocation: &CallbackInvocation,
    ) -> Result<(), Error> {
        if program.identity.program_fingerprint != self.program_fingerprint {
            return Err(runtime_error(
                program,
                invocation,
                None,
                RuntimeKind::Transition,
            ));
        }
        let entry = program
            .entrypoints
            .get(&invocation.callback)
            .ok_or_else(|| runtime_error(program, invocation, None, RuntimeKind::Transition))?;
        if !entry.active {
            return Err(runtime_error(
                program,
                invocation,
                None,
                RuntimeKind::Transition,
            ));
        }
        for requirement in program
            .required_inputs
            .iter()
            .filter(|item| item.entrypoint == invocation.callback)
        {
            let Some(value) = invocation.inputs.get(&requirement.name) else {
                return Err(runtime_error(
                    program,
                    invocation,
                    None,
                    RuntimeKind::Missing,
                ));
            };
            if !matches_type(value, &requirement.value_type) {
                return Err(runtime_error(
                    program,
                    invocation,
                    None,
                    RuntimeKind::Missing,
                ));
            }
        }
        Ok(())
    }
}
