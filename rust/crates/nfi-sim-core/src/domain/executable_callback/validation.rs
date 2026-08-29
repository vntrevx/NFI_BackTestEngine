use std::collections::BTreeSet;

use super::validation_tree::valid_id;
use super::{
    executable_callback_fingerprint, CallbackRunMode, CallbackTradingMode, ExecutableCallbackError,
    ExecutableCallbackIdentity, ExecutableCallbackProgram,
};
use crate::domain::PortfolioConfig;

#[path = "validation/entries.rs"]
mod entries;
#[path = "validation/policies.rs"]
mod policies;
use entries::{validate_declarations, validate_entrypoints};

const CONTRACT_FILE_SHA256: &str =
    "a2cd2bf7ea60b131885122a2b5a308ba64f610942ce3869fda08c6dc3a258576";
const CONTRACT_FINGERPRINT: &str =
    "7c26cbaea6853a20b93932dbc0f3bc788cf0d43e58f243e9985029a727d6ec7f";
const ENTRYPOINTS: [&str; 10] = [
    "bot_loop_start",
    "leverage",
    "custom_stake_amount",
    "confirm_trade_entry",
    "order_filled",
    "adjust_trade_position",
    "custom_stoploss",
    "custom_exit",
    "confirm_trade_exit",
    "loop_cadence_startup_lookback",
];

#[derive(Debug, Clone, Default)]
pub struct ExecutableCallbackIdentitySeal {
    pub callback_execution_ir_fingerprint: Option<String>,
    pub selected_class_ast_sha256: Option<String>,
    pub source_ids: BTreeSet<String>,
    pub run_mode: Option<CallbackRunMode>,
    pub trading_mode: Option<CallbackTradingMode>,
}

/// Validates an executable callback program against the wire-contract invariants.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] for malformed programs,
/// or [`ExecutableCallbackError::ExecutableCallbackIdentityMismatch`] for invalid identity data.
pub fn validate_executable_callback_program(
    program: &ExecutableCallbackProgram,
) -> Result<(), ExecutableCallbackError> {
    if program.schema_version != "executable-callback-program-v1" {
        return Err(invalid("unsupported schema_version"));
    }
    let bytes = serde_json::to_vec(program).map_err(|error| invalid(error.to_string()))?;
    if bytes.len() > 1_048_576 {
        return Err(invalid("program exceeds 1048576 bytes"));
    }
    validate_identity(&program.identity)?;
    let fingerprint = executable_callback_fingerprint(program)?;
    if fingerprint != program.identity.program_fingerprint {
        return Err(identity("program_fingerprint"));
    }
    validate_declarations(program)?;
    validate_entrypoints(program)
}

/// Validates an executable callback program and verifies that it matches a runtime identity seal.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] for malformed programs,
/// or [`ExecutableCallbackError::ExecutableCallbackIdentityMismatch`] when the seal differs.
pub fn validate_executable_callback_identity(
    program: &ExecutableCallbackProgram,
    seal: &ExecutableCallbackIdentitySeal,
) -> Result<(), ExecutableCallbackError> {
    validate_executable_callback_program(program)?;
    let identity_value = &program.identity;
    let source_ids = identity_value
        .source_closure
        .iter()
        .map(|item| item.source_id.clone())
        .collect::<BTreeSet<_>>();
    for (name, mismatch) in [
        (
            "callback_execution_ir_fingerprint",
            seal.callback_execution_ir_fingerprint
                .as_ref()
                .is_some_and(|value| value != &identity_value.callback_execution_ir_fingerprint),
        ),
        (
            "selected_class_ast_sha256",
            seal.selected_class_ast_sha256
                .as_ref()
                .is_some_and(|value| value != &identity_value.selected_class_ast_sha256),
        ),
        (
            "source_ids",
            !seal.source_ids.is_empty() && seal.source_ids != source_ids,
        ),
        (
            "run_mode",
            seal.run_mode
                .is_some_and(|value| value != identity_value.run_mode),
        ),
        (
            "trading_mode",
            seal.trading_mode
                .is_some_and(|value| value != identity_value.trading_mode),
        ),
    ] {
        if mismatch {
            return Err(identity(name));
        }
    }
    Ok(())
}

/// Validates the executable callback configuration for a portfolio.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] when callback program
/// configuration is invalid, or [`ExecutableCallbackError::ExecutableCallbackIdentityMismatch`]
/// when its trading mode differs from the portfolio mode.
pub fn validate_executable_callback_config(
    config: &PortfolioConfig,
) -> Result<(), ExecutableCallbackError> {
    let Some(program) = config.executable_callback_program.as_ref() else {
        return Ok(());
    };
    if config.callback_program.is_some()
        || config.state_machine_program.is_some()
        || config.stake_program.is_some()
        || config.entry_confirmation_program.is_some()
        || config.exit_confirmation_program.is_some()
        || config.custom_exit_program.is_some()
        || config.adjust_trade_position_program.is_some()
    {
        return Err(invalid(
            "executable callback program cannot coexist with fragmented callback programs",
        ));
    }
    let expected = if config.is_futures {
        CallbackTradingMode::Futures
    } else {
        CallbackTradingMode::Spot
    };
    if program.identity.trading_mode != expected {
        return Err(identity("trading_mode"));
    }
    validate_executable_callback_program(program)
}

fn validate_identity(value: &ExecutableCallbackIdentity) -> Result<(), ExecutableCallbackError> {
    if value.callback_contract_file_sha256 != CONTRACT_FILE_SHA256 {
        return Err(identity("callback_contract_file_sha256"));
    }
    if value.callback_contract_fingerprint != CONTRACT_FINGERPRINT {
        return Err(identity("callback_contract_fingerprint"));
    }
    for (name, hash) in [
        (
            "callback_execution_ir_fingerprint",
            &value.callback_execution_ir_fingerprint,
        ),
        ("program_fingerprint", &value.program_fingerprint),
        (
            "selected_class_ast_sha256",
            &value.selected_class_ast_sha256,
        ),
    ] {
        if !is_hash(hash) {
            return Err(identity(name));
        }
    }
    if value.source_closure.is_empty() {
        return Err(invalid("source_closure is empty"));
    }
    let mut methods = BTreeSet::new();
    for source in &value.source_closure {
        if !is_hash(&source.ast_sha256)
            || !is_hash(&source.source_body_sha256)
            || !source
                .source_id
                .strip_prefix("sha256:")
                .is_some_and(is_hash)
            || source.logical_method_id.is_empty()
            || source.logical_owner_id.is_empty()
            || !methods.insert(source.logical_method_id.clone())
        {
            return Err(invalid("source_closure identity is invalid"));
        }
    }
    let mut predicates = BTreeSet::new();
    for predicate in &value.source_predicates {
        if !valid_id(&predicate.id, 'p')
            || !predicates.insert(predicate.id.clone())
            || !is_hash(&predicate.ast_sha256)
            || predicate.expression.is_empty()
            || !methods.contains(&predicate.producer_method_id)
        {
            return Err(invalid("source predicate identity is invalid"));
        }
    }
    Ok(())
}

fn is_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn invalid(reason: impl Into<String>) -> ExecutableCallbackError {
    ExecutableCallbackError::InvalidExecutableCallbackProgram {
        reason: reason.into(),
    }
}
fn identity(field: impl Into<String>) -> ExecutableCallbackError {
    ExecutableCallbackError::ExecutableCallbackIdentityMismatch {
        field: field.into(),
    }
}
