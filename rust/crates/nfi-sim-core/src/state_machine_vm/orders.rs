//! Finite, source-ordered trade order collection execution.

use serde_json::Value;

use crate::domain::{
    StateMachineExpression, StateMachineInstruction, StateMachineOrderSelector,
    StateMachineOrderValueType,
};

use super::{Machine, StateMachineAction, StateMachineError};

impl Machine<'_> {
    pub(super) fn execute_order_loop(
        &mut self,
        variable: &str,
        collection: &StateMachineExpression,
        instruction_limit: usize,
        instructions: &[StateMachineInstruction],
    ) -> Result<Option<StateMachineAction>, StateMachineError> {
        let orders = self
            .expression(collection)?
            .as_array()
            .cloned()
            .ok_or(StateMachineError::InvalidType)?;
        let program_limit = self
            .max_order_iterations
            .ok_or(StateMachineError::InvalidLoop)?;
        if orders.len() > instruction_limit || orders.len() > program_limit {
            return Err(StateMachineError::InvalidLoop);
        }
        if orders.iter().any(|order| !order.is_object()) {
            return Err(StateMachineError::InvalidType);
        }
        for order in orders {
            self.locals.insert(variable.to_owned(), order);
            if let Some(action) = self.execute(instructions)? {
                return Ok(Some(action));
            }
        }
        Ok(None)
    }

    pub(super) fn order_collection(
        &self,
        selector: StateMachineOrderSelector,
    ) -> Result<Value, StateMachineError> {
        let key = match selector {
            StateMachineOrderSelector::All => "filled",
            StateMachineOrderSelector::EntrySide => "filled_entries",
            StateMachineOrderSelector::ExitSide => "filled_exits",
        };
        let value = self
            .context
            .orders
            .get(key)
            .ok_or(StateMachineError::MissingRead)?;
        if !value.is_array() {
            return Err(StateMachineError::InvalidType);
        }
        Ok(value.clone())
    }

    pub(super) fn order_field(
        &mut self,
        order: &StateMachineExpression,
        field: &str,
        value_type: StateMachineOrderValueType,
    ) -> Result<Value, StateMachineError> {
        if self.order_field_types.get(field) != Some(&value_type) {
            return Err(StateMachineError::InvalidType);
        }
        let order = self.expression(order)?;
        let value = order
            .as_object()
            .and_then(|order| order.get(field))
            .ok_or(StateMachineError::MissingRead)?;
        if !value_matches_type(value, value_type) {
            return Err(StateMachineError::InvalidType);
        }
        Ok(value.clone())
    }
}

fn value_matches_type(value: &Value, expected: StateMachineOrderValueType) -> bool {
    match expected {
        StateMachineOrderValueType::Number => value.is_number(),
        StateMachineOrderValueType::NumberOrNull => value.is_number() || value.is_null(),
        StateMachineOrderValueType::String => value.is_string(),
        StateMachineOrderValueType::StringOrNull => value.is_string() || value.is_null(),
        StateMachineOrderValueType::TimestampMs => value.as_i64().is_some(),
    }
}
