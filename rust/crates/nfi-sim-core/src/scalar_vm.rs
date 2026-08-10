//! Fail-closed scalar callback virtual machine.

use std::collections::BTreeMap;

use num_traits::ToPrimitive;
use serde_json::Value;

use crate::domain::ScalarDecisionProgram;

pub(crate) fn valid_vm_value(value: &Value) -> bool {
    match value {
        Value::Bool(_) | Value::Number(_) | Value::String(_) => true,
        Value::Array(values) => values
            .iter()
            .all(|item| matches!(item, Value::Bool(_) | Value::Number(_) | Value::String(_))),
        Value::Null | Value::Object(_) => false,
    }
}

pub(crate) fn number_value(value: f64) -> Option<Value> {
    serde_json::Number::from_f64(value).map(Value::Number)
}

#[allow(clippy::float_cmp)] // A VM index is valid only when its float token is exactly integral.
pub(crate) fn integer_value(value: &Value) -> Option<i64> {
    if let Some(integer) = value.as_i64() {
        return Some(integer);
    }
    // Arithmetic expressions such as unary minus are serialized through
    // `Number::from_f64`, so JSON `-1.0` no longer answers `as_i64()` even
    // though Python treats it as the exact list index -1. Accept only finite,
    // integral values inside i64's exactly checked conversion range.
    let numeric = value.as_f64()?;
    if !numeric.is_finite() || numeric.fract() != 0.0 {
        return None;
    }
    numeric.to_i64()
}

enum ScalarControl {
    Continue,
    Return(Value),
}

/// Per-program mutable scope over an immutable callback input map.
///
/// NFI evaluates several pure exit programs against the same dataframe window.
/// Keeping program-local writes in this overlay avoids deep-cloning the trade
/// and seven projected row objects for every program while preserving the
/// fresh Python local scope each method receives.
struct ScalarScope<'a> {
    base: &'a BTreeMap<String, Value>,
    local: Vec<(String, Value)>,
}

impl<'a> ScalarScope<'a> {
    fn new(base: &'a BTreeMap<String, Value>) -> Self {
        Self {
            base,
            local: Vec::new(),
        }
    }

    fn get(&self, name: &str) -> Option<&Value> {
        self.local
            .iter()
            .rev()
            .find_map(|(key, value)| (key == name).then_some(value))
            .or_else(|| self.base.get(name))
    }

    fn insert(&mut self, name: String, value: Value) {
        if let Some((_, current)) = self.local.iter_mut().find(|(key, _)| key == &name) {
            *current = value;
        } else {
            self.local.push((name, value));
        }
    }
}

/// Evaluate a compact scalar-decision program without entering Python.
///
/// Inputs are the already-normalized method arguments. The function returns
/// `None` when either the program contract or a runtime value is invalid.
#[must_use]
pub fn evaluate_scalar_decision_program(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(program, variables, None, 0)
}

/// Evaluate one entry method in a hash-bound scalar program bundle.
///
/// Calls are resolved only inside `programs`; missing methods, arity drift,
/// recursive overflow, and malformed values all fail closed.
#[must_use]
pub fn evaluate_scalar_program_bundle(
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    entry: &str,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program(programs.get(entry)?, variables, Some(programs), 0)
}

#[cfg(test)]
pub(crate) fn evaluate_scalar_program_bundle_from_base(
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    entry: &str,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program_from_base(programs.get(entry)?, variables, Some(programs), 0)
}

pub(crate) fn evaluate_scalar_program_handle_from_base(
    program: &ScalarDecisionProgram,
    programs: &BTreeMap<String, ScalarDecisionProgram>,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    evaluate_scalar_program_from_base(program, variables, Some(programs), 0)
}

fn evaluate_scalar_program(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    evaluate_scalar_program_from_base(program, variables, programs, depth)
}

fn evaluate_scalar_program_from_base(
    program: &ScalarDecisionProgram,
    variables: &BTreeMap<String, Value>,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 64
        || !matches!(program.schema_version.as_str(), "1.0.0" | "1.1.0" | "1.2.0")
        || program.opcode != "scalar-decision-program-v1"
    {
        return None;
    }
    if program
        .parameters
        .iter()
        .any(|parameter| !variables.contains_key(parameter))
    {
        return None;
    }
    let mut scope = ScalarScope::new(variables);
    let ScalarControl::Return(value) =
        evaluate_scalar_statements(&program.statements, &mut scope, program, programs, depth)?
    else {
        return None;
    };
    Some(value)
}

fn evaluate_scalar_statements(
    statements: &[Value],
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    if depth > 256 {
        return None;
    }
    for statement in statements {
        let fields = statement.as_array()?;
        match fields.first()?.as_str()? {
            "set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(fields.get(1)?.as_str()?.to_owned(), value);
            }
            "ephemeral-set" if fields.len() == 3 => {
                let value = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                variables.insert(format!("$ephemeral.{}", fields.get(1)?.as_str()?), value);
            }
            "unpack" if fields.len() == 3 => {
                let names = fields.get(1)?.as_array()?;
                let values = evaluate_scalar_expression(
                    value_index(fields.get(2)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let values = values.as_array()?;
                if names.len() != values.len() {
                    return None;
                }
                for (name, value) in names.iter().zip(values) {
                    variables.insert(name.as_str()?.to_owned(), value.clone());
                }
            }
            "if" if fields.len() == 4 => {
                let condition = evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let branch = if scalar_truthy(&condition) {
                    fields.get(2)?
                } else {
                    fields.get(3)?
                };
                if let control @ ScalarControl::Return(_) = evaluate_scalar_statements(
                    branch.as_array()?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )? {
                    return Some(control);
                }
            }
            "if-chain" if fields.len() == 3 => {
                if let control @ ScalarControl::Return(_) =
                    evaluate_scalar_if_chain(fields, variables, program, programs, depth)?
                {
                    return Some(control);
                }
            }
            "return" if fields.len() == 2 => {
                return Some(ScalarControl::Return(evaluate_scalar_expression(
                    value_index(fields.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?));
            }
            "pass" if fields.len() == 1 => {}
            _ => return None,
        }
    }
    Some(ScalarControl::Continue)
}

fn evaluate_scalar_if_chain(
    fields: &[Value],
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<ScalarControl> {
    let mut selected = None;
    for branch in fields.get(1)?.as_array()? {
        let branch = branch.as_array()?;
        if branch.len() != 2 {
            return None;
        }
        let condition = evaluate_scalar_expression(
            value_index(branch.first()?)?,
            variables,
            program,
            programs,
            depth + 1,
        )?;
        if scalar_truthy(&condition) {
            selected = Some(branch.get(1)?);
            break;
        }
    }
    let branch = selected.unwrap_or(fields.get(2)?);
    evaluate_scalar_statements(branch.as_array()?, variables, program, programs, depth + 1)
}

#[allow(clippy::too_many_lines)]
fn evaluate_scalar_expression(
    index: usize,
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    if depth > 256 {
        return None;
    }
    let fields = program.expressions.get(index)?.as_array()?;
    let opcode = fields.first()?.as_str()?;
    match opcode {
        "literal" if fields.len() == 2 => fields.get(1).cloned(),
        "variable" if fields.len() == 2 => variables.get(fields.get(1)?.as_str()?).cloned(),
        "attribute" if fields.len() == 3 => {
            if let Some(value) =
                scalar_direct_variable(program, value_index(fields.get(1)?)?, variables)
            {
                return value.as_object()?.get(fields.get(2)?.as_str()?).cloned();
            }
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            value.as_object()?.get(fields.get(2)?.as_str()?).cloned()
        }
        "index" if fields.len() == 3 => {
            let base_index = value_index(fields.get(1)?)?;
            let key_index = value_index(fields.get(2)?)?;
            if let (Some(value), Some(index)) = (
                scalar_direct_variable(program, base_index, variables),
                scalar_direct_literal(program, key_index),
            ) {
                // Dataframe access is represented as
                // `last_candle["field"]`. Resolve that immutable lookup by
                // reference instead of cloning the complete projected row
                // before selecting one scalar.
                return scalar_index(value, index);
            }
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let index = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_index(&value, &index)
        }
        "not" if fields.len() == 2 => Some(Value::Bool(!scalar_truthy(&scalar_operand(
            fields, 1, variables, program, programs, depth,
        )?))),
        "negative" | "positive" if fields.len() == 2 => {
            let value = scalar_number(&scalar_operand(
                fields, 1, variables, program, programs, depth,
            )?)?;
            scalar_number_value(if opcode == "negative" { -value } else { value })
        }
        "add" | "subtract" | "multiply" | "divide" | "floor-divide" | "modulo" | "power"
            if fields.len() == 3 =>
        {
            let left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let right = scalar_operand(fields, 2, variables, program, programs, depth)?;
            scalar_binary(opcode, &left, &right)
        }
        "and" | "or" if fields.len() == 2 => {
            let operands = fields.get(1)?.as_array()?;
            let mut last = Value::Bool(opcode == "and");
            for operand in operands {
                last = evaluate_scalar_expression(
                    value_index(operand)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if (opcode == "and" && !scalar_truthy(&last))
                    || (opcode == "or" && scalar_truthy(&last))
                {
                    break;
                }
            }
            Some(last)
        }
        "compare" if fields.len() == 3 => {
            let mut left = scalar_operand(fields, 1, variables, program, programs, depth)?;
            for comparison in fields.get(2)?.as_array()? {
                let comparison = comparison.as_array()?;
                if comparison.len() != 2 {
                    return None;
                }
                let right = evaluate_scalar_expression(
                    value_index(comparison.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                if !scalar_compare(comparison.first()?.as_str()?, &left, &right)? {
                    return Some(Value::Bool(false));
                }
                left = right;
            }
            Some(Value::Bool(true))
        }
        "if-expression" if fields.len() == 4 => {
            let condition = scalar_operand(fields, 1, variables, program, programs, depth)?;
            scalar_operand(
                fields,
                if scalar_truthy(&condition) { 2 } else { 3 },
                variables,
                program,
                programs,
                depth,
            )
        }
        "tuple" | "list" | "set-literal" if fields.len() == 2 => Some(Value::Array(
            fields
                .get(1)?
                .as_array()?
                .iter()
                .map(|item| {
                    evaluate_scalar_expression(
                        value_index(item)?,
                        variables,
                        program,
                        programs,
                        depth + 1,
                    )
                })
                .collect::<Option<Vec<_>>>()?,
        )),
        "dict" if fields.len() == 2 => {
            let mut result = serde_json::Map::new();
            for item in fields.get(1)?.as_array()? {
                let item = item.as_array()?;
                if item.len() != 2 {
                    return None;
                }
                let key = evaluate_scalar_expression(
                    value_index(item.first()?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                let value = evaluate_scalar_expression(
                    value_index(item.get(1)?)?,
                    variables,
                    program,
                    programs,
                    depth + 1,
                )?;
                result.insert(scalar_string(&key), value);
            }
            Some(Value::Object(result))
        }
        "format" if fields.len() == 2 => {
            let mut result = String::new();
            for part in fields.get(1)?.as_array()? {
                let part = part.as_array()?;
                match part.first()?.as_str()? {
                    "text" if part.len() == 2 => result.push_str(part.get(1)?.as_str()?),
                    "value" if part.len() == 2 => {
                        let value = evaluate_scalar_expression(
                            value_index(part.get(1)?)?,
                            variables,
                            program,
                            programs,
                            depth + 1,
                        )?;
                        result.push_str(&scalar_string(&value));
                    }
                    _ => return None,
                }
            }
            Some(Value::String(result))
        }
        "call-program" if fields.len() == 3 => {
            let programs = programs?;
            let callee = programs.get(fields.get(1)?.as_str()?)?;
            let arguments = fields.get(2)?.as_array()?;
            if arguments.len() != callee.parameters.len() {
                return None;
            }
            let mut callee_variables = BTreeMap::new();
            for (parameter, argument) in callee.parameters.iter().zip(arguments) {
                let value = evaluate_scalar_expression(
                    value_index(argument)?,
                    variables,
                    program,
                    Some(programs),
                    depth + 1,
                )?;
                callee_variables.insert(parameter.clone(), value);
            }
            evaluate_scalar_program(callee, &callee_variables, Some(programs), depth + 1)
        }
        "is-instance" if fields.len() == 3 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let matches = match fields.get(2)?.as_str()? {
                "bool" => value.is_boolean(),
                "float" | "np.float64" => scalar_number(&value).is_some(),
                "int" => value
                    .as_i64()
                    .or_else(|| value.as_u64().and_then(|item| i64::try_from(item).ok()))
                    .is_some(),
                "str" => value.is_string(),
                _ => return None,
            };
            Some(Value::Bool(matches))
        }
        "length" if fields.len() == 2 => {
            let value = scalar_operand(fields, 1, variables, program, programs, depth)?;
            let length = match value {
                Value::Array(values) => values.len(),
                Value::Object(values) => values.len(),
                Value::String(value) => value.chars().count(),
                _ => return None,
            };
            Some(Value::Number(u64::try_from(length).ok()?.into()))
        }
        _ => None,
    }
}

fn scalar_direct_variable<'a>(
    program: &ScalarDecisionProgram,
    index: usize,
    variables: &'a ScalarScope<'_>,
) -> Option<&'a Value> {
    let expression = program.expressions.get(index)?.as_array()?;
    (expression.first()?.as_str()? == "variable")
        .then(|| expression.get(1)?.as_str())
        .flatten()
        .and_then(|name| variables.get(name))
}

fn scalar_direct_literal(program: &ScalarDecisionProgram, index: usize) -> Option<&Value> {
    let expression = program.expressions.get(index)?.as_array()?;
    (expression.first()?.as_str()? == "literal")
        .then(|| expression.get(1))
        .flatten()
}

fn scalar_operand(
    fields: &[Value],
    position: usize,
    variables: &mut ScalarScope<'_>,
    program: &ScalarDecisionProgram,
    programs: Option<&BTreeMap<String, ScalarDecisionProgram>>,
    depth: usize,
) -> Option<Value> {
    evaluate_scalar_expression(
        value_index(fields.get(position)?)?,
        variables,
        program,
        programs,
        depth + 1,
    )
}

pub(crate) fn value_index(value: &Value) -> Option<usize> {
    usize::try_from(value.as_u64()?).ok()
}

pub(crate) fn scalar_number(value: &Value) -> Option<f64> {
    if let Some(value) = value.as_f64() {
        return Some(value);
    }
    let marker = value.as_object()?.get("$float")?.as_str()?;
    match marker {
        "nan" => Some(f64::NAN),
        "inf" | "infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        _ => None,
    }
}

pub(crate) fn scalar_number_value(value: f64) -> Option<Value> {
    if value.is_finite() {
        return number_value(value);
    }
    let marker = if value.is_nan() {
        "nan"
    } else if value.is_sign_positive() {
        "inf"
    } else {
        "-inf"
    };
    Some(serde_json::json!({"$float": marker}))
}

fn scalar_binary(opcode: &str, left: &Value, right: &Value) -> Option<Value> {
    if opcode == "add" {
        if let (Some(left), Some(right)) = (left.as_str(), right.as_str()) {
            return Some(Value::String(format!("{left}{right}")));
        }
        if let (Some(left), Some(right)) = (left.as_array(), right.as_array()) {
            return Some(Value::Array(
                left.iter().chain(right).cloned().collect::<Vec<_>>(),
            ));
        }
    }
    let left = scalar_number(left)?;
    let right = scalar_number(right)?;
    let result = match opcode {
        "add" => left + right,
        "subtract" => left - right,
        "multiply" => left * right,
        "divide" => left / right,
        "floor-divide" => (left / right).floor(),
        "modulo" => left - (left / right).floor() * right,
        "power" => left.powf(right),
        _ => return None,
    };
    scalar_number_value(result)
}

fn scalar_compare(opcode: &str, left: &Value, right: &Value) -> Option<bool> {
    match opcode {
        "equal" | "is" => Some(scalar_equal(left, right)),
        "not-equal" | "is-not" => Some(!scalar_equal(left, right)),
        "less" | "less-equal" | "greater" | "greater-equal" => {
            if let (Some(left), Some(right)) = (scalar_number(left), scalar_number(right)) {
                return Some(match opcode {
                    "less" => left < right,
                    "less-equal" => left <= right,
                    "greater" => left > right,
                    "greater-equal" => left >= right,
                    _ => unreachable!(),
                });
            }
            let (left, right) = (left.as_str()?, right.as_str()?);
            Some(match opcode {
                "less" => left < right,
                "less-equal" => left <= right,
                "greater" => left > right,
                "greater-equal" => left >= right,
                _ => unreachable!(),
            })
        }
        "in" | "not-in" => {
            let included = match right {
                Value::Array(values) => values.iter().any(|value| scalar_equal(left, value)),
                Value::Object(values) => left.as_str().is_some_and(|key| values.contains_key(key)),
                Value::String(value) => left.as_str().is_some_and(|item| value.contains(item)),
                _ => return None,
            };
            Some(if opcode == "in" { included } else { !included })
        }
        _ => None,
    }
}

#[allow(clippy::float_cmp)]
fn scalar_equal(left: &Value, right: &Value) -> bool {
    match (scalar_number(left), scalar_number(right)) {
        (Some(left), Some(right)) => left == right,
        (Some(left), None) if right.is_boolean() => {
            left == f64::from(u8::from(right.as_bool().unwrap_or(false)))
        }
        (None, Some(right)) if left.is_boolean() => {
            f64::from(u8::from(left.as_bool().unwrap_or(false))) == right
        }
        _ => left == right,
    }
}

fn scalar_index(value: &Value, index: &Value) -> Option<Value> {
    match value {
        Value::Object(values) => values.get(index.as_str()?).cloned(),
        Value::Array(values) => {
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(values.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            values.get(usize::try_from(normalized).ok()?).cloned()
        }
        Value::String(value) => {
            let characters = value.chars().collect::<Vec<_>>();
            let raw = integer_value(index)?;
            let normalized = if raw < 0 {
                i64::try_from(characters.len()).ok()?.checked_add(raw)?
            } else {
                raw
            };
            Some(Value::String(
                characters
                    .get(usize::try_from(normalized).ok()?)?
                    .to_string(),
            ))
        }
        _ => None,
    }
}

pub(crate) fn scalar_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => scalar_number(&Value::Object(value.clone()))
            .map_or(!value.is_empty(), |number| number != 0.0),
    }
}

fn scalar_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        Value::Number(value) => value.to_string(),
        Value::Object(_) if scalar_number(value).is_some() => {
            let number = scalar_number(value).unwrap_or(f64::NAN);
            if number.is_nan() {
                "nan".to_owned()
            } else if number.is_sign_positive() {
                "inf".to_owned()
            } else {
                "-inf".to_owned()
            }
        }
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}
