//! Rebuy adjustment and callback-version validation.

use super::{
    shared, valid_scalar_program, CompiledAdjustmentExecutionMode, CompiledAdjustmentOperation,
    CompiledAdjustmentProgram, NfiX7AdjustmentPolicy,
};

pub(super) fn valid_versioned_rebuy_program(
    schema_version: &str,
    program: Option<&CompiledAdjustmentProgram>,
    delegate_policy: Option<&NfiX7AdjustmentPolicy>,
    delegate_source_callback: Option<&str>,
) -> bool {
    if !matches!(
        schema_version,
        "0.22.0"
            | "0.23.0"
            | "0.24.0"
            | "0.25.0"
            | "0.26.0"
            | "0.27.0"
            | "0.28.0"
            | "0.29.0"
            | "0.30.0"
            | "0.31.0"
    ) {
        return program.is_none();
    }
    let Some(program) = program else {
        return false;
    };
    program.schema_version == "adjustment-transition-program-v1"
        && program.execution_mode == CompiledAdjustmentExecutionMode::Primary
        && program.source_order
            == [
                CompiledAdjustmentOperation::Delegate,
                CompiledAdjustmentOperation::Decision,
            ]
        && program.order_scan.cluster_order_side != program.order_scan.boundary_order_side
        && program.order_scan.exclude_first_order
        && !program.delegate.tag.is_empty()
        && !program.delegate.source_target.is_empty()
        && program.delegate.target_entry_retry_ms > 0
        && delegate_policy
            .is_some_and(|policy| policy.entry_retry_ms == program.delegate.target_entry_retry_ms)
        && delegate_source_callback == Some(program.delegate.source_target.as_str())
        && program.input_contract.is_object()
        && program.location.line > 0
        && program.location.end_line >= program.location.line
        && program.delegate.location.line > 0
        && program.delegate.location.end_line >= program.delegate.location.line
        && shared::valid_sha256(&program.fingerprint)
        && valid_scalar_program(&program.decision_program)
}

pub(super) fn valid_adjustment_source_callback(
    schema_version: &str,
    callback: Option<&str>,
) -> bool {
    if matches!(
        schema_version,
        "0.22.0"
            | "0.23.0"
            | "0.24.0"
            | "0.25.0"
            | "0.26.0"
            | "0.27.0"
            | "0.28.0"
            | "0.29.0"
            | "0.30.0"
            | "0.31.0"
    ) {
        callback.is_some_and(|value| !value.is_empty())
    } else {
        callback.is_none()
    }
}
