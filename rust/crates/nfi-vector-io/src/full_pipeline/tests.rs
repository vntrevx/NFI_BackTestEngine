use std::collections::BTreeMap;

use nfi_sim_core::{simulate, PortfolioConfig};
use nfi_vector_core::alignment::{FrameCatalog, FrameIdentity, NumericFrame, Timeframe};
use nfi_vector_core::mutation::MutationProgram;
use nfi_vector_core::program::IndicatorProgram;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::*;
use crate::full_manifest::{
    retained_feature_fingerprint, CompileContext, FeatureRetention, PairContract, PairLimits,
    PairOptions, PairPrecision, RunContract, SourceExecutionSeal, SourceSeal, TradingMode,
};
use crate::{assemble_in_memory_vectors_profiled, prepare_freqtrade_ohlcv_catalog};

const INDICATOR: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/indicator-program.json");
const SIGNAL: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/signal-program.json");
const SOURCE_SHA: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CLASS_NAME: &str = "FullPipelineContract";
const START_MS: i64 = 1_699_999_800_000;
const STOP_MS: i64 = START_MS + 1_500_000;

#[test]
fn complete_spot_pipeline_is_exact_to_the_existing_in_memory_transport() {
    let actual_bundle = bundle(TradingMode::Spot);
    let expected_bundle = bundle(TradingMode::Spot);
    let prepared = prepare_freqtrade_ohlcv_catalog(
        &expected_bundle.frames,
        &format!("{START_MS}-{STOP_MS}"),
        expected_bundle.run.startup_candles,
    )
    .expect("prepared catalog");
    let expected_pair = stage::execute_pair(
        &expected_bundle.indicator_program,
        &expected_bundle.signal_program,
        &expected_bundle.tag_program,
        &prepared,
        &expected_bundle.run,
        &expected_bundle.retained_features.columns,
        &expected_bundle.futures,
        expected_bundle.pairs[0].clone(),
    )
    .expect("pair vector DAG");
    let (expected, expected_profile) =
        assemble_in_memory_vectors_profiled(expected_bundle.config, vec![expected_pair])
            .expect("legacy in-memory transport");

    let (actual, profile) =
        execute_full_native_vector_bundle_profiled(actual_bundle).expect("complete pipeline");

    assert_eq!(
        simulate(&actual).expect("actual simulation"),
        simulate(&expected).expect("expected simulation")
    );
    assert_eq!(profile.manifest_sha256, None);
    assert_eq!(profile.strategy_source_mode, "python-ast-compile-only");
    assert!(!profile.populate_methods_executed);
    assert_eq!(profile.runtime_mode, "rust-full-native");
    assert_eq!(profile.trading_mode, "spot");
    assert_eq!(profile.source_row_shift, 1);
    assert_eq!(profile.raw_frame_count, 1);
    assert_eq!(profile.transport.pair_count, expected_profile.pair_count);
    assert_eq!(profile.transport.row_count, 6);
    assert_eq!(profile.transport.feature_column_count, 2);
    assert_eq!(actual.pairs[0].execution_start_index, 1);
    assert_eq!(actual.pairs[0].feature_columns["delta"].len(), 6);
    assert_eq!(actual.pairs[0].feature_columns["enter_long"].len(), 6);
}

#[test]
fn futures_pipeline_fails_closed_without_funding_and_mark_data() {
    let bundle = bundle(TradingMode::Futures);

    let error = execute_full_native_vector_bundle_profiled(bundle)
        .expect_err("missing Futures descriptor must fail closed");

    assert!(error.to_string().contains("funding descriptor"), "{error}");
}

fn bundle(mode: TradingMode) -> NativeVectorBundle {
    let (indicator_program, signal_program, tag_program) = programs(mode);
    let identity = identity("TEST/USDT", "5m");
    let raw = NumericFrame {
        identity: identity.clone(),
        timestamps_ms: (0..6).map(|row| START_MS + row * 300_000).collect(),
        columns: BTreeMap::from([
            ("open".to_owned(), numbers([10.0; 6])),
            ("high".to_owned(), numbers([14.0; 6])),
            ("low".to_owned(), numbers([7.0; 6])),
            (
                "close".to_owned(),
                numbers([9.0, 11.0, 12.0, 8.0, 13.0, 7.0]),
            ),
            ("volume".to_owned(), numbers([1.0; 6])),
        ]),
    };
    let features = vec!["delta".to_owned(), "enter_long".to_owned()];
    let mut config_document = json!({
        "starting_balance": 1_000.0,
        "max_open_trades": 2,
        "stake_amount": 100.0,
        "fee_rate": 0.001,
        "stoploss_ratio": -0.2,
        "amount_step": 0.001,
        "price_step": 0.01,
        "is_futures": mode == TradingMode::Futures
    });
    if mode == TradingMode::Futures {
        config_document["funding_fee_interval_ms"] = json!(3_600_000);
    }
    let config: PortfolioConfig =
        serde_json::from_value(config_document).expect("simulator config");
    NativeVectorBundle {
        source: SourceSeal {
            strategy_sha256: SOURCE_SHA.to_owned(),
            config_sha256: "b".repeat(64),
            compiler_source_fingerprint: "c".repeat(64),
            selected_class: CLASS_NAME.to_owned(),
        },
        source_execution: SourceExecutionSeal {
            strategy_source_mode: "python-ast-compile-only".to_owned(),
            populate_methods_executed: false,
            runtime_mode: "rust-full-native".to_owned(),
        },
        config,
        compile_context: CompileContext {
            run_mode: "backtest".to_owned(),
            trading_mode: mode,
        },
        run: RunContract {
            trading_mode: mode,
            timerange_start_ms: START_MS,
            timerange_stop_ms: STOP_MS,
            startup_candles: 0,
            base_timeframe: Timeframe::parse("5m").expect("timeframe"),
            source_row_shift: 1,
        },
        retained_features: FeatureRetention {
            fingerprint: retained_feature_fingerprint(&features),
            columns: features,
        },
        pairs: vec![PairContract {
            identity: identity.clone(),
            metadata: BTreeMap::from([("pair".to_owned(), identity.pair.clone())]),
            precision: PairPrecision {
                amount_step: Some(0.001),
                price_step: Some(0.01),
            },
            limits: PairLimits {
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
            },
            price_steps: Vec::new(),
            options: PairOptions {
                can_short: mode == TradingMode::Futures,
                include_funding: mode == TradingMode::Futures,
                use_exit_signal: true,
                include_previous_close: true,
            },
        }],
        indicator_program,
        signal_program,
        tag_program,
        frames: FrameCatalog::new([(identity, raw)]).expect("raw catalog"),
        futures: Vec::new(),
    }
}

fn programs(mode: TradingMode) -> (IndicatorProgram, MutationProgram, MutationProgram) {
    let indicator = rewrite_program(INDICATOR, mode, false);
    let signal = rewrite_program(SIGNAL, mode, true);
    let tag = tag_from_signal(signal.clone());
    (
        IndicatorProgram::from_json(&indicator.to_string()).expect("indicator program"),
        MutationProgram::from_json(&signal.to_string()).expect("Signal program"),
        MutationProgram::from_json(&tag.to_string()).expect("Tag program"),
    )
}

fn tag_from_signal(mut program: Value) -> Value {
    program["schema_version"] = json!("tag-program-v1");
    program
        .as_object_mut()
        .expect("program object")
        .remove("signal_outputs");
    program["tag_outputs"] = json!([
        {
            "column": "enter_tag", "phase": "entry",
            "wrapper_initializer": "", "final_mutation": null
        },
        {
            "column": "exit_tag", "phase": "exit",
            "wrapper_initializer": "", "final_mutation": null
        }
    ]);
    program["tag_mutation_nodes"] = json!([]);
    program["route_contract"] = json!({
        "canonicalization": "python-str-split",
        "original_storage": "preserve-exact",
        "trailing_whitespace": "preserve"
    });
    reseal(&mut program);
    program
}

fn rewrite_program(encoded: &str, mode: TradingMode, use_delta: bool) -> Value {
    let mut program: Value = serde_json::from_str(encoded).expect("reference program");
    program["source"]["path"] = json!("strategy.py");
    program["source"]["sha256"] = json!(SOURCE_SHA);
    program["selected_class"] = json!(CLASS_NAME);
    if program.get("compile_context").is_some() {
        program["compile_context"] = json!({"run_mode": "backtest", "trading_mode": mode.as_str()});
    }
    if use_delta {
        replace_signal_inputs(&mut program);
        program["required_input_columns"] = json!(["delta", "exit_mask"]);
    } else {
        compact_indicator(&mut program);
    }
    reseal(&mut program);
    program
}

fn compact_indicator(program: &mut Value) {
    let nodes = program["nodes"].as_array().expect("indicator nodes")[..6].to_vec();
    let mut compact = nodes;
    compact.push(json!({
        "function": "f1",
        "id": "n7",
        "inputs": [],
        "lookback": {"candles": 0, "causal": true, "expression": null, "kind": "finite"},
        "op": "literal",
        "parameters": {"value": 0},
        "source_order": 6,
        "value_type": "int-scalar"
    }));
    compact.push(json!({
        "function": "f1",
        "id": "n8",
        "inputs": ["n5", "n7"],
        "lookback": {"candles": 0, "causal": true, "expression": null, "kind": "finite"},
        "op": "compare",
        "parameters": {"operator": "less-than"},
        "source_order": 7,
        "value_type": "bool-column"
    }));
    compact.push(json!({
        "function": "f1",
        "id": "n9",
        "inputs": ["n6", "n8"],
        "lookback": {"candles": 0, "causal": true, "expression": null, "kind": "finite"},
        "op": "column-write",
        "parameters": {"column": "exit_mask"},
        "source_order": 8,
        "value_type": "dataframe"
    }));
    compact.push(json!({
        "function": "f1",
        "id": "n10",
        "inputs": ["n9"],
        "lookback": {"candles": 0, "causal": true, "expression": null, "kind": "finite"},
        "op": "return",
        "parameters": {},
        "source_order": 9,
        "value_type": "dataframe"
    }));
    program["nodes"] = Value::Array(compact);
    program["functions"][0]["node_ids"] =
        json!(["n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8", "n9", "n10"]);
    program["functions"][0]["return_node"] = json!("n10");
    program["produced_columns"] = json!(["delta", "exit_mask"]);
    program["opcodes"] = json!([
        "binary",
        "column-read",
        "column-write",
        "compare",
        "literal",
        "parameter",
        "return"
    ]);
    program["max_lookback"] =
        json!({"candles": 0, "causal": true, "expression": null, "kind": "finite"});
    let location = program["source_map"]["n6"].clone();
    let map = program["source_map"].as_object_mut().expect("source map");
    map.retain(|name, _| {
        name.strip_prefix('n')
            .and_then(|value| value.parse::<usize>().ok())
            .is_some_and(|index| index <= 6)
    });
    for index in 7..=10 {
        map.insert(format!("n{index}"), location.clone());
    }
}

fn replace_signal_inputs(value: &mut Value) {
    match value {
        Value::String(text) if text == "score" => {
            *text = "delta".to_owned();
        }
        Value::Array(items) => items.iter_mut().for_each(replace_signal_inputs),
        Value::Object(items) => items.values_mut().for_each(replace_signal_inputs),
        _ => {}
    }
}

fn reseal(program: &mut Value) {
    let mut identity = program.clone();
    let object = identity.as_object_mut().expect("program object");
    object.remove("fingerprint");
    object["source"]
        .as_object_mut()
        .expect("source object")
        .remove("path");
    program["fingerprint"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&identity).expect("program identity"))
    ));
}

fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
    FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
}

fn numbers<const N: usize>(values: [f64; N]) -> Vec<Option<f64>> {
    values.into_iter().map(Some).collect()
}
