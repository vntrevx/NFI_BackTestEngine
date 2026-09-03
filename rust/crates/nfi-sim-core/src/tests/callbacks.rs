//! Compiled callback, stake, confirmation, and scalar-VM contracts.

use super::*;

#[test]
fn order_filled_program_updates_custom_state_and_projects_liquidation_price() {
    let mut config = config(1);
    config.callback_program = Some(CallbackProgram {
        order_filled: Some(OrderFilledProgram {
            initial_successful_entry_writes: vec![CustomDataWrite {
                key: "system_version".to_owned(),
                value: Value::String("system_v3_2".to_owned()),
            }],
            order_tag_actions: BTreeMap::from([(
                "grind_1_exit".to_owned(),
                vec![
                    CustomDataWrite {
                        key: "grind_1_cluster_max_profit_stake".to_owned(),
                        value: serde_json::json!(0.0),
                    },
                    CustomDataWrite {
                        key: "grind_1_cluster_max_profit_rate".to_owned(),
                        value: serde_json::json!(0.0),
                    },
                ],
            )]),
        }),
    });
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: vec![candle(1, 100.0, 99.0)].into(),
    };
    let signal = EntrySignal {
        tag: Some("grind_1_exit detail".to_owned()),
        leverage: None,
        liquidation_price: None,
    };
    let entry_candle = pair.candles.get(0).expect("fixture candle");

    let mut trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1_000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &config,
    )
    .expect("valid entry")
    .expect("sized entry");

    assert_eq!(
        trade.custom_data.get("system_version"),
        Some(&Value::String("system_v3_2".to_owned()))
    );
    assert_eq!(
        trade.custom_data.get("grind_1_cluster_max_profit_stake"),
        Some(&serde_json::json!(0.0))
    );
    trade.liquidation_price = Some(80.0);
    assert_eq!(
        crate::callbacks::scalar_trade_value(&trade)
            .and_then(|value| value.get("liquidation_price").cloned()),
        Some(serde_json::json!(80.0))
    );
}

#[test]
fn generic_order_filled_state_machine_updates_typed_custom_state() {
    let mut config = config(1);
    config.state_machine_program = Some(
        serde_json::from_value(serde_json::json!({
            "schema_version": "state-machine-program-v1",
            "entrypoints": {
                "order_filled": {
                    "max_steps": 4,
                    "instructions": [{
                        "opcode": "set_state",
                        "id": "i1",
                        "key": "system_version",
                        "value_type": "string",
                        "value": {"kind": "literal", "value": "system_v3_2"}
                    }]
                }
            },
            "required_reads": [],
            "required_columns": [],
            "required_state_keys": ["system_version"],
            "opcodes": ["set_state"],
            "source_map": {
                "i1": {
                    "path": "strategy.py",
                    "line": 1,
                    "column": 0,
                    "end_line": 1,
                    "end_column": 1
                }
            }
        }))
        .expect("valid generic state machine"),
    );
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: vec![candle(1, 100.0, 99.0)].into(),
    };
    let signal = EntrySignal {
        tag: Some("63".to_owned()),
        leverage: None,
        liquidation_price: None,
    };

    let trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &pair.candles.get(0).expect("fixture candle"),
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1_000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &config,
    )
    .expect("valid entry")
    .expect("sized entry");

    assert_eq!(
        trade.custom_data.get("system_version"),
        Some(&Value::String("system_v3_2".to_owned()))
    );
}

#[test]
fn bounded_stake_vm_applies_tag_rule_and_exchange_minimum() {
    let program: StakeProgram = serde_json::from_value(serde_json::json!({
        "statements": [
            {
                "op": "let",
                "name": "enter_tags",
                "value": {
                    "op": "split_words",
                    "value": {"op": "variable", "name": "entry_tag"}
                }
            },
            {
                "op": "if",
                "condition": {
                    "op": "all_in",
                    "items": {"op": "variable", "name": "enter_tags"},
                    "container": {"op": "literal", "value": ["61", "62"]}
                },
                "then": [{
                    "op": "return",
                    "value": {
                        "op": "stake_clamp_min",
                        "multiplier": {"op": "literal", "value": 0.25}
                    }
                }],
                "otherwise": []
            },
            {
                "op": "return",
                "value": {"op": "variable", "name": "proposed_stake"}
            }
        ]
    }))
    .expect("valid stake program");

    let stake = evaluate_stake_program(
        &program,
        &StakeInputs {
            proposed_stake: 100.0,
            minimum_stake: 30.0,
            maximum_stake: 1_000.0,
            current_rate: 100.0,
            leverage: 1.0,
            entry_tag: Some("61"),
            side: TradeSide::Long,
        },
    )
    .expect("stake result");

    assert!((stake - 30.0).abs() < f64::EPSILON);
}

#[test]
fn entry_confirmation_vm_evaluates_tag_and_slippage_gates() {
    let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
        "statements": [
            {
                "op": "let",
                "name": "entry_tags",
                "value": {
                    "op": "split_words",
                    "value": {"op": "variable", "name": "entry_tag"}
                }
            },
            {
                "op": "if",
                "condition": {
                    "op": "all_in",
                    "items": {"op": "variable", "name": "entry_tags"},
                    "container": {"op": "literal", "value": ["120"]}
                },
                "then": [{
                    "op": "return",
                    "value": {"op": "literal", "value": false}
                }],
                "otherwise": []
            },
            {
                "op": "if",
                "condition": {
                    "op": "greater",
                    "left": {"op": "variable", "name": "rate"},
                    "right": {"op": "literal", "value": 102.0}
                },
                "then": [{
                    "op": "return",
                    "value": {"op": "literal", "value": false}
                }],
                "otherwise": []
            },
            {
                "op": "return",
                "value": {"op": "literal", "value": true}
            }
        ],
        "functions": {}
    }))
    .expect("valid confirmation program");
    let open_trades = Vec::new();
    let base = ConfirmInputs {
        pair: "BTC/USDT",
        timestamp_ms: 1,
        amount: 0.99,
        rate: 101.0,
        entry_tag: Some("61"),
        side: TradeSide::Long,
        previous_close: Some(100.0),
        open_trades: &open_trades,
        max_open_trades: 6,
        is_futures: false,
        order_type: OrderType::Limit,
    };

    assert_eq!(evaluate_confirm_program(&program, base), Some(true));
    assert_eq!(
        evaluate_confirm_program(
            &program,
            ConfirmInputs {
                entry_tag: Some("120"),
                ..base
            },
        ),
        Some(false)
    );
    assert_eq!(
        evaluate_confirm_program(
            &program,
            ConfirmInputs {
                rate: 103.0,
                ..base
            },
        ),
        Some(false)
    );
}

#[test]
fn entry_confirmation_vm_accepts_a_computed_negative_dataframe_index() {
    let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
        "statements": [
            {
                "op": "let",
                "name": "df",
                "value": {"op": "analyzed_frame"}
            },
            {
                "op": "let",
                "name": "last_candle",
                "value": {
                    "op": "index",
                    "value": {"op": "variable", "name": "df"},
                    "index": {
                        "op": "negative",
                        "value": {"op": "literal", "value": 1}
                    }
                }
            },
            {
                "op": "return",
                "value": {
                    "op": "less",
                    "left": {
                        "op": "field",
                        "value": {"op": "variable", "name": "last_candle"},
                        "name": "close"
                    },
                    "right": {"op": "variable", "name": "rate"}
                }
            }
        ],
        "functions": {}
    }))
    .expect("valid analyzed-frame confirmation program");
    let open_trades = Vec::new();
    let inputs = ConfirmInputs {
        pair: "APE/USDT",
        timestamp_ms: 1,
        amount: 1.0,
        rate: 101.0,
        entry_tag: Some("62"),
        side: TradeSide::Long,
        previous_close: Some(100.0),
        open_trades: &open_trades,
        max_open_trades: 6,
        is_futures: false,
        order_type: OrderType::Limit,
    };

    assert_eq!(evaluate_confirm_program(&program, inputs), Some(true));
}

#[test]
fn exit_confirmation_vm_rejects_spot_stop_and_emits_clear_effect() {
    let program: ConfirmProgram = serde_json::from_value(serde_json::json!({
        "statements": [
            {
                "op": "if",
                "condition": {
                    "op": "contains",
                    "container": {
                        "op": "literal",
                        "value": ["stop_loss", "trailing_stop_loss"]
                    },
                    "value": {"op": "variable", "name": "exit_reason"}
                },
                "then": [{
                    "op": "return",
                    "value": {"op": "literal", "value": false}
                }],
                "otherwise": []
            },
            {
                "op": "clear_profit_target",
                "pair": {"op": "variable", "name": "pair"}
            },
            {
                "op": "return",
                "value": {"op": "literal", "value": true}
            }
        ],
        "functions": {}
    }))
    .expect("valid exit confirmation program");
    let config = config(1);
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: vec![candle(1, 100.0, 99.0)].into(),
    };
    let signal = EntrySignal {
        tag: Some("61".to_owned()),
        leverage: None,
        liquidation_price: None,
    };
    let entry_candle = pair.candles.get(0).expect("fixture candle");
    let trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1_000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &config,
    )
    .expect("valid entry")
    .expect("sized entry");

    assert_eq!(
        evaluate_exit_confirm_program(&program, &trade, 1, 99.0, "stop_loss", &config),
        Some((false, false))
    );
    assert_eq!(
        evaluate_exit_confirm_program(&program, &trade, 2, 101.0, "custom_exit", &config),
        Some((true, true))
    );

    let mode_program: ConfirmProgram = serde_json::from_value(serde_json::json!({
        "statements": [{
            "op": "return",
            "value": {"op": "config_value", "name": "is_futures"}
        }],
        "functions": {}
    }))
    .expect("valid runtime-mode confirmation program");
    assert_eq!(
        evaluate_exit_confirm_program(&mode_program, &trade, 3, 101.0, "custom_exit", &config,),
        Some((false, false))
    );
    let mut futures = config;
    futures.is_futures = true;
    assert_eq!(
        evaluate_exit_confirm_program(&mode_program, &trade, 4, 101.0, "custom_exit", &futures,),
        Some((true, false))
    );
}

#[test]
fn scalar_decision_vm_evaluates_chained_comparison_and_formatted_reason() {
    let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.0.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["mode", "current_profit", "last_candle"],
        "expressions": [
            ["literal", "RSI_14"],
            ["variable", "last_candle"],
            ["index", 1, 0],
            ["variable", "current_profit"],
            ["literal", 0.01],
            ["literal", 0.001],
            ["compare", 4, [["greater", 3], ["greater-equal", 5]]],
            ["is-instance", 2, "np.float64"],
            ["literal", 80.0],
            ["compare", 2, [["greater", 8]]],
            ["and", [6, 7, 9]],
            ["literal", true],
            ["variable", "mode"],
            ["format", [["text", "exit_"], ["value", 12], ["text", "_0_1"]]],
            ["tuple", [11, 13]],
            ["literal", false],
            ["literal", null],
            ["tuple", [15, 16]]
        ],
        "statements": [
            ["set", "last_rsi", 2],
            ["if", 10, [["return", 14]], []],
            ["return", 17]
        ]
    }))
    .expect("valid scalar decision program");
    let inputs = BTreeMap::from([
        ("mode".to_owned(), Value::String("normal".to_owned())),
        ("current_profit".to_owned(), serde_json::json!(0.005)),
        (
            "last_candle".to_owned(),
            serde_json::json!({"RSI_14": 85.0}),
        ),
    ]);

    assert_eq!(
        evaluate_scalar_decision_program(&program, &inputs),
        Some(serde_json::json!([true, "exit_normal_0_1"]))
    );
    let nan_inputs = BTreeMap::from([
        ("mode".to_owned(), Value::String("normal".to_owned())),
        ("current_profit".to_owned(), serde_json::json!(0.005)),
        (
            "last_candle".to_owned(),
            serde_json::json!({"RSI_14": {"$float": "nan"}}),
        ),
    ]);
    assert_eq!(
        evaluate_scalar_decision_program(&program, &nan_inputs),
        Some(serde_json::json!([false, null]))
    );
}

#[test]
fn scalar_program_overlays_do_not_leak_writes_between_callbacks() {
    let writer: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.0.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["value"],
        "expressions": [
            ["literal", 2],
            ["variable", "value"]
        ],
        "statements": [
            ["set", "value", 0],
            ["return", 1]
        ]
    }))
    .expect("valid scalar writer");
    let reader: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.0.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["value"],
        "expressions": [["variable", "value"]],
        "statements": [["return", 0]]
    }))
    .expect("valid scalar reader");
    let programs = BTreeMap::from([("writer".to_owned(), writer), ("reader".to_owned(), reader)]);
    let base = BTreeMap::from([("value".to_owned(), serde_json::json!(1))]);

    assert_eq!(
        evaluate_scalar_program_bundle_from_base(&programs, "writer", &base),
        Some(serde_json::json!(2))
    );
    assert_eq!(
        evaluate_scalar_program_bundle_from_base(&programs, "reader", &base),
        Some(serde_json::json!(1))
    );
}

#[test]
fn scalar_decision_vm_resolves_transitive_program_calls_fail_closed() {
    let entry_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["mode", "current_profit"],
        "expressions": [
            ["variable", "mode"],
            ["variable", "current_profit"],
            ["call-program", "decide", [0, 1]]
        ],
        "statements": [["return", 2]]
    }))
    .expect("valid caller");
    let decision_program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
    "schema_version": "1.1.0",
    "opcode": "scalar-decision-program-v1",
    "parameters": ["mode", "current_profit"],
    "expressions": [
        ["variable", "current_profit"],
        ["literal", 0.1],
        ["compare", 0, [["greater", 1]]],
        ["literal", true],
        ["variable", "mode"],
        ["format", [["text", "exit_"], ["value", 4]]],
        ["tuple", [3, 5]],
        ["literal", false],
        ["literal", null],
        ["tuple", [7, 8]]
    ],
    "statements": [
        ["if", 2, [["return", 6]], []],
        ["return", 9]
    ]
    }))
    .expect("valid decision program");
    let programs = BTreeMap::from([
        ("custom_exit".to_owned(), entry_program.clone()),
        ("decide".to_owned(), decision_program),
    ]);
    let inputs = BTreeMap::from([
        ("mode".to_owned(), Value::String("normal".to_owned())),
        ("current_profit".to_owned(), serde_json::json!(0.2)),
    ]);

    assert_eq!(
        evaluate_scalar_program_bundle(&programs, "custom_exit", &inputs),
        Some(serde_json::json!([true, "exit_normal"]))
    );
    assert_eq!(
        evaluate_scalar_decision_program(&entry_program, &inputs),
        None
    );
    assert_eq!(
        evaluate_scalar_program_bundle(&programs, "missing", &BTreeMap::new()),
        None
    );
}

#[test]
fn scalar_decision_vm_preserves_first_match_for_flat_elif_chains() {
    let program: ScalarDecisionProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "1.2.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["score"],
        "expressions": [
            ["variable", "score"],
            ["literal", 1.0],
            ["compare", 0, [["less", 1]]],
            ["literal", "first"],
            ["literal", 3.0],
            ["compare", 0, [["less", 4]]],
            ["literal", "second"],
            ["literal", "fallback"]
        ],
        "statements": [
            ["if-chain", [
                [2, [["return", 3]]],
                [5, [["return", 6]]]
            ], [["return", 7]]]
        ]
    }))
    .expect("valid flat elif program");

    assert_eq!(
        evaluate_scalar_decision_program(
            &program,
            &BTreeMap::from([("score".to_owned(), serde_json::json!(2.0))]),
        ),
        Some(Value::String("second".to_owned()))
    );
}
