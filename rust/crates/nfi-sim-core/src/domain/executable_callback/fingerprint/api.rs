use serde_json::Value;

use super::sha256::sha256;
use crate::domain::executable_callback::{ExecutableCallbackError, ExecutableCallbackProgram};

/// Calculates the canonical fingerprint for an executable callback program.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] when the program cannot
/// be serialized to its JSON representation.
pub fn digest(program: &ExecutableCallbackProgram) -> Result<String, ExecutableCallbackError> {
    let value = serde_json::to_value(program).map_err(|error| invalid(error.to_string()))?;
    fingerprint_value(&value)
}

/// Calculates the SHA-256 digest of serde JSON bytes as lowercase hexadecimal.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] when JSON serialization
/// of `value` fails.
pub fn canonical_callback_json_sha256(value: &Value) -> Result<String, ExecutableCallbackError> {
    let bytes = serde_json::to_vec(value).map_err(|error| invalid(error.to_string()))?;
    Ok(hex(&sha256(&bytes)))
}

/// Calculates an executable callback program fingerprint from a JSON value.
///
/// Diagnostic paths and the program fingerprint field itself are omitted before hashing.
///
/// # Errors
///
/// Returns [`ExecutableCallbackError::InvalidExecutableCallbackProgram`] when `identity` or its
/// `program_fingerprint` field is absent, or when canonical JSON serialization fails.
pub fn fingerprint_value(value: &Value) -> Result<String, ExecutableCallbackError> {
    let mut canonical = value.clone();
    omit_diagnostics(&mut canonical);
    let identity = canonical
        .get_mut("identity")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| invalid("identity is missing"))?;
    if identity.remove("program_fingerprint").is_none() {
        return Err(invalid("program_fingerprint is missing"));
    }
    canonical_callback_json_sha256(&canonical)
}

fn omit_diagnostics(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.remove("diagnostic_path");
            for child in object.values_mut() {
                omit_diagnostics(child);
            }
        }
        Value::Array(array) => array.iter_mut().for_each(omit_diagnostics),
        _ => {}
    }
}

fn invalid(reason: impl Into<String>) -> ExecutableCallbackError {
    ExecutableCallbackError::InvalidExecutableCallbackProgram {
        reason: reason.into(),
    }
}

fn hex(bytes: &[u8; 32]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(64);
    for byte in bytes {
        output.push(char::from(DIGITS[usize::from(byte >> 4)]));
        output.push(char::from(DIGITS[usize::from(byte & 0x0f)]));
    }
    output
}
