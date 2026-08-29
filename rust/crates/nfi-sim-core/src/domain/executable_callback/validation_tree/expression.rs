use serde_json::Value;

use super::{invalid, Declarations};
use crate::domain::{
    CallbackExpression as E, CallbackRecordField, CallbackType, ExecutableCallbackError,
};

pub(super) fn expression(
    value: &E,
    d: &Declarations<'_>,
    depth: usize,
) -> Result<usize, ExecutableCallbackError> {
    if depth > 64 {
        return Err(invalid("expression depth exceeds 64"));
    }
    let children: Vec<&E> = match value {
        E::Literal { value } => {
            finite_json(value)?;
            Vec::new()
        }
        E::ReadRegister { register_id } => {
            if !d.registers.contains(register_id) {
                return Err(invalid("read_register is undeclared"));
            }
            Vec::new()
        }
        E::ReadCustomState { key, default } => {
            if !d.custom.contains(key) {
                return Err(invalid("read_custom_state key is undeclared"));
            }
            vec![default]
        }
        E::MapGet {
            value,
            key,
            default,
        } => vec![value, key, default],
        E::ReadInput { name } => {
            if !d.inputs.contains(&(d.callback.to_owned(), name.clone())) {
                return Err(invalid("read_input is undeclared for this entrypoint"));
            }
            Vec::new()
        }
        E::ReadLocal { name } => {
            if name.is_empty() {
                return Err(invalid("read name is empty"));
            }
            Vec::new()
        }
        E::ReadTrade { field }
        | E::ReadOrder { field }
        | E::ReadCandle { field }
        | E::ReadWallet { field } => {
            if field.is_empty() {
                return Err(invalid("read field is empty"));
            }
            Vec::new()
        }
        E::Record { fields } => {
            if fields.len() > 128 {
                return Err(invalid("record fields are invalid"));
            }
            fields
                .iter()
                .map(|field| match field {
                    CallbackRecordField::Named(field) => &field.value,
                    CallbackRecordField::Spread(field) => &field.spread,
                })
                .collect()
        }
        E::List { items }
        | E::Tuple { items }
        | E::And { values: items }
        | E::Or { values: items } => items.iter().collect(),
        E::Index { value, index }
        | E::Binary {
            left: value,
            right: index,
            ..
        } => vec![value, index],
        E::Compare { left, comparisons } => {
            let mut children = vec![left.as_ref()];
            children.extend(comparisons.iter().map(|item| &item.right));
            children
        }
        E::Unary { value, .. } | E::TimestampMs { value } => vec![value],
        E::Choose {
            condition,
            then,
            otherwise,
        } => vec![condition, then, otherwise],
        E::CallBuiltin { args, .. } => args.iter().collect(),
    };
    children
        .into_iter()
        .try_for_each(|child| expression(child, d, depth + 1).map(|_| ()))?;
    Ok(0)
}

pub(crate) fn matches_type(value: &Value, expected: &CallbackType) -> bool {
    match expected {
        CallbackType::Bool => value.is_boolean(),
        CallbackType::I64 | CallbackType::TimestampMs => value.as_i64().is_some(),
        CallbackType::F64 => value.as_f64().is_some_and(f64::is_finite),
        CallbackType::String => value.is_string(),
        CallbackType::Null => value.is_null(),
        CallbackType::List { item, max_length } => value.as_array().is_some_and(|items| {
            max_length.is_none_or(|limit| items.len() <= limit)
                && item
                    .as_ref()
                    .is_none_or(|kind| items.iter().all(|value| matches_type(value, kind)))
        }),
        CallbackType::Record { fields } => value.as_object().is_some_and(|object| {
            fields.as_ref().is_none_or(|fields| {
                object.len() == fields.len()
                    && fields.iter().all(|field| {
                        object
                            .get(&field.name)
                            .is_some_and(|value| matches_type(value, &field.value_type))
                    })
            })
        }),
    }
}

pub(crate) fn initial_expression(value: &E) -> bool {
    match value {
        E::Literal { value } => finite_json(value).is_ok(),
        E::Record { fields } => fields.iter().all(|field| match field {
            CallbackRecordField::Named(field) => initial_expression(&field.value),
            CallbackRecordField::Spread(field) => initial_expression(&field.spread),
        }),
        E::List { items } | E::Tuple { items } => items.iter().all(initial_expression),
        _ => false,
    }
}

pub(super) fn range_iterations(
    target: &str,
    bounds: &[E],
) -> Result<usize, ExecutableCallbackError> {
    if target.is_empty() || bounds.len() != 2 {
        return Err(invalid("for_range bound is invalid"));
    }
    let values = bounds
        .iter()
        .map(|item| match item {
            E::Literal { value } => value.as_i64(),
            _ => None,
        })
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| invalid("for_range bounds must be literal i64"))?;
    values[1]
        .checked_sub(values[0])
        .and_then(|value| usize::try_from(value).ok())
        .filter(|value| *value <= 4096)
        .ok_or_else(|| invalid("for_range bound is invalid"))
}

fn finite_json(value: &Value) -> Result<(), ExecutableCallbackError> {
    match value {
        Value::Number(number) if number.as_f64().is_none_or(|value| !value.is_finite()) => {
            Err(invalid("numeric literal is not finite"))
        }
        Value::Array(items) => items.iter().try_for_each(finite_json),
        Value::Object(object) => {
            if object.len() > 128 {
                return Err(invalid("literal record exceeds 128 fields"));
            }
            object.values().try_for_each(finite_json)
        }
        _ => Ok(()),
    }
}
