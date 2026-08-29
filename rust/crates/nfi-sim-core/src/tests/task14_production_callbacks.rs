//! Todo 14 production simulator callback semantics.

use super::*;

#[test]
fn legacy_v1_futures_entry_order_remains_byte_compatible() {
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("production_futures".to_owned()),
        leverage: Some(2.0),
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
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
    let mut events = Vec::new();
    let result = simulate_with_observer(&input, |event| events.push(event.clone()));
    assert!(result.is_ok());
    let phases = events
        .iter()
        .flat_map(|event| event.callback_events.iter())
        .map(|event| event.phase)
        .take(4)
        .collect::<Vec<_>>();
    assert_eq!(
        phases,
        [
            CallbackPhase::StakeSizing,
            CallbackPhase::Leverage,
            CallbackPhase::EntryConfirmation,
            CallbackPhase::OrderFilled,
        ]
    );
}

#[test]
fn legacy_v1_rejects_executable_futures_entry_order() {
    let visibility = CallbackVisibility {
        feature_index: Some(0),
        order_count: 0,
        wallet_available: 1_000.0,
        custom_state_generation: 0,
    };
    let mut runtime = CallbackRuntime::new(1, visibility.clone());
    assert_eq!(
        runtime.record(CallbackPhase::Leverage, CallbackOutcome::Value, visibility),
        Err(CallbackRuntimeError::InvalidTransition {
            from: CallbackPhase::CandleStart,
            to: CallbackPhase::Leverage,
        })
    );
    assert!(runtime.events().is_empty());
}

fn custom_exit_program(kind: &str) -> Result<StateMachineProgram, serde_json::Error> {
    serde_json::from_value(serde_json::json!({
        "schema_version": "state-machine-program-v1",
        "entrypoints": {
            "custom_exit": {
                "max_steps": 4,
                "instructions": [
                    {
                        "opcode": "set_state",
                        "id": "write",
                        "key": "scratch",
                        "value_type": "integer",
                        "value": {"kind": "literal", "value": 1}
                    },
                    {
                        "opcode": "action",
                        "id": "action",
                        "kind": kind,
                        "stake": if kind == "add_entry" {
                            serde_json::json!({"kind": "literal", "value": 5.0})
                        } else {
                            serde_json::Value::Null
                        },
                        "tag": {"kind": "literal", "value": "custom_winner"}
                    }
                ]
            }
        },
        "required_reads": [],
        "required_columns": [],
        "required_state_keys": ["scratch"],
        "opcodes": ["set_state", "action"],
        "source_map": {
            "write": {"path": "strategy.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1},
            "action": {"path": "strategy.py", "line": 2, "column": 0, "end_line": 2, "end_column": 1}
        }
    }))
}

fn callback_input(kind: &str, reject_exit: bool) -> Result<SimulationInput, serde_json::Error> {
    let mut portfolio = config(1);
    portfolio.stoploss_ratio = -0.01;
    portfolio.state_machine_program = Some(custom_exit_program(kind)?);
    if reject_exit {
        portfolio.exit_confirmation_program = Some(serde_json::from_value(serde_json::json!({
            "statements": [{"op": "return", "value": {"op": "literal", "value": false}}],
            "functions": {}
        }))?);
    }
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("production".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let collision = candle(3, 100.0, 98.0);
    Ok(SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
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
            candles: vec![candle(1, 100.0, 100.0), entry, collision].into(),
        }],
    })
}

#[test]
fn production_rejected_custom_exit_continues_to_rejected_stop_candidate() {
    let input = callback_input("exit", true);
    assert!(input.is_ok());
    let Ok(input) = input else {
        return;
    };
    let plain = simulate(&input);
    let mut events = Vec::new();
    let observed = simulate_with_observer(&input, |event| events.push(event.clone()));
    let mut repeated_events = Vec::new();
    let repeated = simulate_with_observer(&input, |event| repeated_events.push(event.clone()));
    assert_eq!(observed.as_ref(), plain.as_ref());
    assert_eq!(repeated.as_ref(), plain.as_ref());
    assert_eq!(events, repeated_events);
    assert_eq!(
        observed
            .as_ref()
            .ok()
            .and_then(|result| result.trades.first())
            .map(|trade| trade.exit_reason.as_str()),
        Some("force_exit")
    );
    let phases = events
        .iter()
        .filter(|event| event.timestamp_ms == 3)
        .flat_map(|event| event.callback_events.iter())
        .map(|event| (event.phase, event.outcome))
        .collect::<Vec<_>>();
    assert!(phases.windows(2).any(|window| window
        == [
            (CallbackPhase::CustomExit, CallbackOutcome::Value),
            (CallbackPhase::ExitConfirmation, CallbackOutcome::Rejected),
        ]));
    assert_eq!(
        phases
            .iter()
            .filter(|phase| {
                **phase == (CallbackPhase::ExitConfirmation, CallbackOutcome::Rejected)
            })
            .count(),
        2
    );
    let generations = events
        .iter()
        .flat_map(|event| event.callback_events.iter())
        .filter(|event| {
            matches!(
                event.phase,
                CallbackPhase::CustomExit | CallbackPhase::ExitConfirmation
            )
        })
        .map(|event| event.visibility.custom_state_generation)
        .collect::<Vec<_>>();
    assert!(generations.windows(2).any(|window| window == [1, 1]));
}

#[test]
fn production_callback_exception_rolls_back_real_trade_and_falls_through() {
    let input = callback_input("add_entry", false);
    assert!(input.is_ok());
    let Ok(input) = input else {
        return;
    };
    let mut events = Vec::new();
    let result = simulate_with_observer(&input, |event| events.push(event.clone()));
    assert!(result.is_ok());
    assert_eq!(
        result
            .as_ref()
            .ok()
            .and_then(|value| value.trades.first())
            .map(|trade| (trade.exit_reason.as_str(), trade.orders.len())),
        Some(("stop_loss", 2))
    );
    let rollback = events
        .iter()
        .flat_map(|event| event.callback_events.iter())
        .find(|event| event.outcome == CallbackOutcome::Exception)
        .map(|event| {
            (
                event.phase,
                event.transaction,
                event.visibility.order_count,
                event.visibility.custom_state_generation,
            )
        });
    assert_eq!(
        rollback,
        Some((
            CallbackPhase::CustomExit,
            CallbackTransaction::RolledBack,
            1,
            0,
        ))
    );
}
