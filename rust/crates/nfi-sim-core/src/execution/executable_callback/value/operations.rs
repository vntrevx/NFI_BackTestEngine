use std::{cmp::Ordering, collections::BTreeMap};

use num_traits::ToPrimitive;
use serde_json::{Number, Value};

use super::ValueFault;
use crate::domain::{
    CallbackBinaryOperator as B, CallbackBuiltin, CallbackCompareOperator as C,
    CallbackUnaryOperator as U,
};

pub(super) fn read(values: &BTreeMap<String, Value>, key: &str) -> Result<Value, ValueFault> {
    values.get(key).cloned().ok_or(ValueFault::Missing)
}
pub(super) fn indexed(value: &Value, index: &Value) -> Result<Value, ValueFault> {
    if let (Some(items), Some(index)) = (
        value.as_array(),
        index.as_u64().and_then(|item| usize::try_from(item).ok()),
    ) {
        return items.get(index).cloned().ok_or(ValueFault::Type);
    }
    if let (Some(object), Some(key)) = (value.as_object(), index.as_str()) {
        return object.get(key).cloned().ok_or(ValueFault::Missing);
    }
    Err(ValueFault::Type)
}
pub(super) fn unary(operator: U, value: &Value) -> Result<Value, ValueFault> {
    match operator {
        U::Not => Ok(Value::Bool(!truthy(value))),
        U::Neg => number(-numeric(value)?),
        U::Pos => number(numeric(value)?),
    }
}
pub(super) fn binary(operator: B, left: &Value, right: &Value) -> Result<Value, ValueFault> {
    if matches!(operator, B::Add) {
        if let (Some(left), Some(right)) = (left.as_str(), right.as_str()) {
            return Ok(Value::String([left, right].concat()));
        }
    }
    if let (Some(left), Some(right)) = (left.as_i64(), right.as_i64()) {
        let integer = match operator {
            B::Add => left.checked_add(right),
            B::Sub => left.checked_sub(right),
            B::Mul => left.checked_mul(right),
            B::Mod if right != 0 => left.checked_rem_euclid(right),
            _ => None,
        };
        if let Some(value) = integer {
            return Ok(Value::Number(value.into()));
        }
    }
    let (left, right) = (numeric(left)?, numeric(right)?);
    let value = match operator {
        B::Add => left + right,
        B::Sub => left - right,
        B::Mul => left * right,
        B::Div => left / right,
        B::Mod => left.rem_euclid(right),
    };
    number(value)
}
pub(super) fn compare(operator: C, left: &Value, right: &Value) -> Result<bool, ValueFault> {
    match operator {
        C::Eq | C::Is => Ok(equal(left, right)),
        C::Ne | C::IsNot => Ok(!equal(left, right)),
        C::Lt => ordered(left, right, |a, b| a < b),
        C::Le => ordered(left, right, |a, b| a <= b),
        C::Gt => ordered(left, right, |a, b| a > b),
        C::Ge => ordered(left, right, |a, b| a >= b),
        C::In | C::NotIn => {
            let found = match right {
                Value::Array(items) => items.iter().any(|item| equal(left, item)),
                Value::Object(object) => left.as_str().is_some_and(|key| object.contains_key(key)),
                Value::String(text) => left.as_str().is_some_and(|item| text.contains(item)),
                _ => return Err(ValueFault::Type),
            };
            Ok(if matches!(operator, C::NotIn) {
                !found
            } else {
                found
            })
        }
    }
}
fn equal(left: &Value, right: &Value) -> bool {
    match (numeric(left), numeric(right)) {
        (Ok(left), Ok(right)) => left.partial_cmp(&right).is_some_and(Ordering::is_eq),
        _ => left == right,
    }
}
fn ordered(
    left: &Value,
    right: &Value,
    operation: impl FnOnce(f64, f64) -> bool,
) -> Result<bool, ValueFault> {
    Ok(operation(numeric(left)?, numeric(right)?))
}
pub(super) fn builtin(name: CallbackBuiltin, mut values: Vec<Value>) -> Result<Value, ValueFault> {
    match name {
        CallbackBuiltin::Min | CallbackBuiltin::Max => {
            let mut numbers = values.drain(..).map(|value| numeric(&value));
            let first = numbers.next().ok_or(ValueFault::Type)??;
            number(numbers.try_fold(first, |result, value| {
                value.map(|value| {
                    if matches!(name, CallbackBuiltin::Min) {
                        result.min(value)
                    } else {
                        result.max(value)
                    }
                })
            })?)
        }
        CallbackBuiltin::Len => {
            let value = only(values)?;
            let length = match value {
                Value::String(value) => value.len(),
                Value::Array(value) => value.len(),
                Value::Object(value) => value.len(),
                _ => return Err(ValueFault::Type),
            };
            i64::try_from(length)
                .map(|value| Value::Number(value.into()))
                .map_err(|_| ValueFault::Arithmetic)
        }
        CallbackBuiltin::Int => {
            let value = numeric(&only(values)?)?
                .trunc()
                .to_i64()
                .ok_or(ValueFault::Arithmetic)?;
            Ok(Value::Number(value.into()))
        }
        CallbackBuiltin::Float => number(numeric(&only(values)?)?),
        CallbackBuiltin::Bool => Ok(Value::Bool(truthy(&only(values)?))),
    }
}
fn only(mut values: Vec<Value>) -> Result<Value, ValueFault> {
    if values.len() != 1 {
        return Err(ValueFault::Type);
    }
    values.pop().ok_or(ValueFault::Type)
}
fn numeric(value: &Value) -> Result<f64, ValueFault> {
    value
        .as_f64()
        .filter(|value| value.is_finite())
        .ok_or(ValueFault::Type)
}
fn number(value: f64) -> Result<Value, ValueFault> {
    Number::from_f64(value)
        .map(Value::Number)
        .ok_or(ValueFault::Arithmetic)
}
pub(super) fn finite(value: Value) -> Result<Value, ValueFault> {
    match &value {
        Value::Number(number) if number.as_f64().is_none_or(|value| !value.is_finite()) => {
            Err(ValueFault::Arithmetic)
        }
        _ => Ok(value),
    }
}
pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::equal;

    #[test]
    fn exact_numeric_equality_rejects_nearby_values_and_accepts_signed_zero() {
        assert!(equal(&json!(0.0), &json!(-0.0)));
        assert!(!equal(&json!(1.0), &json!(1.000_000_000_000_000_2)));
    }
}
