//! Todo 14 executable callback schema and bounded VM contract.

use super::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn hash(seed: char) -> String {
    std::iter::repeat_n(seed, 64).collect()
}

fn expression(value: &Value) -> Value {
    json!({"op": "literal", "value": value})
}

fn entrypoint(
    name: &str,
    active: bool,
    accepted: &[&str],
    fallback: &str,
    instructions: &Value,
) -> Value {
    json!({
        "name": name,
        "active": active,
        "cadence": match name {
            "bot_loop_start" => "once_per_main_candle",
            "leverage" | "custom_stake_amount" | "confirm_trade_entry" => "per_initial_entry",
            "order_filled" => "per_fill",
            "confirm_trade_exit" => "per_exit_candidate",
            "loop_cadence_startup_lookback" => "synthetic_lifecycle",
            _ => "per_open_trade_candle",
        },
        "accepted_returns": accepted,
        "exception_fallback": {
            "class": fallback,
            "value": match fallback {
                "adjustment" => json!([null, ""]),
                "leverage" => json!(1.0),
                "stake" => json!("proposed_stake"),
                "boolean" => json!(name != "custom_exit"),
                "lifecycle_transition" => json!("load_trim_execute"),
                _ => Value::Null,
            }
        },
        "instructions": instructions.clone(),
        "max_steps": 32,
        "order": {
            "after": match name {
                "custom_stake_amount" => vec!["leverage"],
                "confirm_trade_entry" => vec!["custom_stake_amount"],
                "order_filled" => vec!["confirm_trade_entry"],
                "adjust_trade_position" => vec!["order_filled"],
                "custom_stoploss" => vec!["adjust_trade_position"],
                "custom_exit" => vec!["custom_stoploss"],
                "confirm_trade_exit" => vec!["custom_exit"],
                _ => Vec::new(),
            },
            "before": [],
            "phase": match name {
                "loop_cadence_startup_lookback" => 0, "bot_loop_start" => 1,
                "leverage" => 2, "custom_stake_amount" => 3,
                "confirm_trade_entry" => 4, "order_filled" => 5,
                "adjust_trade_position" => 6, "custom_stoploss" => 7,
                "custom_exit" => 8, _ => 9,
            }
        },
        "predicate_ids": ["p1"],
        "transaction_policy": {
            "ordinary_trade": "commit_on_success_rollback_on_exception",
            "scheduler_prior": "preserve",
            "shared_custom_state": "commit_executed_writes",
            "strategy_registers": "commit_executed_writes"
        },
        "visibility": {
            "callback_dataframe_completed_candle_lag": 2,
            "signal_row_offset": -1,
            "successful_state_visible": "next_callback_in_scheduler_order"
        }
    })
}

fn program_json(mode: &str) -> Value {
    let rows = [
        ("bot_loop_start", &["none"][..], "none", "none"),
        ("leverage", &["finite-number"][..], "leverage", "leverage"),
        (
            "custom_stake_amount",
            &["finite-positive-number", "zero", "none"][..],
            "stake",
            "stake",
        ),
        (
            "confirm_trade_entry",
            &["truthy-accept", "falsy-reject"][..],
            "boolean",
            "boolean",
        ),
        ("order_filled", &["none"][..], "none", "none"),
        (
            "adjust_trade_position",
            &[
                "none",
                "zero",
                "positive-number",
                "negative-number",
                "number-and-tag",
            ][..],
            "adjustment",
            "adjustment",
        ),
        (
            "custom_stoploss",
            &["none", "finite-number"][..],
            "none",
            "stoploss",
        ),
        (
            "custom_exit",
            &["none", "false", "true", "non-empty-string"][..],
            "boolean",
            "exit_reason",
        ),
        (
            "confirm_trade_exit",
            &["truthy-accept", "falsy-reject"][..],
            "boolean",
            "boolean",
        ),
        (
            "loop_cadence_startup_lookback",
            &["lifecycle-transition"][..],
            "lifecycle_transition",
            "lifecycle_transition",
        ),
    ];
    let entrypoints = rows.into_iter().enumerate().map(|(index, (name, accepted, fallback_class, return_class))| {
        let returned = if name == "bot_loop_start" {
            json!([
                {"op":"set_register","id":"i1","predicate_ids":["p1"],"register_id":"r1","value":expression(&json!(2))},
                {"op":"set_custom_state","id":"i2","predicate_ids":["p1"],"key":"seen","value":{"op":"read_register","register_id":"r1"}},
                {"op":"emit_observation","id":"i3","predicate_ids":["p1"],"channel":"strategy_stdout_json","payload":{"op":"record","fields":[{"name":"seen","value":{"op":"read_custom_state","key":"seen","default":expression(&json!(null))}}]}},
                {"op":"return","id":"i4","predicate_ids":["p1"],"result":{"class":"none","value":expression(&json!(null))}}
            ])
        } else if name == "loop_cadence_startup_lookback" {
            json!([{"op":"return","id":format!("i{}", index + 5),"predicate_ids":["p1"],"result":{"class":"lifecycle_transition","value":expression(&json!("ready"))}}])
        } else {
            let value = match return_class {
                "leverage" => expression(&json!(2.0)),
                "stake" => expression(&json!(10.0)),
                "boolean" => expression(&json!(true)),
                "exit_reason" => expression(&json!("exit")),
                "stoploss" => expression(&json!(null)),
                _ => Value::Null,
            };
            json!([{"op":"return","id":format!("i{}", index + 5),"predicate_ids":["p1"],"result":{"class":return_class,"value":value}}])
        };
        (name.to_owned(), entrypoint(name, mode == "futures" || name != "leverage", accepted, fallback_class, &returned))
    }).collect::<serde_json::Map<_, _>>();
    json!({
        "schema_version":"executable-callback-program-v1",
        "identity":{
            "callback_contract_file_sha256":"a2cd2bf7ea60b131885122a2b5a308ba64f610942ce3869fda08c6dc3a258576",
            "callback_contract_fingerprint":"7c26cbaea6853a20b93932dbc0f3bc788cf0d43e58f243e9985029a727d6ec7f",
            "callback_execution_ir_fingerprint":hash('a'),
            "program_fingerprint":hash('0'),
            "run_mode":"backtest",
            "selected_class_ast_sha256":hash('b'),
            "source_closure":[{"ast_sha256":hash('c'),"logical_method_id":"m1","logical_owner_id":"o1","source_body_sha256":hash('d'),"source_id":format!("sha256:{}",hash('e')),"diagnostic_path":"strategy.py"}],
            "source_predicates":[{"ast_sha256":hash('f'),"expression":"x > 0","id":"p1","producer_method_id":"m1","source_order":0}],
            "trading_mode":mode
        },
        "entrypoints":entrypoints,
        "registers":[{"id":"r1","initial":expression(&json!(1)),"logical_name_hash":hash('1'),"scope":"strategy_run","type":{"kind":"i64"}}],
        "required_custom_state":[{"key":"seen","type":{"kind":"i64"}}],
        "required_inputs":[]
    })
}

fn parsed_program(mode: &str) -> Result<ExecutableCallbackProgram, serde_json::Error> {
    parsed_mutation(mode, |_| {})
}

fn parsed_mutation(
    mode: &str,
    mutation: impl FnOnce(&mut Value),
) -> Result<ExecutableCallbackProgram, serde_json::Error> {
    let mut value = program_json(mode);
    mutation(&mut value);
    let fingerprint = executable_callback_fingerprint_value(&value);
    if let (Ok(fingerprint), Some(identity)) = (fingerprint, value.get_mut("identity")) {
        identity["program_fingerprint"] = Value::String(fingerprint);
    }
    serde_json::from_value(value)
}

#[path = "task14_callback_program/runtime_tests.rs"]
mod runtime_tests;
#[path = "task14_callback_program/schema_tests.rs"]
mod schema_tests;
