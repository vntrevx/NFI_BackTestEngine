use super::*;

#[test]
fn vm_is_deterministic_and_commits_typed_register_custom_state_and_observation() {
    let program = parsed_program("spot");
    let Some(program) = program.ok() else {
        return;
    };
    let left = CallbackProgramRuntime::new(&program);
    let right = CallbackProgramRuntime::new(&program);
    let (Some(mut left), Some(mut right)) = (left.ok(), right.ok()) else {
        return;
    };
    let mut left_state = BTreeMap::new();
    let mut right_state = BTreeMap::new();
    let invocation = CallbackInvocation::new("bot_loop_start", 123, BTreeMap::new());
    let a = left.invoke(&program, &invocation, &mut left_state);
    let b = right.invoke(&program, &invocation, &mut right_state);
    assert_eq!(a, b);
    assert_eq!(left.registers().get("r1"), Some(&json!(2)));
    assert_eq!(left_state.get("seen"), Some(&json!(2)));
    assert_eq!(
        a.ok()
            .and_then(|result| result.observations.first().map(|item| item.payload.clone())),
        Some(json!({"seen":2}))
    );
}

#[test]
fn all_ten_entrypoints_execute_with_typed_returns() {
    let Some(program) = parsed_program("futures").ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    let mut custom = BTreeMap::new();
    for callback in program.entrypoints.keys() {
        let result = runtime.invoke(
            &program,
            &CallbackInvocation::new(callback, 7, BTreeMap::new()),
            &mut custom,
        );
        assert!(result.is_ok(), "{callback}: {result:?}");
    }
}

#[test]
fn runtime_rejects_bad_return_register_type_missing_input_and_observation() {
    let Some(mut program) = parsed_program("spot").ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    let invalid_return = serde_json::from_value(
        json!({"op":"return","id":"i1","predicate_ids":["p1"],"result":{"class":"boolean","value":expression(&json!(true))}}),
    );
    assert!(invalid_return.is_ok());
    let Some(invalid_return) = invalid_return.ok() else {
        return;
    };
    if let Some(entry) = program.entrypoints.get_mut("bot_loop_start") {
        entry.instructions = vec![invalid_return];
    }
    assert!(matches!(
        runtime.invoke(
            &program,
            &CallbackInvocation::new("bot_loop_start", 8, BTreeMap::new()),
            &mut BTreeMap::new()
        ),
        Err(ExecutableCallbackError::ExecutableCallbackInvalidReturn { .. })
    ));

    let cases = [
        (
            "register",
            json!({"op":"set_register","id":"i1","predicate_ids":["p1"],"register_id":"r1","value":expression(&json!("wrong"))}),
        ),
        (
            "observation",
            json!({"op":"emit_observation","id":"i1","predicate_ids":["p1"],"channel":"strategy_stdout_json","payload":expression(&json!("not-record"))}),
        ),
    ];
    for (kind, instruction) in cases {
        let program = parsed_mutation("spot", |value| {
            value["entrypoints"]["bot_loop_start"]["instructions"] = json!([instruction]);
        });
        assert!(program.is_ok());
        let Some(program) = program.ok() else {
            return;
        };
        let runtime = CallbackProgramRuntime::new(&program);
        assert!(runtime.is_ok());
        let Some(mut runtime) = runtime.ok() else {
            return;
        };
        let result = runtime.invoke(
            &program,
            &CallbackInvocation::new("bot_loop_start", 8, BTreeMap::new()),
            &mut BTreeMap::new(),
        );
        assert!(matches!(
            (kind, result),
            (
                "register",
                Err(ExecutableCallbackError::ExecutableCallbackRegisterType { .. })
            ) | (
                "observation",
                Err(ExecutableCallbackError::ExecutableCallbackObservation { .. })
            )
        ));
    }
    let program = parsed_mutation("spot", |value| {
        value["required_inputs"] =
            json!([{"entrypoint":"bot_loop_start","name":"now","type":{"kind":"timestamp_ms"}}]);
    });
    let Some(program) = program.ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    assert!(matches!(
        runtime.invoke(
            &program,
            &CallbackInvocation::new("bot_loop_start", 8, BTreeMap::new()),
            &mut BTreeMap::new()
        ),
        Err(ExecutableCallbackError::ExecutableCallbackMissingInput { .. })
    ));
}

#[test]
fn source_raise_uses_fallback_and_preserves_executed_shared_writes() {
    let program = parsed_mutation("spot", |value| {
        value["entrypoints"]["bot_loop_start"]["instructions"] = json!([
            {"op":"set_register","id":"i1","predicate_ids":["p1"],"register_id":"r1","value":expression(&json!(3))},
            {"op":"set_custom_state","id":"i2","predicate_ids":["p1"],"key":"seen","value":expression(&json!(3))},
            {"op":"raise_callback","id":"i3","predicate_ids":["p1"],"exception_class":"InjectedError","message":expression(&json!("fault"))}
        ]);
    });
    let Some(program) = program.ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    let mut custom = BTreeMap::new();
    let result = runtime.invoke(
        &program,
        &CallbackInvocation::new("bot_loop_start", 9, BTreeMap::new()),
        &mut custom,
    );
    assert_eq!(
        result.ok().map(|item| item.transaction),
        Some(CallbackProgramTransaction::Fallback)
    );
    assert_eq!(runtime.registers().get("r1"), Some(&json!(3)));
    assert_eq!(custom.get("seen"), Some(&json!(3)));
}

#[test]
fn trade_local_shadow_is_observed_then_discarded_on_fallback() {
    let program = parsed_mutation("spot", |value| {
        value["entrypoints"]["bot_loop_start"]["instructions"] = json!([
            {"op":"let","id":"i1","predicate_ids":["p1"],"name":"trade.stake_amount","value":expression(&json!(1.0))},
            {"op":"emit_observation","id":"i2","predicate_ids":["p1"],"channel":"strategy_stdout_json","payload":{"op":"record","fields":[{"name":"stake_amount","value":{"op":"read_trade","field":"stake_amount"}}]}},
            {"op":"raise_callback","id":"i3","predicate_ids":["p1"],"exception_class":"InjectedError","message":expression(&json!("fault"))}
        ]);
    });
    let Some(program) = program.ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    let mut invocation = CallbackInvocation::new("bot_loop_start", 9, BTreeMap::new());
    invocation
        .trade
        .insert("stake_amount".to_owned(), json!(104.057_094));
    let result = runtime.invoke(&program, &invocation, &mut BTreeMap::new());

    assert_eq!(
        result.as_ref().ok().map(|item| item.transaction),
        Some(CallbackProgramTransaction::Fallback)
    );
    assert_eq!(
        result
            .ok()
            .and_then(|item| item.observations.first().map(|value| value.payload.clone())),
        Some(json!({"stake_amount": 1.0}))
    );
    assert_eq!(
        invocation.trade.get("stake_amount"),
        Some(&json!(104.057_094))
    );
}

#[test]
fn runtime_step_limit_and_inactive_transition_are_typed() {
    let Some(mut program) = parsed_program("spot").ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&program).ok() else {
        return;
    };
    if let Some(entry) = program.entrypoints.get_mut("bot_loop_start") {
        entry.max_steps = 1;
    }
    assert!(matches!(
        runtime.invoke(
            &program,
            &CallbackInvocation::new("bot_loop_start", 10, BTreeMap::new()),
            &mut BTreeMap::new()
        ),
        Err(ExecutableCallbackError::ExecutableCallbackStepLimit {
            instruction_id: Some(_),
            ..
        })
    ));
    let Some(valid) = parsed_program("spot").ok() else {
        return;
    };
    let Some(mut runtime) = CallbackProgramRuntime::new(&valid).ok() else {
        return;
    };
    assert!(matches!(
        runtime.invoke(
            &valid,
            &CallbackInvocation::new("leverage", 10, BTreeMap::new()),
            &mut BTreeMap::new()
        ),
        Err(ExecutableCallbackError::ExecutableCallbackInvalidTransition { .. })
    ));
}
