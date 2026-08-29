use std::collections::BTreeSet;

use crate::domain::{
    CallbackTradingMode, CallbackType, ExecutableCallbackError, ExecutableCallbackProgram,
};

use super::super::validation_tree::{initial_expression, valid_id, validate_entry, Declarations};
use super::policies::{
    accepted_returns, expected_cadence, valid_fallback, valid_order, valid_statement_returns,
};
use super::{invalid, is_hash, ENTRYPOINTS};

pub(super) fn validate_declarations(
    program: &ExecutableCallbackProgram,
) -> Result<(), ExecutableCallbackError> {
    if program.registers.len() > 256 {
        return Err(invalid("register count exceeds 256"));
    }
    let mut registers = BTreeSet::new();
    let mut logical = BTreeSet::new();
    for register in &program.registers {
        if !valid_id(&register.id, 'r')
            || !registers.insert(register.id.clone())
            || !is_hash(&register.logical_name_hash)
            || !logical.insert(register.logical_name_hash.clone())
            || !initial_expression(&register.initial)
        {
            return Err(invalid("register declaration is invalid"));
        }
        validate_type(&register.value_type, 0)?;
    }
    let mut custom = BTreeSet::new();
    for requirement in &program.required_custom_state {
        if requirement.key.is_empty() || !custom.insert(requirement.key.clone()) {
            return Err(invalid("custom state declaration is invalid"));
        }
        if let Some(value_type) = &requirement.value_type {
            validate_type(value_type, 0)?;
        }
    }
    let mut inputs = BTreeSet::new();
    for requirement in &program.required_inputs {
        if !ENTRYPOINTS.contains(&requirement.entrypoint.as_str())
            || requirement.name.is_empty()
            || !inputs.insert((requirement.entrypoint.clone(), requirement.name.clone()))
        {
            return Err(invalid("required input declaration is invalid"));
        }
        validate_type(&requirement.value_type, 0)?;
    }
    Ok(())
}

pub(super) fn validate_entrypoints(
    program: &ExecutableCallbackProgram,
) -> Result<(), ExecutableCallbackError> {
    if program.entrypoints.len() != ENTRYPOINTS.len()
        || ENTRYPOINTS
            .iter()
            .any(|name| !program.entrypoints.contains_key(*name))
    {
        return Err(invalid("program must contain exactly ten entrypoints"));
    }
    let registers: BTreeSet<String> = program
        .registers
        .iter()
        .map(|item| item.id.clone())
        .collect();
    let custom: BTreeSet<String> = program
        .required_custom_state
        .iter()
        .map(|item| item.key.clone())
        .collect();
    let inputs: BTreeSet<(String, String)> = program
        .required_inputs
        .iter()
        .map(|item| (item.entrypoint.clone(), item.name.clone()))
        .collect();
    let predicates: BTreeSet<String> = program
        .identity
        .source_predicates
        .iter()
        .map(|item| item.id.clone())
        .collect();
    let mut ids = BTreeSet::new();
    for name in ENTRYPOINTS {
        let Some(entry) = program.entrypoints.get(name) else {
            return Err(invalid("entrypoint is missing"));
        };
        if entry.name != name
            || entry.cadence != expected_cadence(name)
            || entry.visibility.callback_dataframe_completed_candle_lag != 2
            || entry.visibility.signal_row_offset != -1
            || entry.accepted_returns != accepted_returns(name)
            || entry.instructions.is_empty()
            || !valid_order(
                name,
                entry.order.phase,
                &entry.order.after,
                &entry.order.before,
            )
            || !valid_fallback(name, &entry.exception_fallback)
            || !valid_statement_returns(name, &entry.instructions)
            || entry
                .predicate_ids
                .iter()
                .any(|id| !predicates.contains(id))
            || entry
                .order
                .after
                .iter()
                .chain(&entry.order.before)
                .any(|item| item == name || !ENTRYPOINTS.contains(&item.as_str()))
        {
            return Err(invalid(format!("entrypoint {name} policy is invalid")));
        }
        let should_be_active =
            program.identity.trading_mode == CallbackTradingMode::Futures || name != "leverage";
        if entry.active != should_be_active {
            return Err(invalid(format!("entrypoint {name} activity is invalid")));
        }
        let declarations = Declarations {
            callback: name,
            registers: &registers,
            custom: &custom,
            inputs: &inputs,
            predicates: &predicates,
        };
        validate_entry(entry, &declarations, &mut ids)?;
    }
    Ok(())
}

fn validate_type(value: &CallbackType, depth: usize) -> Result<(), ExecutableCallbackError> {
    if depth > 64 {
        return Err(invalid("type depth exceeds 64"));
    }
    match value {
        CallbackType::List { item, max_length } => {
            if max_length.is_some_and(|limit| limit > 65_536) {
                return Err(invalid("list type bound exceeds 65536"));
            }
            item.as_ref()
                .map_or(Ok(()), |item| validate_type(item, depth + 1))
        }
        CallbackType::Record { fields } => {
            let Some(fields) = fields else {
                return Ok(());
            };
            if fields.len() > 128 || fields.iter().any(|field| field.name.is_empty()) {
                return Err(invalid("record type is invalid"));
            }
            fields
                .iter()
                .try_for_each(|field| validate_type(&field.value_type, depth + 1))
        }
        _ => Ok(()),
    }
}
