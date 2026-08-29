use std::collections::BTreeSet;

#[path = "validation_tree/expression.rs"]
mod expression_validation;
pub(crate) use expression_validation::initial_expression;
pub(crate) use expression_validation::matches_type;
use expression_validation::{expression, range_iterations};

use super::{CallbackStatement as S, ExecutableCallbackEntrypoint, ExecutableCallbackError};

pub(super) struct Declarations<'a> {
    pub callback: &'a str,
    pub registers: &'a BTreeSet<String>,
    pub custom: &'a BTreeSet<String>,
    pub inputs: &'a BTreeSet<(String, String)>,
    pub predicates: &'a BTreeSet<String>,
}

pub(super) fn validate_entry(
    entry: &ExecutableCallbackEntrypoint,
    declarations: &Declarations<'_>,
    ids: &mut BTreeSet<String>,
) -> Result<(), ExecutableCallbackError> {
    if entry.max_steps == 0 || entry.max_steps > 4096 {
        return Err(invalid("entrypoint max_steps is outside 1..=4096"));
    }
    validate_predicates(&entry.predicate_ids, declarations)?;
    let cost = statements(&entry.instructions, declarations, ids, 0)?;
    if cost > entry.max_steps {
        return Err(invalid("entrypoint max_steps is below its static bound"));
    }
    Ok(())
}

fn statements(
    values: &[S],
    d: &Declarations<'_>,
    ids: &mut BTreeSet<String>,
    depth: usize,
) -> Result<usize, ExecutableCallbackError> {
    validate_statement_depth(depth)?;
    let mut total = 0_usize;
    for statement in values {
        let (id, predicates) = statement_identity(statement);
        if !valid_id(id, 'i') || !ids.insert(id.to_owned()) {
            return Err(invalid("instruction id is invalid or duplicate"));
        }
        validate_predicates(predicates, d)?;
        let cost = match statement {
            S::Let { value, .. } => expression(value, d, depth + 1)?,
            S::SetRegister {
                register_id, value, ..
            } => {
                if !d.registers.contains(register_id) {
                    return Err(invalid("set_register references an undeclared register"));
                }
                expression(value, d, depth + 1)?
            }
            S::SetRegisterItem {
                register_id,
                key,
                value,
                ..
            } => register_item_cost(register_id, key, value, d, depth)?,
            S::SetCustomState { key, value, .. } => {
                if !d.custom.contains(key) {
                    return Err(invalid("set_custom_state key is undeclared"));
                }
                expression(value, d, depth + 1)?
            }
            S::DeleteCustomState { key, .. } => {
                if !d.custom.contains(key) {
                    return Err(invalid("delete_custom_state key is undeclared"));
                }
                0
            }
            S::If {
                condition,
                then,
                otherwise,
                ..
            } => {
                let condition = expression(condition, d, depth + 1)?;
                condition
                    .checked_add(statements(then, d, ids, depth + 1)?.max(statements(
                        otherwise,
                        d,
                        ids,
                        depth + 1,
                    )?))
                    .ok_or_else(|| invalid("step bound overflow"))?
            }
            S::ForRange {
                target,
                bounds,
                body,
                ..
            } => {
                let iterations = range_iterations(target, bounds)?;
                let base = bounds.iter().try_fold(0_usize, |count, item| {
                    count
                        .checked_add(expression(item, d, depth + 1)?)
                        .ok_or_else(|| invalid("step bound overflow"))
                })?;
                base.checked_add(
                    statements(body, d, ids, depth + 1)?
                        .checked_mul(iterations)
                        .ok_or_else(|| invalid("step bound overflow"))?,
                )
                .ok_or_else(|| invalid("step bound overflow"))?
            }
            S::Return { result, .. } => result
                .value
                .as_ref()
                .map_or(Ok(0), |item| expression(item, d, depth + 1))?,
            S::RaiseCallback {
                exception_class,
                message,
                ..
            } => {
                if exception_class.is_empty() {
                    return Err(invalid("raise_callback exception class is empty"));
                }
                expression(message, d, depth + 1)?
            }
            S::EmitObservation {
                channel, payload, ..
            } => {
                if channel != "strategy_stdout_json" {
                    return Err(invalid("observation channel is unsupported"));
                }
                expression(payload, d, depth + 1)?
            }
        };
        total = total
            .checked_add(cost + 1)
            .ok_or_else(|| invalid("step bound overflow"))?;
    }
    Ok(total)
}

fn validate_statement_depth(depth: usize) -> Result<(), ExecutableCallbackError> {
    if depth > 64 {
        Err(invalid("statement depth exceeds 64"))
    } else {
        Ok(())
    }
}

fn register_item_cost(
    register_id: &str,
    key: &crate::domain::CallbackExpression,
    value: &crate::domain::CallbackExpression,
    declarations: &Declarations<'_>,
    depth: usize,
) -> Result<usize, ExecutableCallbackError> {
    if !declarations.registers.contains(register_id) {
        return Err(invalid(
            "set_register_item references an undeclared register",
        ));
    }
    expression(key, declarations, depth + 1)?
        .checked_add(expression(value, declarations, depth + 1)?)
        .ok_or_else(|| invalid("step bound overflow"))
}

fn validate_predicates(
    values: &[String],
    d: &Declarations<'_>,
) -> Result<(), ExecutableCallbackError> {
    if values.iter().all(|id| d.predicates.contains(id)) {
        Ok(())
    } else {
        Err(invalid("instruction predicate is undeclared"))
    }
}

fn statement_identity(value: &S) -> (&str, &[String]) {
    match value {
        S::Let {
            id, predicate_ids, ..
        }
        | S::SetRegister {
            id, predicate_ids, ..
        }
        | S::SetRegisterItem {
            id, predicate_ids, ..
        }
        | S::SetCustomState {
            id, predicate_ids, ..
        }
        | S::DeleteCustomState {
            id, predicate_ids, ..
        }
        | S::If {
            id, predicate_ids, ..
        }
        | S::ForRange {
            id, predicate_ids, ..
        }
        | S::Return {
            id, predicate_ids, ..
        }
        | S::RaiseCallback {
            id, predicate_ids, ..
        }
        | S::EmitObservation {
            id, predicate_ids, ..
        } => (id, predicate_ids),
    }
}

pub(super) fn valid_id(value: &str, prefix: char) -> bool {
    value.strip_prefix(prefix).is_some_and(|tail| {
        !tail.is_empty() && !tail.starts_with('0') && tail.bytes().all(|byte| byte.is_ascii_digit())
    })
}
fn invalid(reason: impl Into<String>) -> ExecutableCallbackError {
    ExecutableCallbackError::InvalidExecutableCallbackProgram {
        reason: reason.into(),
    }
}
