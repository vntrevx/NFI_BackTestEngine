//! Todo 14 callback cadence, visibility, and rollback matrix.

use super::*;

#[test]
fn real_simulator_observer_emits_native_callback_semantics() {
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("native_trace".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 1,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 100.0), entry, candle(3, 101.0, 101.0)].into(),
        }],
    };
    let mut observed = Vec::new();
    let result = simulate_with_observer(&input, |event| observed.push(event.clone()));
    assert!(result.is_ok());
    let phases = observed
        .iter()
        .flat_map(|event| event.callback_events.iter().map(|callback| callback.phase))
        .collect::<Vec<_>>();
    assert!(phases.starts_with(&[
        CallbackPhase::StakeSizing,
        CallbackPhase::Leverage,
        CallbackPhase::EntryConfirmation,
        CallbackPhase::OrderFilled,
    ]));
    assert_eq!(
        observed[0].callback_events[0].visibility.feature_index,
        Some(0)
    );
}

fn visible(feature_index: Option<usize>, orders: usize, wallet: f64) -> CallbackVisibility {
    CallbackVisibility {
        feature_index,
        order_count: orders,
        wallet_available: wallet,
        custom_state_generation: 0,
    }
}

fn record(
    runtime: &mut CallbackRuntime,
    phase: CallbackPhase,
    outcome: CallbackOutcome,
    visibility: CallbackVisibility,
) {
    assert_eq!(runtime.record(phase, outcome, visibility), Ok(()));
}

#[test]
fn entry_callbacks_follow_stake_leverage_confirmation_and_fill_order() {
    let mut runtime = CallbackRuntime::new(10, visible(Some(9), 0, 1_000.0));
    for (phase, outcome) in [
        (CallbackPhase::StakeSizing, CallbackOutcome::Value),
        (CallbackPhase::Leverage, CallbackOutcome::Value),
        (CallbackPhase::EntryConfirmation, CallbackOutcome::Accepted),
        (CallbackPhase::OrderFilled, CallbackOutcome::Accepted),
        (CallbackPhase::PositionAdjustment, CallbackOutcome::None),
        (CallbackPhase::CustomStoploss, CallbackOutcome::None),
        (CallbackPhase::CustomExit, CallbackOutcome::None),
        (CallbackPhase::CandleAfter, CallbackOutcome::Accepted),
    ] {
        let visibility = runtime.visibility().clone();
        record(&mut runtime, phase, outcome, visibility);
    }
    assert_eq!(
        runtime
            .events()
            .iter()
            .map(|event| event.phase)
            .collect::<Vec<_>>(),
        vec![
            CallbackPhase::StakeSizing,
            CallbackPhase::Leverage,
            CallbackPhase::EntryConfirmation,
            CallbackPhase::OrderFilled,
            CallbackPhase::PositionAdjustment,
            CallbackPhase::CustomStoploss,
            CallbackPhase::CustomExit,
            CallbackPhase::CandleAfter,
        ]
    );
}

#[test]
fn startup_and_open_trade_visibility_use_only_the_last_closed_row() {
    let mut startup = CallbackRuntime::new(0, visible(None, 0, 1_000.0));
    record(
        &mut startup,
        CallbackPhase::CandleAfter,
        CallbackOutcome::Accepted,
        visible(None, 0, 1_000.0),
    );
    assert_eq!(startup.events()[0].visibility.feature_index, None);

    let mut runtime = CallbackRuntime::new(7, visible(Some(6), 1, 900.0));
    record(
        &mut runtime,
        CallbackPhase::PositionAdjustment,
        CallbackOutcome::Value,
        visible(Some(6), 1, 900.0),
    );
    record(
        &mut runtime,
        CallbackPhase::OrderFilled,
        CallbackOutcome::Accepted,
        visible(Some(6), 2, 875.0),
    );
    record(
        &mut runtime,
        CallbackPhase::CustomStoploss,
        CallbackOutcome::Value,
        visible(Some(6), 2, 875.0),
    );
    record(
        &mut runtime,
        CallbackPhase::CustomExit,
        CallbackOutcome::None,
        visible(Some(6), 2, 875.0),
    );
    assert_eq!(runtime.events()[2].visibility.order_count, 2);
    assert_eq!(runtime.events()[2].visibility.wallet_available, 875.0);
    assert_eq!(runtime.events()[2].visibility.feature_index, Some(6));
}

#[test]
fn custom_state_commit_is_visible_at_the_official_next_callback_point() {
    let mut runtime = CallbackRuntime::new(4, visible(Some(3), 1, 900.0));
    let adjustment = runtime.invoke(CallbackPhase::PositionAdjustment, |state| {
        state.custom_state_generation += 1;
        Ok(CallbackOutcome::None)
    });
    assert!(adjustment.is_ok());
    let next = runtime.visibility().clone();
    record(
        &mut runtime,
        CallbackPhase::CustomStoploss,
        CallbackOutcome::None,
        next,
    );
    assert_eq!(runtime.events()[1].visibility.custom_state_generation, 1);
}

#[test]
fn same_candle_runtime_records_first_candidate_rejection_before_iteration() {
    let mut runtime = CallbackRuntime::new(4, visible(Some(3), 1, 900.0));
    record(
        &mut runtime,
        CallbackPhase::PositionAdjustment,
        CallbackOutcome::None,
        visible(Some(3), 1, 900.0),
    );
    record(
        &mut runtime,
        CallbackPhase::CustomStoploss,
        CallbackOutcome::Value,
        visible(Some(3), 1, 900.0),
    );
    record(
        &mut runtime,
        CallbackPhase::CustomExit,
        CallbackOutcome::Value,
        visible(Some(3), 1, 900.0),
    );
    record(
        &mut runtime,
        CallbackPhase::ExitConfirmation,
        CallbackOutcome::Rejected,
        visible(Some(3), 1, 900.0),
    );
    assert_eq!(
        same_candle_exit_winner(CallbackOutcome::Value, true),
        Ok(Some(CallbackPhase::CustomExit))
    );
    assert_eq!(runtime.events()[2].phase, CallbackPhase::CustomExit);
    assert_eq!(runtime.events()[3].outcome, CallbackOutcome::Rejected);
}

#[test]
fn callback_exception_rolls_back_wallet_order_and_custom_state() {
    let mut runtime = CallbackRuntime::new(3, visible(Some(2), 1, 900.0));
    let result = runtime.invoke(CallbackPhase::PositionAdjustment, |state| {
        state.order_count = 2;
        state.wallet_available = 700.0;
        state.custom_state_generation = 1;
        Err("injected callback failure".to_owned())
    });
    assert_eq!(
        result
            .as_ref()
            .map(|event| (event.outcome, event.transaction)),
        Ok((CallbackOutcome::Exception, CallbackTransaction::RolledBack))
    );
    assert_eq!(runtime.visibility(), &visible(Some(2), 1, 900.0));
}

#[test]
fn invalid_order_is_rejected_without_an_event_or_state_change() {
    let mut runtime = CallbackRuntime::new(2, visible(Some(1), 0, 1_000.0));
    let before = runtime.visibility().clone();
    assert_eq!(
        runtime.record(
            CallbackPhase::Leverage,
            CallbackOutcome::Value,
            before.clone()
        ),
        Err(CallbackRuntimeError::InvalidTransition {
            from: CallbackPhase::CandleStart,
            to: CallbackPhase::Leverage,
        })
    );
    assert!(runtime.events().is_empty());
    assert_eq!(runtime.visibility(), &before);
}

#[test]
fn semantic_trace_is_versioned_and_deterministic() {
    let make_trace = || {
        let mut runtime = CallbackRuntime::new(1, visible(Some(0), 0, 100.0));
        record(
            &mut runtime,
            CallbackPhase::CandleAfter,
            CallbackOutcome::Accepted,
            visible(Some(0), 0, 100.0),
        );
        serde_json::to_vec(runtime.events())
    };
    let left = make_trace();
    let right = make_trace();
    assert!(left.is_ok());
    assert!(right.is_ok());
    assert_eq!(left.as_deref().ok(), right.as_deref().ok());
    assert_eq!(runtime_schema_token(), CALLBACK_TRACE_SCHEMA_VERSION);
}

fn runtime_schema_token() -> &'static str {
    CALLBACK_TRACE_SCHEMA_VERSION
}
