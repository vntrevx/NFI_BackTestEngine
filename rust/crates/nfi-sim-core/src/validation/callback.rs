//! Callback-program structural validation.

use serde_json::Value;

use crate::domain::{CallbackProgram, CustomDataWrite, SimError};

pub(crate) fn validate_callback_program(program: &CallbackProgram) -> Result<(), SimError> {
    let Some(order_filled) = &program.order_filled else {
        return Ok(());
    };
    if order_filled.initial_successful_entry_writes.is_empty()
        || order_filled
            .initial_successful_entry_writes
            .iter()
            .any(invalid_custom_write)
        || order_filled.order_tag_actions.iter().any(|(tag, writes)| {
            tag.is_empty() || writes.is_empty() || writes.iter().any(invalid_custom_write)
        })
    {
        return Err(SimError::InvalidCallbackProgram);
    }
    Ok(())
}

fn invalid_custom_write(write: &CustomDataWrite) -> bool {
    write.key.is_empty()
        || !matches!(
            write.value,
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_)
        )
}
