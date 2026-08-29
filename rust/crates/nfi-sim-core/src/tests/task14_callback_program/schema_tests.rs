use super::*;

#[test]
fn strict_schema_rejects_missing_and_unknown_fields() {
    let mut unknown = program_json("spot");
    unknown["unknown"] = json!(true);
    assert!(serde_json::from_value::<ExecutableCallbackProgram>(unknown).is_err());
    let mut missing = program_json("spot");
    if let Some(object) = missing.as_object_mut() {
        object.remove("registers");
    }
    assert!(serde_json::from_value::<ExecutableCallbackProgram>(missing).is_err());
}

#[test]
fn complete_spot_program_preflights_and_leverage_is_inactive() {
    let program = parsed_program("spot");
    assert!(program.is_ok());
    let Some(program) = program.ok() else {
        return;
    };
    assert_eq!(validate_executable_callback_program(&program), Ok(()));
    assert_eq!(program.entrypoints.len(), 10);
    assert_eq!(
        program
            .entrypoints
            .get("leverage")
            .map(|entry| entry.active),
        Some(false)
    );
}

#[test]
fn fingerprint_is_canonical_and_detects_stale_identity() {
    let mut value = program_json("futures");
    let first = executable_callback_fingerprint_value(&value);
    let reparsed = serde_json::to_string(&value)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok());
    let second = reparsed
        .as_ref()
        .and_then(|item| executable_callback_fingerprint_value(item).ok());
    assert_eq!(first.ok(), second);
    let Some(identity) = value.get_mut("identity") else {
        return;
    };
    identity["program_fingerprint"] = json!(hash('9'));
    let stale = serde_json::from_value::<ExecutableCallbackProgram>(value);
    assert!(stale.ok().is_some_and(|program| matches!(
        validate_executable_callback_program(&program),
        Err(ExecutableCallbackError::ExecutableCallbackIdentityMismatch { .. })
    )));
}

#[test]
fn preflight_rejects_coexistence_and_legacy_absence_is_unchanged() {
    let mut config = config(1);
    assert!(config.executable_callback_program.is_none());
    assert_eq!(validate_executable_callback_config(&config), Ok(()));
    let Some(program) = parsed_program("spot").ok() else {
        return;
    };
    config.executable_callback_program = Some(program);
    config.state_machine_program = serde_json::from_value(json!({
        "schema_version":"state-machine-program-v1","entrypoints":{},"required_reads":[],"required_columns":[],"required_state_keys":[],"opcodes":[],"source_map":{}
    })).ok();
    assert!(matches!(
        validate_executable_callback_config(&config),
        Err(ExecutableCallbackError::InvalidExecutableCallbackProgram { .. })
    ));
}
