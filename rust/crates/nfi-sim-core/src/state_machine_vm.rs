//! Bounded interpreter for generic state-machine programs.

use std::collections::{BTreeMap, BTreeSet};

use num_traits::ToPrimitive;
use serde_json::{Number, Value};
use thiserror::Error;

use crate::domain::{
    StateMachineActionKind, StateMachineBinaryOperator, StateMachineBooleanOperator,
    StateMachineComparison, StateMachineExpression, StateMachineInstruction, StateMachineProgram,
    StateMachineReadSource, StateMachineScalarCall, StateMachineUnaryOperator,
    StateMachineValueType,
};

#[derive(Debug, Clone, Default)]
pub struct StateMachineContext {
    pub candle: BTreeMap<String, Value>,
    pub wallet: BTreeMap<String, Value>,
    pub trade: BTreeMap<String, Value>,
    pub orders: BTreeMap<String, Value>,
    pub custom_state: BTreeMap<String, Value>,
    pub input: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StateMachineAction {
    pub kind: StateMachineActionKind,
    pub stake: Option<f64>,
    pub tag: Option<String>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum StateMachineError {
    #[error("unsupported state-machine schema")]
    UnsupportedSchema,
    #[error("state-machine entrypoint is unavailable")]
    MissingEntrypoint,
    #[error("state-machine step budget is invalid or exhausted")]
    StepBudget,
    #[error("state-machine read is unavailable")]
    MissingRead,
    #[error("state-machine value type is invalid")]
    InvalidType,
    #[error("state-machine arithmetic is invalid")]
    InvalidArithmetic,
    #[error("state-machine loop bound is invalid")]
    InvalidLoop,
}

/// Execute one entrypoint in source order and stop at its first action.
///
/// # Errors
///
/// Unknown reads, invalid types, invalid arithmetic, and exhausted static step
/// bounds fail closed.
pub fn evaluate_state_machine(
    program: &StateMachineProgram,
    entrypoint: &str,
    context: &mut StateMachineContext,
) -> Result<Option<StateMachineAction>, StateMachineError> {
    if !supported_schema(&program.schema_version) {
        return Err(StateMachineError::UnsupportedSchema);
    }
    let entrypoint = program
        .entrypoints
        .get(entrypoint)
        .ok_or(StateMachineError::MissingEntrypoint)?;
    if entrypoint.max_steps == 0 {
        return Err(StateMachineError::StepBudget);
    }
    let original_custom_state = context.custom_state.clone();
    let result = {
        let mut machine = Machine {
            remaining_steps: entrypoint.max_steps,
            locals: BTreeMap::new(),
            context,
        };
        machine.execute(&entrypoint.instructions)
    };
    if result.is_err() {
        context.custom_state = original_custom_state;
    }
    result
}

#[must_use]
pub fn validate_state_machine_program(program: &StateMachineProgram) -> bool {
    if !supported_schema(&program.schema_version)
        || program.entrypoints.keys().any(|name| {
            !matches!(
                name.as_str(),
                "order_filled" | "adjust_trade_position" | "custom_exit"
            )
        })
        || program
            .entrypoints
            .values()
            .any(|entry| entry.max_steps == 0)
    {
        return false;
    }
    let mut ids = BTreeSet::new();
    let mut opcodes = BTreeSet::new();
    if !program.entrypoints.values().all(|entry| {
        validate_instructions(
            &entry.instructions,
            &mut ids,
            &mut opcodes,
            &program.source_map,
        )
    }) {
        return false;
    }
    let declared = program.opcodes.iter().cloned().collect::<BTreeSet<_>>();
    ids.len() == program.source_map.len()
        && declared == opcodes
        && program
            .required_reads
            .iter()
            .all(|read| !read.key.is_empty())
        && program.required_columns.iter().all(|key| !key.is_empty())
        && program
            .required_state_keys
            .iter()
            .all(|key| !key.is_empty())
}

fn supported_schema(schema_version: &str) -> bool {
    matches!(
        schema_version,
        "state-machine-program-v1" | "state-machine-program-v2"
    )
}

fn validate_instructions(
    instructions: &[StateMachineInstruction],
    ids: &mut BTreeSet<String>,
    opcodes: &mut BTreeSet<String>,
    source_map: &BTreeMap<String, crate::domain::StateMachineSourceLocation>,
) -> bool {
    instructions.iter().all(|instruction| {
        let id = instruction.id();
        if id.is_empty() || !ids.insert(id.to_owned()) || !source_map.contains_key(id) {
            return false;
        }
        let opcode = match instruction {
            StateMachineInstruction::If {
                then_instructions,
                else_instructions,
                ..
            } => {
                if !validate_instructions(then_instructions, ids, opcodes, source_map)
                    || !validate_instructions(else_instructions, ids, opcodes, source_map)
                {
                    return false;
                }
                "if"
            }
            StateMachineInstruction::SetLocal { name, .. } => {
                if name.is_empty() {
                    return false;
                }
                "set_local"
            }
            StateMachineInstruction::SetState { key, .. }
            | StateMachineInstruction::DeleteState { key, .. } => {
                if key.is_empty() {
                    return false;
                }
                if matches!(instruction, StateMachineInstruction::SetState { .. }) {
                    "set_state"
                } else {
                    "delete_state"
                }
            }
            StateMachineInstruction::Evaluate { .. } => "evaluate",
            StateMachineInstruction::BoundedFor {
                variable,
                start,
                stop,
                max_iterations,
                instructions,
                ..
            } => {
                let Some(iterations) = stop
                    .checked_sub(*start)
                    .and_then(|value| usize::try_from(value).ok())
                else {
                    return false;
                };
                if variable.is_empty()
                    || iterations > *max_iterations
                    || !validate_instructions(instructions, ids, opcodes, source_map)
                {
                    return false;
                }
                "bounded_for"
            }
            StateMachineInstruction::Action { .. } => "action",
        };
        opcodes.insert(opcode.to_owned());
        true
    })
}

struct Machine<'a> {
    remaining_steps: usize,
    locals: BTreeMap<String, Value>,
    context: &'a mut StateMachineContext,
}

impl Machine<'_> {
    fn step(&mut self) -> Result<(), StateMachineError> {
        self.remaining_steps = self
            .remaining_steps
            .checked_sub(1)
            .ok_or(StateMachineError::StepBudget)?;
        Ok(())
    }

    fn execute(
        &mut self,
        instructions: &[StateMachineInstruction],
    ) -> Result<Option<StateMachineAction>, StateMachineError> {
        for instruction in instructions {
            self.step()?;
            match instruction {
                StateMachineInstruction::If {
                    condition,
                    then_instructions,
                    else_instructions,
                    ..
                } => {
                    let condition = self.expression(condition)?;
                    let branch = if truthy(&condition) {
                        then_instructions
                    } else {
                        else_instructions
                    };
                    if let Some(action) = self.execute(branch)? {
                        return Ok(Some(action));
                    }
                }
                StateMachineInstruction::SetLocal { name, value, .. } => {
                    let value = self.expression(value)?;
                    self.locals.insert(name.clone(), value);
                }
                StateMachineInstruction::SetState {
                    key,
                    value_type,
                    value,
                    ..
                } => {
                    let value = self.expression(value)?;
                    if !value_matches_type(&value, *value_type) {
                        return Err(StateMachineError::InvalidType);
                    }
                    self.context.custom_state.insert(key.clone(), value);
                }
                StateMachineInstruction::DeleteState { key, .. } => {
                    self.context.custom_state.remove(key);
                }
                StateMachineInstruction::Evaluate { expression, .. } => {
                    self.expression(expression)?;
                }
                StateMachineInstruction::BoundedFor {
                    variable,
                    start,
                    stop,
                    max_iterations,
                    instructions,
                    ..
                } => {
                    let iterations = stop
                        .checked_sub(*start)
                        .and_then(|value| usize::try_from(value).ok())
                        .ok_or(StateMachineError::InvalidLoop)?;
                    if iterations > *max_iterations {
                        return Err(StateMachineError::InvalidLoop);
                    }
                    for value in *start..*stop {
                        self.locals
                            .insert(variable.clone(), Value::Number(value.into()));
                        if let Some(action) = self.execute(instructions)? {
                            return Ok(Some(action));
                        }
                    }
                }
                StateMachineInstruction::Action {
                    kind, stake, tag, ..
                } => {
                    let stake = stake
                        .as_ref()
                        .map(|value| {
                            let value = self.expression(value)?;
                            number(&value)
                        })
                        .transpose()?;
                    let tag = tag
                        .as_ref()
                        .map(|value| {
                            self.expression(value)?
                                .as_str()
                                .map(ToOwned::to_owned)
                                .ok_or(StateMachineError::InvalidType)
                        })
                        .transpose()?;
                    return Ok(Some(StateMachineAction {
                        kind: *kind,
                        stake,
                        tag,
                    }));
                }
            }
        }
        Ok(None)
    }

    fn expression(
        &mut self,
        expression: &StateMachineExpression,
    ) -> Result<Value, StateMachineError> {
        self.step()?;
        match expression {
            StateMachineExpression::Literal { value } => Ok(value.clone()),
            StateMachineExpression::Read {
                source,
                key,
                default,
            } => {
                let value = match source {
                    StateMachineReadSource::Candle => self.context.candle.get(key),
                    StateMachineReadSource::Wallet => self.context.wallet.get(key),
                    StateMachineReadSource::Trade => self.context.trade.get(key),
                    StateMachineReadSource::Orders => self.context.orders.get(key),
                    StateMachineReadSource::CustomState => self.context.custom_state.get(key),
                    StateMachineReadSource::Input => self.context.input.get(key),
                    StateMachineReadSource::Local => self.locals.get(key),
                };
                if let Some(value) = value {
                    Ok(value.clone())
                } else if let Some(default) = default {
                    self.expression(default)
                } else {
                    Err(StateMachineError::MissingRead)
                }
            }
            StateMachineExpression::Unary { operator, operand } => {
                let value = self.expression(operand)?;
                match operator {
                    StateMachineUnaryOperator::Not => Ok(Value::Bool(!truthy(&value))),
                    StateMachineUnaryOperator::Negative => json_number(-number(&value)?),
                    StateMachineUnaryOperator::Positive => json_number(number(&value)?),
                }
            }
            StateMachineExpression::Binary {
                operator,
                left,
                right,
            } => {
                let left_value = self.expression(left)?;
                let right_value = self.expression(right)?;
                let left = number(&left_value)?;
                let right = number(&right_value)?;
                let value = match operator {
                    StateMachineBinaryOperator::Add => left + right,
                    StateMachineBinaryOperator::Subtract => left - right,
                    StateMachineBinaryOperator::Multiply => left * right,
                    StateMachineBinaryOperator::Divide => left / right,
                    StateMachineBinaryOperator::FloorDivide => (left / right).floor(),
                    StateMachineBinaryOperator::Modulo => left % right,
                    StateMachineBinaryOperator::Power => left.powf(right),
                };
                json_number(value)
            }
            StateMachineExpression::Boolean { operator, values } => {
                if values.is_empty() {
                    return Err(StateMachineError::InvalidType);
                }
                let mut result = matches!(operator, StateMachineBooleanOperator::And);
                for value in values {
                    let value = truthy(&self.expression(value)?);
                    match operator {
                        StateMachineBooleanOperator::And => result &= value,
                        StateMachineBooleanOperator::Or => result |= value,
                    }
                }
                Ok(Value::Bool(result))
            }
            StateMachineExpression::Compare {
                operator,
                left,
                right,
            } => {
                let left = self.expression(left)?;
                let right = self.expression(right)?;
                Ok(Value::Bool(compare(*operator, &left, &right)?))
            }
            StateMachineExpression::ScalarCall { name, arguments } => {
                let values = arguments
                    .iter()
                    .map(|argument| self.expression(argument))
                    .collect::<Result<Vec<_>, _>>()?;
                scalar_call(*name, values)
            }
        }
    }
}

fn scalar_call(
    name: StateMachineScalarCall,
    values: Vec<Value>,
) -> Result<Value, StateMachineError> {
    match name {
        StateMachineScalarCall::Abs => unary_number(values, f64::abs),
        StateMachineScalarCall::Min => aggregate_numbers(values, f64::min),
        StateMachineScalarCall::Max => aggregate_numbers(values, f64::max),
        StateMachineScalarCall::Float => unary_number(values, |value| value),
        StateMachineScalarCall::Int => {
            let value = only(values)?;
            let value = number(&value)?;
            let integer = value
                .trunc()
                .to_i64()
                .ok_or(StateMachineError::InvalidArithmetic)?;
            Ok(Value::Number(integer.into()))
        }
        StateMachineScalarCall::Bool => Ok(Value::Bool(truthy(&only(values)?))),
        StateMachineScalarCall::Len => {
            let value = only(values)?;
            let length = match value {
                Value::String(value) => value.len(),
                Value::Array(value) => value.len(),
                Value::Object(value) => value.len(),
                _ => return Err(StateMachineError::InvalidType),
            };
            Ok(Value::Number(
                u64::try_from(length)
                    .map_err(|_| StateMachineError::InvalidArithmetic)?
                    .into(),
            ))
        }
    }
}

fn unary_number(
    values: Vec<Value>,
    operation: impl FnOnce(f64) -> f64,
) -> Result<Value, StateMachineError> {
    let value = only(values)?;
    json_number(operation(number(&value)?))
}

fn aggregate_numbers(
    values: Vec<Value>,
    operation: impl Fn(f64, f64) -> f64,
) -> Result<Value, StateMachineError> {
    let mut values = values.into_iter().map(|value| number(&value));
    let first = values.next().ok_or(StateMachineError::InvalidType)??;
    json_number(values.try_fold(first, |result, value| Ok(operation(result, value?)))?)
}

fn only(mut values: Vec<Value>) -> Result<Value, StateMachineError> {
    if values.len() != 1 {
        return Err(StateMachineError::InvalidType);
    }
    values.pop().ok_or(StateMachineError::InvalidType)
}

fn number(value: &Value) -> Result<f64, StateMachineError> {
    value
        .as_f64()
        .filter(|value| value.is_finite())
        .ok_or(StateMachineError::InvalidType)
}

fn json_number(value: f64) -> Result<Value, StateMachineError> {
    Number::from_f64(value)
        .map(Value::Number)
        .ok_or(StateMachineError::InvalidArithmetic)
}

fn compare(
    operator: StateMachineComparison,
    left: &Value,
    right: &Value,
) -> Result<bool, StateMachineError> {
    match operator {
        StateMachineComparison::Equal | StateMachineComparison::Is => Ok(left == right),
        StateMachineComparison::NotEqual | StateMachineComparison::IsNot => Ok(left != right),
        StateMachineComparison::Less => ordered(left, right, |a, b| a < b),
        StateMachineComparison::LessEqual => ordered(left, right, |a, b| a <= b),
        StateMachineComparison::Greater => ordered(left, right, |a, b| a > b),
        StateMachineComparison::GreaterEqual => ordered(left, right, |a, b| a >= b),
    }
}

fn ordered(
    left: &Value,
    right: &Value,
    comparison: impl FnOnce(f64, f64) -> bool,
) -> Result<bool, StateMachineError> {
    Ok(comparison(number(left)?, number(right)?))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn value_matches_type(value: &Value, expected: StateMachineValueType) -> bool {
    match expected {
        StateMachineValueType::Null => value.is_null(),
        StateMachineValueType::Bool => value.is_boolean(),
        StateMachineValueType::Integer => value.as_i64().is_some() || value.as_u64().is_some(),
        StateMachineValueType::Number => value.is_number(),
        StateMachineValueType::String => value.is_string(),
        StateMachineValueType::Json => true,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::{json, Value};

    use super::{
        evaluate_state_machine, validate_state_machine_program, StateMachineActionKind,
        StateMachineContext, StateMachineProgram,
    };

    #[test]
    fn v1_and_v2_program_contracts_remain_executable() {
        for schema_version in ["state-machine-program-v1", "state-machine-program-v2"] {
            let program: StateMachineProgram = serde_json::from_value(json!({
                "schema_version": schema_version,
                "entrypoints": {
                    "custom_exit": {
                        "max_steps": 1,
                        "instructions": []
                    }
                },
                "required_reads": [],
                "required_columns": [],
                "required_state_keys": [],
                "opcodes": [],
                "source_map": {}
            }))
            .expect("supported state-machine program");

            assert!(validate_state_machine_program(&program));
            assert!(evaluate_state_machine(
                &program,
                "custom_exit",
                &mut StateMachineContext::default()
            )
            .expect("supported program executes")
            .is_none());
        }
    }

    #[test]
    fn dynamic_grind_action_and_typed_state_execute_in_source_order() {
        let program: StateMachineProgram = serde_json::from_value(json!({
            "schema_version": "state-machine-program-v1",
            "entrypoints": {
                "adjust_trade_position": {
                    "max_steps": 32,
                    "instructions": [
                        {
                            "opcode": "set_state",
                            "id": "i1",
                            "key": "grind_level_12",
                            "value_type": "integer",
                            "value": {"kind": "literal", "value": 1}
                        },
                        {
                            "opcode": "if",
                            "id": "i2",
                            "condition": {
                                "kind": "compare",
                                "operator": "less",
                                "left": {
                                    "kind": "read",
                                    "source": "candle",
                                    "key": "current_profit",
                                    "default": null
                                },
                                "right": {"kind": "literal", "value": -0.1}
                            },
                            "then_instructions": [{
                                "opcode": "action",
                                "id": "i3",
                                "kind": "add_entry",
                                "stake": {"kind": "literal", "value": 25.0},
                                "tag": {"kind": "literal", "value": "grind_12_entry"}
                            }],
                            "else_instructions": []
                        }
                    ]
                }
            },
            "required_reads": [{"source": "candle", "key": "current_profit"}],
            "required_columns": [],
            "required_state_keys": ["grind_level_12"],
            "opcodes": ["action", "if", "set_state"],
            "source_map": {
                "i1": {"path": "strategy.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1},
                "i2": {"path": "strategy.py", "line": 2, "column": 0, "end_line": 2, "end_column": 1},
                "i3": {"path": "strategy.py", "line": 3, "column": 0, "end_line": 3, "end_column": 1}
            }
        }))
        .expect("valid state-machine program");
        let mut context = StateMachineContext {
            candle: BTreeMap::from([("current_profit".to_owned(), json!(-0.2))]),
            ..StateMachineContext::default()
        };

        let action = evaluate_state_machine(&program, "adjust_trade_position", &mut context)
            .expect("program executes")
            .expect("action exists");

        assert_eq!(action.kind, StateMachineActionKind::AddEntry);
        assert_eq!(action.stake, Some(25.0));
        assert_eq!(action.tag.as_deref(), Some("grind_12_entry"));
        assert_eq!(
            context.custom_state.get("grind_level_12"),
            Some(&Value::from(1))
        );
    }

    #[test]
    fn bounded_loop_rejects_runtime_bound_above_compiled_limit() {
        let program: StateMachineProgram = serde_json::from_value(json!({
            "schema_version": "state-machine-program-v1",
            "entrypoints": {
                "order_filled": {
                    "max_steps": 8,
                    "instructions": [{
                        "opcode": "bounded_for",
                        "id": "i1",
                        "variable": "level",
                        "start": 0,
                        "stop": 12,
                        "max_iterations": 7,
                        "instructions": []
                    }]
                }
            },
            "required_reads": [],
            "required_columns": [],
            "required_state_keys": [],
            "opcodes": ["bounded_for"],
            "source_map": {
                "i1": {"path": "strategy.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1}
            }
        }))
        .expect("valid state-machine program");

        let error = evaluate_state_machine(
            &program,
            "order_filled",
            &mut StateMachineContext::default(),
        )
        .expect_err("loop must fail closed");

        assert_eq!(error, super::StateMachineError::InvalidLoop);
    }

    #[test]
    fn failed_program_rolls_back_custom_state_writes() {
        let program: StateMachineProgram = serde_json::from_value(json!({
            "schema_version": "state-machine-program-v1",
            "entrypoints": {
                "order_filled": {
                    "max_steps": 8,
                    "instructions": [
                        {
                            "opcode": "set_state",
                            "id": "i1",
                            "key": "level",
                            "value_type": "integer",
                            "value": {"kind": "literal", "value": 12}
                        },
                        {
                            "opcode": "evaluate",
                            "id": "i2",
                            "expression": {
                                "kind": "read",
                                "source": "input",
                                "key": "missing",
                                "default": null
                            }
                        }
                    ]
                }
            },
            "required_reads": [{"source": "input", "key": "missing"}],
            "required_columns": [],
            "required_state_keys": ["level"],
            "opcodes": ["evaluate", "set_state"],
            "source_map": {
                "i1": {"path": "strategy.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1},
                "i2": {"path": "strategy.py", "line": 2, "column": 0, "end_line": 2, "end_column": 1}
            }
        }))
        .expect("valid state-machine program");
        let mut context = StateMachineContext {
            custom_state: BTreeMap::from([("level".to_owned(), json!(1))]),
            ..StateMachineContext::default()
        };

        let error = evaluate_state_machine(&program, "order_filled", &mut context)
            .expect_err("missing read must fail closed");

        assert_eq!(error, super::StateMachineError::MissingRead);
        assert_eq!(context.custom_state.get("level"), Some(&json!(1)));
    }
}
