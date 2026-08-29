use std::collections::BTreeMap;

use serde_json::{Map, Value};

use super::types::CallbackInvocation;
use crate::domain::{CallbackExpression as E, CallbackRecordField};

#[path = "value/operations.rs"]
mod operations;
pub(crate) use operations::truthy;
use operations::{binary, builtin, compare, finite, indexed, read, unary};

#[derive(Debug, Clone, Copy)]
pub(super) enum ValueFault {
    Missing,
    Type,
    Arithmetic,
}

pub(super) struct Values<'a> {
    pub invocation: &'a CallbackInvocation,
    pub registers: &'a BTreeMap<String, Value>,
    pub custom: &'a BTreeMap<String, Value>,
    pub locals: &'a BTreeMap<String, Value>,
}

impl Values<'_> {
    pub fn evaluate(&mut self, expression: &E) -> Result<Value, ValueFault> {
        match expression {
            E::Literal { value } => finite(value.clone()),
            E::ReadInput { name } => read(&self.invocation.inputs, name),
            E::ReadLocal { name } => read(self.locals, name),
            E::ReadRegister { register_id } => read(self.registers, register_id),
            E::ReadTrade { field } => self
                .locals
                .get(&format!("trade.{field}"))
                .cloned()
                .map_or_else(|| read(&self.invocation.trade, field), Ok),
            E::ReadOrder { field } => read(&self.invocation.order, field),
            E::ReadCandle { field } => read(&self.invocation.candle, field),
            E::ReadWallet { field } => read(&self.invocation.wallet, field),
            E::ReadCustomState { key, default } => {
                if let Some(value) = self.custom.get(key) {
                    Ok(value.clone())
                } else {
                    self.evaluate(default)
                }
            }
            E::MapGet {
                value,
                key,
                default,
            } => {
                let map = self.evaluate(value)?;
                let key = map_key(&self.evaluate(key)?)?;
                if let Some(value) = map.as_object().and_then(|values| values.get(&key)) {
                    Ok(value.clone())
                } else {
                    self.evaluate(default)
                }
            }
            E::Record { fields } => self.record(fields),
            E::List { items } | E::Tuple { items } => Ok(Value::Array(
                items
                    .iter()
                    .map(|item| self.evaluate(item))
                    .collect::<Result<_, _>>()?,
            )),
            E::Index { value, index } => {
                let value = self.evaluate(value)?;
                let index = self.evaluate(index)?;
                indexed(&value, &index)
            }
            E::Unary { operator, value } => {
                let value = self.evaluate(value)?;
                unary(*operator, &value)
            }
            E::Binary {
                operator,
                left,
                right,
            } => {
                let left = self.evaluate(left)?;
                let right = self.evaluate(right)?;
                binary(*operator, &left, &right)
            }
            E::Compare { left, comparisons } => {
                let mut left = self.evaluate(left)?;
                for comparison in comparisons {
                    let right = self.evaluate(&comparison.right)?;
                    if !compare(comparison.operator, &left, &right)? {
                        return Ok(Value::Bool(false));
                    }
                    left = right;
                }
                Ok(Value::Bool(true))
            }
            E::And { values } => self.short_circuit(values, false),
            E::Or { values } => self.short_circuit(values, true),
            E::Choose {
                condition,
                then,
                otherwise,
            } => {
                if truthy(&self.evaluate(condition)?) {
                    self.evaluate(then)
                } else {
                    self.evaluate(otherwise)
                }
            }
            E::CallBuiltin { name, args } => {
                let values = args
                    .iter()
                    .map(|item| self.evaluate(item))
                    .collect::<Result<Vec<_>, _>>()?;
                builtin(*name, values)
            }
            E::TimestampMs { value } => {
                let value = self.evaluate(value)?;
                value
                    .as_i64()
                    .map(|item| Value::Number(item.into()))
                    .ok_or(ValueFault::Type)
            }
        }
    }

    fn record(&mut self, fields: &[CallbackRecordField]) -> Result<Value, ValueFault> {
        let mut values = Map::new();
        for field in fields {
            match field {
                CallbackRecordField::Named(field) => {
                    values.insert(field.name.clone(), self.evaluate(&field.value)?);
                }
                CallbackRecordField::Spread(field) => {
                    let spread = self
                        .evaluate(&field.spread)?
                        .as_object()
                        .cloned()
                        .ok_or(ValueFault::Type)?;
                    values.extend(spread);
                }
            }
        }
        Ok(Value::Object(values))
    }

    fn short_circuit(&mut self, expressions: &[E], any: bool) -> Result<Value, ValueFault> {
        if expressions.is_empty() {
            return Err(ValueFault::Type);
        }
        let mut last = Value::Null;
        for expression in expressions {
            last = self.evaluate(expression)?;
            if truthy(&last) == any {
                return Ok(last);
            }
        }
        Ok(last)
    }
}

pub(super) fn map_key(value: &Value) -> Result<String, ValueFault> {
    match value {
        Value::String(value) => Ok(value.clone()),
        Value::Number(value) => Ok(value.to_string()),
        _ => Err(ValueFault::Type),
    }
}
