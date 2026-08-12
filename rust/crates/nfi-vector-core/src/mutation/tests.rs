//! Pinned Python/Rust vector-shadow oracle tests.

use std::collections::BTreeMap;

use arrow2::array::Array;
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Field, Schema};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::*;
use crate::batch::BatchView;
use crate::column::{OwnedColumn, ValueType};
use crate::engine::VectorEngine;
use crate::program::IndicatorProgram;
use crate::VectorCoreError;

const FIXTURE: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/freqtrade-2026.5.1.json");
const INDICATOR_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/indicator-program.json");
const SIGNAL_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/signal-program.json");
const TAG_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/tag-program.json");

#[test]
fn tag_decision_projection_is_exact_and_rejects_semantic_drift() {
    let signal = MutationProgram::from_json(SIGNAL_PROGRAM).expect("signal program valid");
    let mut tag = signal.clone();
    tag.schema_version = super::model::TAG_PROGRAM_VERSION.to_owned();

    prove_signal_tag_decision_equivalence(&signal, &tag)
        .expect("Tag-only projection leaves the exact Signal DAG");

    // A Tag assignment may repeat a Signal predicate solely to choose its
    // string. Once that Tag write is removed, its predicate branch is not
    // observable and must not make otherwise exact decision programs differ.
    let mut orphan = tag
        .nodes
        .iter()
        .find(|node| node.op == "literal")
        .expect("reference program has a literal")
        .clone();
    orphan.id = format!("n{}", tag.nodes.len() + 1);
    orphan.source_order = tag
        .nodes
        .iter()
        .filter(|node| node.function == orphan.function)
        .map(|node| node.source_order)
        .max()
        .expect("function contains nodes")
        + 1;
    tag.source_map.insert(
        orphan.id.clone(),
        tag.source_map
            .get(&tag.nodes[0].id)
            .expect("reference source location")
            .clone(),
    );
    tag.functions
        .iter_mut()
        .find(|function| function.id == orphan.function)
        .expect("literal function exists")
        .node_ids
        .push(orphan.id.clone());
    tag.nodes.push(orphan);

    prove_signal_tag_decision_equivalence(&signal, &tag)
        .expect("unreachable Tag predicate branch is not a Signal decision");

    let decision = tag
        .nodes
        .iter_mut()
        .find(|node| node.op == "literal")
        .expect("reference Tag program has a decision literal");
    decision
        .parameters
        .insert("semantic-drift".to_owned(), json!(true));
    assert!(prove_signal_tag_decision_equivalence(&signal, &tag).is_err());
}

#[test]
#[ignore = "explicit external compiler-artifact diagnostic"]
fn external_signal_tag_decision_projection_is_exact() {
    let signal_path = std::env::var("NFI_SIGNAL_PROGRAM").expect("Signal artifact path");
    let tag_path = std::env::var("NFI_TAG_PROGRAM").expect("Tag artifact path");
    let signal = MutationProgram::from_json(
        &std::fs::read_to_string(signal_path).expect("read Signal artifact"),
    )
    .expect("external Signal program valid");
    let tag =
        MutationProgram::from_json(&std::fs::read_to_string(tag_path).expect("read Tag artifact"))
            .expect("external Tag program valid");

    prove_signal_tag_decision_equivalence(&signal, &tag)
        .expect("external Tag decision projection exact to Signal");
}

#[derive(Debug, Deserialize)]
struct Fixture {
    fingerprint: String,
    programs: BTreeMap<String, ProgramIdentity>,
    cases: Cases,
}

#[derive(Debug, Deserialize)]
struct ProgramIdentity {
    fingerprint: String,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct Cases {
    indicator: Case,
    signal: Case,
    tag: Case,
    execution: ExecutionCase,
}

#[derive(Debug, Deserialize)]
struct Case {
    input: EncodedFrame,
    outputs: Vec<String>,
    expected: EncodedFrame,
}

#[derive(Debug, Deserialize)]
struct ExecutionCase {
    source_row_shift: usize,
    execution_start_index: usize,
    expected: EncodedFrame,
    enabled_indexes: BTreeMap<String, Vec<usize>>,
}

#[derive(Debug, Deserialize)]
struct EncodedFrame {
    rows: usize,
    columns: Vec<EncodedColumn>,
}

#[derive(Debug, Deserialize)]
struct EncodedColumn {
    name: String,
    #[serde(rename = "type")]
    value_type: String,
    values: Vec<Value>,
}

#[test]
fn committed_shadow_bundle_and_program_identities_are_sealed() {
    let fixture: Fixture = serde_json::from_str(FIXTURE).expect("fixture parses");
    let mut document: Value = serde_json::from_str(FIXTURE).expect("fixture value parses");
    document
        .as_object_mut()
        .expect("fixture object")
        .remove("fingerprint");
    assert_eq!(
        fixture.fingerprint,
        format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&document).expect("canonical fixture"))
        )
    );

    for (name, encoded) in [
        ("indicator", INDICATOR_PROGRAM),
        ("signal", SIGNAL_PROGRAM),
        ("tag", TAG_PROGRAM),
    ] {
        let identity = &fixture.programs[name];
        assert_eq!(identity.sha256, format!("{:x}", Sha256::digest(encoded)));
        let program: Value = serde_json::from_str(encoded).expect("program value parses");
        assert_eq!(identity.fingerprint, program["fingerprint"]);
    }
}

#[test]
fn rust_indicator_lane_is_bit_exact_to_the_independent_python_oracle() {
    let fixture: Fixture = serde_json::from_str(FIXTURE).expect("fixture parses");
    let program = IndicatorProgram::from_json(INDICATOR_PROGRAM).expect("indicator program valid");
    let mut engine = VectorEngine::new(&program, &fixture.cases.indicator.outputs)
        .expect("indicator outputs are reachable");
    let source = decode_frame(&fixture.cases.indicator.input);
    let (schema, batch) = arrow_batch(&source);
    let projected =
        BatchView::project(&schema, &batch, engine.input_requests()).expect("input projects");
    let actual = engine
        .execute_batch(&projected)
        .expect("indicator executes");
    let expected = decode_frame(&fixture.cases.indicator.expected);

    for output in &fixture.cases.indicator.outputs {
        assert_column_exact(
            actual
                .columns()
                .get(output)
                .expect("actual indicator output"),
            expected.column(output).expect("expected indicator output"),
        );
    }
}

#[test]
fn rust_signal_and_tag_lanes_are_exact_without_sharing_python_outputs() {
    let fixture: Fixture = serde_json::from_str(FIXTURE).expect("fixture parses");
    for (label, encoded, case) in [
        ("signal", SIGNAL_PROGRAM, &fixture.cases.signal),
        ("tag", TAG_PROGRAM, &fixture.cases.tag),
    ] {
        let program = MutationProgram::from_json(encoded).expect("mutation program valid");
        let input = decode_frame(&case.input);
        let actual = MutationEngine::new(&program)
            .expect("mutation engine binds")
            .execute(input)
            .unwrap_or_else(|error| panic!("{label} mutation program failed: {error}"));
        let expected = decode_frame(&case.expected);
        for output in &case.outputs {
            assert_column_exact(
                actual.column(output).expect("actual mutation output"),
                expected.column(output).expect("expected mutation output"),
            );
        }
    }
}

#[test]
fn rust_execution_shift_and_numeric_one_indexes_are_exact() {
    let fixture: Fixture = serde_json::from_str(FIXTURE).expect("fixture parses");
    let program = MutationProgram::from_json(TAG_PROGRAM).expect("tag program valid");
    let source = MutationEngine::new(&program)
        .expect("tag engine binds")
        .execute(decode_frame(&fixture.cases.tag.input))
        .expect("tag program executes");
    let execution = materialize_execution_signals(
        &source,
        fixture.cases.execution.source_row_shift,
        fixture.cases.execution.execution_start_index,
    )
    .expect("execution signals materialize");
    let expected = decode_frame(&fixture.cases.execution.expected);

    for (name, column) in expected.columns() {
        assert_column_exact(
            execution
                .frame
                .column(name)
                .expect("actual execution output"),
            column,
        );
    }
    assert_eq!(
        execution.enabled_indexes,
        fixture.cases.execution.enabled_indexes
    );
}

#[test]
fn malformed_mutation_identity_and_mask_fail_closed() {
    let mut program: Value = serde_json::from_str(TAG_PROGRAM).expect("tag program value");
    program["route_contract"]["trailing_whitespace"] = Value::String("trim".to_owned());
    assert!(MutationProgram::from_json(&program.to_string()).is_err());

    let program = MutationProgram::from_json(TAG_PROGRAM).expect("tag program valid");
    let frame = decode_frame(
        &serde_json::from_value::<Fixture>(serde_json::from_str(FIXTURE).unwrap())
            .unwrap()
            .cases
            .tag
            .input,
    );
    let mut columns = frame.columns().clone();
    columns.insert("exit_mask".to_owned(), OwnedColumn::i64(vec![Some(1); 8]));
    let error = MutationEngine::new(&program)
        .expect("tag engine binds")
        .execute(MutationFrame::new(columns).expect("consistent frame"))
        .expect_err("numeric mask must fail closed");
    assert!(error.to_string().contains("Boolean"));
}

#[test]
fn execution_enablement_is_exact_numeric_one_after_the_source_shift() {
    let source = MutationFrame::new(BTreeMap::from([
        (
            "enter_long".to_owned(),
            OwnedColumn::i64(vec![Some(1), Some(2), Some(-1), None]),
        ),
        (
            "enter_short".to_owned(),
            OwnedColumn::f64(vec![Some(1.0), Some(1.000_000_000_1), Some(0.0), None]),
        ),
        (
            "enter_tag".to_owned(),
            OwnedColumn::text(vec![
                Some("101  ".to_owned()),
                Some("wrong ".to_owned()),
                None,
                Some(String::new()),
            ]),
        ),
    ]))
    .expect("source frame");

    let execution = materialize_execution_signals(&source, 1, 1).expect("shifted execution");
    assert_eq!(execution.enabled_indexes["enter_long"], vec![1]);
    assert_eq!(execution.enabled_indexes["enter_short"], vec![1]);
    assert_eq!(
        execution
            .frame
            .column("nfi_exec_enter_tag")
            .expect("shifted tag")
            .as_view()
            .text_at(1),
        Some("101  ")
    );

    let invalid = MutationFrame::new(BTreeMap::from([(
        "enter_long".to_owned(),
        OwnedColumn::text(vec![Some("1".to_owned())]),
    )]))
    .expect("invalid signal frame is structurally consistent");
    assert!(materialize_execution_signals(&invalid, 1, 0).is_err());
}

#[test]
fn numpy_divide_where_preserves_out_and_ieee_null_semantics() {
    let program = numpy_divide_program();
    let source = MutationFrame::new(BTreeMap::from([
        (
            "denominator".to_owned(),
            OwnedColumn::f64(vec![
                Some(3.0),
                Some(0.0),
                Some(-0.0),
                Some(0.0),
                Some(2.0),
                Some(f64::NAN),
                None,
                Some(2.0),
                Some(3.0),
            ]),
        ),
        (
            "mask".to_owned(),
            OwnedColumn::boolean(vec![
                Some(true),
                Some(true),
                Some(true),
                Some(true),
                Some(true),
                Some(true),
                Some(true),
                Some(false),
                None,
            ]),
        ),
        (
            "numerator".to_owned(),
            OwnedColumn::f64(vec![
                Some(6.0),
                Some(1.0),
                Some(1.0),
                Some(0.0),
                None,
                Some(f64::NAN),
                Some(1.0),
                Some(8.0),
                Some(9.0),
            ]),
        ),
        (
            "template".to_owned(),
            OwnedColumn::f64(vec![
                Some(100.0),
                Some(101.0),
                Some(102.0),
                Some(103.0),
                Some(104.0),
                Some(105.0),
                Some(106.0),
                Some(-0.0),
                None,
            ]),
        ),
    ]))
    .expect("source frame");

    let actual = MutationEngine::new(&program)
        .expect("numpy mutation program binds")
        .execute(source)
        .expect("numpy mutation program executes");
    let computed = [
        Some(2.0),
        Some(f64::INFINITY),
        Some(f64::NEG_INFINITY),
        Some(f64::NAN),
        None,
        Some(f64::NAN),
        None,
        Some(f64::NAN),
        Some(f64::NAN),
    ];
    assert_column_exact(
        actual.column("enter_long").expect("full_like output"),
        &OwnedColumn::f64(computed.to_vec()),
    );
    let mut preserved = computed[..7].to_vec();
    preserved.extend([Some(-0.0), None]);
    assert_column_exact(
        actual.column("enter_short").expect("supplied out output"),
        &OwnedColumn::f64(preserved),
    );
}

#[test]
fn numpy_full_accepts_compiler_object_dtype_for_string_columns() {
    let encoded = numpy_string_full_program_json(&json!({"dtype":"object"}));
    let program = MutationProgram::from_json(&encoded.to_string())
        .expect("compiler string full contract is valid");
    let source = MutationFrame::new(BTreeMap::from([(
        "seed".to_owned(),
        OwnedColumn::i64(vec![Some(1), None, Some(3)]),
    )]))
    .expect("source frame");

    let actual = MutationEngine::new(&program)
        .expect("string full program binds")
        .execute(source)
        .expect("string full program executes");

    assert_column_exact(
        actual.column("enter_tag").expect("full output"),
        &OwnedColumn::text(vec![
            Some(String::new()),
            Some(String::new()),
            Some(String::new()),
        ]),
    );
}

#[test]
fn numpy_full_object_dtype_contract_fails_closed() {
    for arguments in [
        json!({"dtype":"str"}),
        json!({"dtype":"object","order":"C"}),
        json!({"dtype":null}),
    ] {
        let encoded = numpy_string_full_program_json(&arguments);
        let error = MutationProgram::from_json(&encoded.to_string())
            .expect_err("unsupported full keyword arguments must fail closed");
        assert!(error
            .to_string()
            .contains("array-call n4 contract is invalid"));
    }

    let mut encoded = numpy_string_full_program_json(&json!({"dtype":"object"}));
    encoded["nodes"][2]["value_type"] = json!("f64-scalar");
    encoded["nodes"][2]["parameters"] = json!({"value":0.0});
    encoded["nodes"][3]["value_type"] = json!("f64-column");
    let error = MutationProgram::from_json(&encoded.to_string())
        .expect_err("object dtype is restricted to string-column full");
    assert!(error
        .to_string()
        .contains("array-call n4 contract is invalid"));
}

#[test]
fn numpy_isnan_returns_a_nullable_boolean_column() {
    let mut encoded = numpy_divide_program_json();
    encoded["nodes"][9]["inputs"] = json!(["n8"]);
    encoded["nodes"][9]["value_type"] = json!("bool-column");
    encoded["nodes"][9]["parameters"] = json!({"family":"numpy","name":"isnan","arguments":{}});
    encoded["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&encoded)
            .expect("numpy isnan test program has canonical identity"),
    );
    let program =
        MutationProgram::from_json(&encoded.to_string()).expect("numpy isnan program is valid");
    let source = MutationFrame::new(BTreeMap::from([
        (
            "numerator".to_owned(),
            OwnedColumn::f64(vec![Some(f64::NAN), Some(1.0), None]),
        ),
        (
            "denominator".to_owned(),
            OwnedColumn::f64(vec![Some(1.0), Some(0.0), Some(1.0)]),
        ),
        (
            "template".to_owned(),
            OwnedColumn::f64(vec![Some(0.0), Some(0.0), Some(0.0)]),
        ),
        (
            "mask".to_owned(),
            OwnedColumn::boolean(vec![Some(true), Some(true), Some(true)]),
        ),
    ]))
    .expect("source frame");

    let actual = MutationEngine::new(&program)
        .expect("numpy isnan program binds")
        .execute(source)
        .expect("numpy isnan program executes");
    assert_column_exact(
        actual.column("enter_short").expect("isnan output"),
        &OwnedColumn::boolean(vec![Some(true), Some(false), None]),
    );
}

#[test]
fn malformed_numpy_array_contracts_fail_closed() {
    let mut program = numpy_divide_program_json();
    program["nodes"][7]["parameters"]["arguments"] = json!({"casting":"unsafe"});
    assert!(MutationProgram::from_json(&program.to_string()).is_err());

    let mut program = numpy_divide_program_json();
    program["nodes"][5]["parameters"]["arguments"] = json!({"dtype":"object"});
    let error = MutationProgram::from_json(&program.to_string())
        .expect_err("full_like keyword arguments must fail closed");
    assert!(error
        .to_string()
        .contains("array-call n6 contract is invalid"));

    let mut program = numpy_divide_program_json();
    program["nodes"][6]["value_type"] = json!("f64-column");
    assert!(MutationProgram::from_json(&program.to_string()).is_err());

    let mut program = numpy_divide_program_json();
    program["nodes"][4]["parameters"] = json!({"special":"not-a-number"});
    assert!(MutationProgram::from_json(&program.to_string()).is_err());
}

#[test]
fn generic_string_mutations_match_python_order_index_and_null_semantics() {
    let cases = [
        (
            "rsplit",
            "long_entry_condition_ 65 _enable",
            "_",
            -2,
            json!([" 65 "]),
            false,
            true,
            " 65 ",
        ),
        (
            "partition",
            "BTC/USDT",
            "/",
            -1,
            json!(["BTC"]),
            true,
            true,
            "USDT",
        ),
        ("split", "a::b::", "::", -1, json!([""]), false, true, ""),
    ];
    for (method, source, separator, index, values, negated, scalar_member, suffix) in cases {
        let program = string_mutation_program(method, source, separator, index, &values, negated);
        let input = string_mutation_frame();
        let actual = MutationEngine::new(&program)
            .expect("string mutation program binds")
            .execute(input)
            .expect("string mutation program executes");
        assert_column_exact(
            actual.column("enter_long").expect("scalar membership"),
            &OwnedColumn::boolean(vec![Some(scalar_member); 4]),
        );
        assert_column_exact(
            actual.column("enter_short").expect("column membership"),
            &OwnedColumn::boolean(vec![Some(true), Some(false), Some(true), Some(false)]),
        );
        assert_column_exact(
            actual.column("enter_tag").expect("masked append"),
            &OwnedColumn::text(vec![
                Some(format!("alpha {suffix}")),
                Some(" keep  ".to_owned()),
                None,
                Some(suffix.to_owned()),
            ]),
        );
    }
}

#[test]
fn mutation_metadata_reads_are_explicit_and_fail_closed_when_missing() {
    let mut encoded =
        string_mutation_program_json("partition", "unused", "/", 0, &json!(["BTC/USDT"]), false);
    encoded["functions"][0]["parameters"] = json!([
        {"name":"dataframe","node":"n1","value_type":"dataframe"},
        {"name":"metadata","node":"n2","value_type":"metadata"}
    ]);
    encoded["nodes"][1] = json!({
        "id":"n2","function":"f1","source_order":1,"op":"parameter",
        "value_type":"metadata","inputs":[],"parameters":{"name":"metadata"},
        "lookback":{"kind":"finite","candles":0,"expression":null,"causal":true}
    });
    encoded["nodes"][2] = json!({
        "id":"n3","function":"f1","source_order":2,"op":"metadata-read",
        "value_type":"string-scalar","inputs":["n2"],"parameters":{"key":"pair"},
        "lookback":{"kind":"finite","candles":0,"expression":null,"causal":true}
    });
    encoded["nodes"][3]["parameters"] = json!({"values":["BTC/USDT"],"negated":false});
    encoded["opcodes"] = json!([
        "column-read",
        "frame-write",
        "masked-string-append",
        "membership",
        "metadata-read",
        "parameter",
        "return"
    ]);
    encoded["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&encoded)
            .expect("metadata mutation program has canonical identity"),
    );
    let program = MutationProgram::from_json(&encoded.to_string())
        .expect("metadata mutation program is valid");
    let engine = MutationEngine::new(&program).expect("metadata mutation engine binds");
    let actual = engine
        .execute_with_metadata(
            string_mutation_frame(),
            &BTreeMap::from([("pair".to_owned(), "BTC/USDT".to_owned())]),
        )
        .expect("explicit metadata executes");
    assert_eq!(
        actual
            .column("enter_long")
            .expect("metadata-gated output")
            .as_view()
            .bool_at(0),
        Some(true)
    );

    let error = engine
        .execute(string_mutation_frame())
        .expect_err("metadata must never be inferred");
    assert!(matches!(
        error,
        VectorCoreError::Execution { node, message }
            if node == "n3"
                && message.contains("strategy.py:1:0")
                && message.contains("runtime metadata has no string key \"pair\"")
    ));

    encoded["nodes"][2]["parameters"] = json!({"key":""});
    encoded["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&encoded)
            .expect("invalid metadata program still has serializable identity"),
    );
    let error = MutationProgram::from_json(&encoded.to_string())
        .expect_err("empty metadata keys must fail validation");
    assert!(error
        .to_string()
        .contains("metadata-read n3 contract is invalid"));
}

#[test]
fn string_mutations_reject_bad_contracts_and_out_of_range_indexes() {
    let program = string_mutation_program("split", "a::b", "::", -9, &json!(["a"]), false);
    let error = MutationEngine::new(&program)
        .expect("out-of-range index is structurally valid")
        .execute(string_mutation_frame())
        .expect_err("out-of-range string result must fail closed");
    assert!(error.to_string().contains("n3"));

    let mut program = string_mutation_program_json("split", "a::b", "::", 0, &json!(["a"]), false);
    program["nodes"][2]["parameters"]["separator"] = json!("");
    assert!(MutationProgram::from_json(&program.to_string()).is_err());

    let mut program = string_mutation_program_json("split", "a::b", "::", 0, &json!(["a"]), false);
    program["nodes"][3]["parameters"]["values"] = json!([["nested"]]);
    assert!(MutationProgram::from_json(&program.to_string()).is_err());

    let mut program = string_mutation_program_json("split", "a::b", "::", 0, &json!(["a"]), false);
    program["nodes"][10]["parameters"] = json!({"trim":true});
    assert!(MutationProgram::from_json(&program.to_string()).is_err());
}

fn string_mutation_frame() -> MutationFrame {
    MutationFrame::new(BTreeMap::from([
        (
            "append_mask".to_owned(),
            OwnedColumn::boolean(vec![Some(true), Some(false), None, Some(true)]),
        ),
        (
            "base_tags".to_owned(),
            OwnedColumn::text(vec![
                Some("alpha ".to_owned()),
                Some(" keep  ".to_owned()),
                None,
                Some(String::new()),
            ]),
        ),
        (
            "coin".to_owned(),
            OwnedColumn::text(vec![
                Some("BTC".to_owned()),
                Some("XRP".to_owned()),
                None,
                Some("ETH".to_owned()),
            ]),
        ),
    ]))
    .expect("string mutation frame")
}

fn string_mutation_program(
    method: &str,
    source: &str,
    separator: &str,
    index: i64,
    membership_values: &Value,
    negated: bool,
) -> MutationProgram {
    MutationProgram::from_json(
        &string_mutation_program_json(method, source, separator, index, membership_values, negated)
            .to_string(),
    )
    .expect("string mutation program is valid")
}

fn string_mutation_program_json(
    method: &str,
    source: &str,
    separator: &str,
    index: i64,
    membership_values: &Value,
    negated: bool,
) -> Value {
    let location = || {
        json!({
            "path":"strategy.py", "line":1, "column":0, "end_line":1, "end_column":1
        })
    };
    let lookback = || {
        json!({
            "kind":"finite", "candles":0, "expression":null, "causal":true
        })
    };
    let mut program = json!({
        "schema_version":"tag-program-v1",
        "source":{
            "path":"contract.py",
            "sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        },
        "selected_class":"Contract",
        "compile_context":{"run_mode":"backtest","trading_mode":"spot"},
        "entrypoints":[
            {"phase":"entry","function":"f1"},
            {"phase":"exit","function":"f2"}
        ],
        "functions":[
            {
                "id":"f1", "source_name":"populate_entry_trend", "kind":"entrypoint-entry",
                "parameters":[{"name":"dataframe","node":"n1","value_type":"dataframe"}],
                "node_ids":[
                    "n1","n2","n3","n4","n5","n6","n7","n8","n9","n10","n11","n12","n13"
                ],
                "return_node":"n13"
            },
            {
                "id":"f2", "source_name":"populate_exit_trend", "kind":"entrypoint-exit",
                "parameters":[{"name":"dataframe","node":"n14","value_type":"dataframe"}],
                "node_ids":["n14","n15"], "return_node":"n15"
            }
        ],
        "nodes":[
            {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n2","function":"f1","source_order":1,"op":"literal","value_type":"string-scalar","inputs":[],"parameters":{"value":source},"lookback":lookback()},
            {"id":"n3","function":"f1","source_order":2,"op":"string-split-index","value_type":"string-scalar","inputs":["n2"],"parameters":{"method":method,"separator":separator,"index":index},"lookback":lookback()},
            {"id":"n4","function":"f1","source_order":3,"op":"membership","value_type":"bool-scalar","inputs":["n3"],"parameters":{"values":membership_values,"negated":negated},"lookback":lookback()},
            {"id":"n5","function":"f1","source_order":4,"op":"frame-write","value_type":"dataframe","inputs":["n1","n4"],"parameters":{"rows":"all","mode":"column","assignment":"scalar-broadcast","columns":["enter_long"]},"lookback":lookback()},
            {"id":"n6","function":"f1","source_order":5,"op":"column-read","value_type":"string-column","inputs":["n1"],"parameters":{"column":"coin"},"lookback":lookback()},
            {"id":"n7","function":"f1","source_order":6,"op":"membership","value_type":"bool-column","inputs":["n6"],"parameters":{"values":["BTC",null,7,true],"negated":false},"lookback":lookback()},
            {"id":"n8","function":"f1","source_order":7,"op":"frame-write","value_type":"dataframe","inputs":["n5","n7"],"parameters":{"rows":"all","mode":"column","assignment":"column-values","columns":["enter_short"]},"lookback":lookback()},
            {"id":"n9","function":"f1","source_order":8,"op":"column-read","value_type":"string-column","inputs":["n1"],"parameters":{"column":"base_tags"},"lookback":lookback()},
            {"id":"n10","function":"f1","source_order":9,"op":"column-read","value_type":"bool-column","inputs":["n1"],"parameters":{"column":"append_mask"},"lookback":lookback()},
            {"id":"n11","function":"f1","source_order":10,"op":"masked-string-append","value_type":"string-column","inputs":["n9","n10","n3"],"parameters":{},"lookback":lookback()},
            {"id":"n12","function":"f1","source_order":11,"op":"frame-write","value_type":"dataframe","inputs":["n8","n11"],"parameters":{"rows":"all","mode":"column","assignment":"column-values","columns":["enter_tag"]},"lookback":lookback()},
            {"id":"n13","function":"f1","source_order":12,"op":"return","value_type":"dataframe","inputs":["n12"],"parameters":{},"lookback":lookback()},
            {"id":"n14","function":"f2","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n15","function":"f2","source_order":1,"op":"return","value_type":"dataframe","inputs":["n14"],"parameters":{},"lookback":lookback()}
        ],
        "required_input_columns":["append_mask","base_tags","coin"],
        "mutation_nodes":["n5","n8","n12"],
        "opcodes":[
            "column-read","frame-write","literal","masked-string-append","membership",
            "parameter","return","string-split-index"
        ],
        "max_lookback":lookback(),
        "source_map":{
            "n1":location(),"n2":location(),"n3":location(),"n4":location(),
            "n5":location(),"n6":location(),"n7":location(),"n8":location(),
            "n9":location(),"n10":location(),"n11":location(),"n12":location(),
            "n13":location(),"n14":location(),"n15":location()
        },
        "route_contract":{
            "canonicalization":"python-str-split",
            "original_storage":"preserve-exact",
            "trailing_whitespace":"preserve"
        },
        "tag_mutation_nodes":["n12"],
        "tag_outputs":[
            {"column":"enter_tag","phase":"entry","wrapper_initializer":"","final_mutation":"n12"},
            {"column":"exit_tag","phase":"exit","wrapper_initializer":"","final_mutation":null}
        ],
        "fingerprint":""
    });
    program["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&program)
            .expect("string mutation test program has canonical identity"),
    );
    program
}

fn numpy_divide_program() -> MutationProgram {
    MutationProgram::from_json(&numpy_divide_program_json().to_string())
        .expect("numpy divide program is valid")
}

fn numpy_divide_program_json() -> Value {
    let location = || {
        json!({
            "path":"strategy.py", "line":1, "column":0, "end_line":1, "end_column":1
        })
    };
    let lookback = || {
        json!({
            "kind":"finite", "candles":0, "expression":null, "causal":true
        })
    };
    let mut program = json!({
        "schema_version":"signal-program-v1",
        "source":{
            "path":"contract.py",
            "sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        },
        "selected_class":"Contract",
        "compile_context":{"run_mode":"backtest","trading_mode":"spot"},
        "entrypoints":[
            {"phase":"entry","function":"f1"},
            {"phase":"exit","function":"f2"}
        ],
        "functions":[
            {
                "id":"f1", "source_name":"populate_entry_trend", "kind":"entrypoint-entry",
                "parameters":[{"name":"dataframe","node":"n1","value_type":"dataframe"}],
                "node_ids":[
                    "n1","n2","n3","n4","n5","n6","n7","n8","n9","n10","n11","n12"
                ],
                "return_node":"n12"
            },
            {
                "id":"f2", "source_name":"populate_exit_trend", "kind":"entrypoint-exit",
                "parameters":[{"name":"dataframe","node":"n13","value_type":"dataframe"}],
                "node_ids":["n13","n14"], "return_node":"n14"
            }
        ],
        "nodes":[
            {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n2","function":"f1","source_order":1,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"numerator"},"lookback":lookback()},
            {"id":"n3","function":"f1","source_order":2,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"denominator"},"lookback":lookback()},
            {"id":"n4","function":"f1","source_order":3,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"template"},"lookback":lookback()},
            {"id":"n5","function":"f1","source_order":4,"op":"literal","value_type":"f64-scalar","inputs":[],"parameters":{"special":"nan"},"lookback":lookback()},
            {"id":"n6","function":"f1","source_order":5,"op":"array-call","value_type":"f64-column","inputs":["n4","n5"],"parameters":{"family":"numpy","name":"full_like","arguments":{}},"lookback":lookback()},
            {"id":"n7","function":"f1","source_order":6,"op":"column-read","value_type":"bool-column","inputs":["n1"],"parameters":{"column":"mask"},"lookback":lookback()},
            {"id":"n8","function":"f1","source_order":7,"op":"array-call","value_type":"f64-column","inputs":["n2","n3","n6","n7"],"parameters":{"family":"numpy","name":"divide","arguments":{}},"lookback":lookback()},
            {"id":"n9","function":"f1","source_order":8,"op":"frame-write","value_type":"dataframe","inputs":["n1","n8"],"parameters":{"rows":"all","mode":"column","assignment":"column-values","columns":["enter_long"]},"lookback":lookback()},
            {"id":"n10","function":"f1","source_order":9,"op":"array-call","value_type":"f64-column","inputs":["n2","n3","n4","n7"],"parameters":{"family":"numpy","name":"divide","arguments":{}},"lookback":lookback()},
            {"id":"n11","function":"f1","source_order":10,"op":"frame-write","value_type":"dataframe","inputs":["n9","n10"],"parameters":{"rows":"all","mode":"column","assignment":"column-values","columns":["enter_short"]},"lookback":lookback()},
            {"id":"n12","function":"f1","source_order":11,"op":"return","value_type":"dataframe","inputs":["n11"],"parameters":{},"lookback":lookback()},
            {"id":"n13","function":"f2","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n14","function":"f2","source_order":1,"op":"return","value_type":"dataframe","inputs":["n13"],"parameters":{},"lookback":lookback()}
        ],
        "required_input_columns":["denominator","mask","numerator","template"],
        "mutation_nodes":["n9","n11"],
        "opcodes":["array-call","column-read","frame-write","literal","parameter","return"],
        "max_lookback":lookback(),
        "source_map":{
            "n1":location(),"n2":location(),"n3":location(),"n4":location(),
            "n5":location(),"n6":location(),"n7":location(),"n8":location(),
            "n9":location(),"n10":location(),"n11":location(),"n12":location(),
            "n13":location(),"n14":location()
        },
        "signal_outputs":[
            {"column":"enter_long","phase":"entry","side":"long","final_mutation":"n9"},
            {"column":"enter_short","phase":"entry","side":"short","final_mutation":"n11"}
        ],
        "fingerprint":""
    });
    program["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&program)
            .expect("numpy divide test program has canonical identity"),
    );
    program
}

fn numpy_string_full_program_json(arguments: &Value) -> Value {
    let location = || {
        json!({
            "path":"strategy.py", "line":1, "column":0, "end_line":1, "end_column":1
        })
    };
    let lookback = || {
        json!({
            "kind":"finite", "candles":0, "expression":null, "causal":true
        })
    };
    let mut program = json!({
        "schema_version":"tag-program-v1",
        "source":{
            "path":"contract.py",
            "sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        },
        "selected_class":"Contract",
        "compile_context":{"run_mode":"backtest","trading_mode":"spot"},
        "entrypoints":[
            {"phase":"entry","function":"f1"},
            {"phase":"exit","function":"f2"}
        ],
        "functions":[
            {
                "id":"f1", "source_name":"populate_entry_trend", "kind":"entrypoint-entry",
                "parameters":[{"name":"dataframe","node":"n1","value_type":"dataframe"}],
                "node_ids":["n1","n2","n3","n4","n5","n6"], "return_node":"n6"
            },
            {
                "id":"f2", "source_name":"populate_exit_trend", "kind":"entrypoint-exit",
                "parameters":[{"name":"dataframe","node":"n7","value_type":"dataframe"}],
                "node_ids":["n7","n8"], "return_node":"n8"
            }
        ],
        "nodes":[
            {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n2","function":"f1","source_order":1,"op":"row-count","value_type":"int-scalar","inputs":["n1"],"parameters":{},"lookback":lookback()},
            {"id":"n3","function":"f1","source_order":2,"op":"literal","value_type":"string-scalar","inputs":[],"parameters":{"value":""},"lookback":lookback()},
            {"id":"n4","function":"f1","source_order":3,"op":"array-call","value_type":"string-column","inputs":["n2","n3"],"parameters":{"family":"numpy","name":"full","arguments":arguments},"lookback":lookback()},
            {"id":"n5","function":"f1","source_order":4,"op":"frame-write","value_type":"dataframe","inputs":["n1","n4"],"parameters":{"rows":"all","mode":"column","assignment":"column-values","columns":["enter_tag"]},"lookback":lookback()},
            {"id":"n6","function":"f1","source_order":5,"op":"return","value_type":"dataframe","inputs":["n5"],"parameters":{},"lookback":lookback()},
            {"id":"n7","function":"f2","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n8","function":"f2","source_order":1,"op":"return","value_type":"dataframe","inputs":["n7"],"parameters":{},"lookback":lookback()}
        ],
        "required_input_columns":[],
        "mutation_nodes":["n5"],
        "opcodes":["array-call","frame-write","literal","parameter","return","row-count"],
        "max_lookback":lookback(),
        "source_map":{
            "n1":location(),"n2":location(),"n3":location(),"n4":location(),
            "n5":location(),"n6":location(),"n7":location(),"n8":location()
        },
        "route_contract":{
            "canonicalization":"python-str-split",
            "original_storage":"preserve-exact",
            "trailing_whitespace":"preserve"
        },
        "tag_mutation_nodes":["n5"],
        "tag_outputs":[
            {"column":"enter_tag","phase":"entry","wrapper_initializer":"","final_mutation":"n5"},
            {"column":"exit_tag","phase":"exit","wrapper_initializer":"","final_mutation":null}
        ],
        "fingerprint":""
    });
    program["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&program)
            .expect("numpy full test program has canonical identity"),
    );
    program
}

fn decode_frame(encoded: &EncodedFrame) -> MutationFrame {
    let columns = encoded
        .columns
        .iter()
        .map(|column| {
            let values = match column.value_type.as_str() {
                "f64" => OwnedColumn::f64(
                    column
                        .values
                        .iter()
                        .map(|value| {
                            if value.is_null() {
                                None
                            } else {
                                Some(f64::from_bits(parse_f64_token(
                                    value.as_str().expect("f64 token"),
                                )))
                            }
                        })
                        .collect(),
                ),
                "i64" => OwnedColumn::i64(column.values.iter().map(Value::as_i64).collect()),
                "bool" => OwnedColumn::boolean(column.values.iter().map(Value::as_bool).collect()),
                "text" => OwnedColumn::text(
                    column
                        .values
                        .iter()
                        .map(|value| value.as_str().map(str::to_owned))
                        .collect(),
                ),
                other => panic!("unknown fixture column type: {other}"),
            };
            (column.name.clone(), values)
        })
        .collect();
    let frame = MutationFrame::new(columns).expect("fixture frame is consistent");
    assert_eq!(frame.len(), encoded.rows);
    frame
}

fn arrow_batch(frame: &MutationFrame) -> (Schema, Chunk<Box<dyn Array>>) {
    let fields = frame
        .columns()
        .iter()
        .map(|(name, column)| Field::new(name, data_type(column), true))
        .collect::<Vec<_>>();
    let arrays = frame
        .columns()
        .values()
        .map(|column| match column {
            OwnedColumn::F64(values) => Box::new(values.clone()) as Box<dyn Array>,
            OwnedColumn::I64(values) => Box::new(values.clone()) as Box<dyn Array>,
            OwnedColumn::Bool(values) => Box::new(values.clone()) as Box<dyn Array>,
            OwnedColumn::Text(values) => Box::new(values.clone()) as Box<dyn Array>,
            OwnedColumn::TimestampMs(values) => Box::new(values.clone()) as Box<dyn Array>,
        })
        .collect();
    (Schema::from(fields), Chunk::new(arrays))
}

fn data_type(column: &OwnedColumn) -> DataType {
    match column.as_view().value_type() {
        ValueType::F64 => DataType::Float64,
        ValueType::I64 => DataType::Int64,
        ValueType::Bool => DataType::Boolean,
        ValueType::Text => DataType::Utf8,
        ValueType::TimestampMs => {
            DataType::Timestamp(arrow2::datatypes::TimeUnit::Millisecond, None)
        }
    }
}

fn assert_column_exact(actual: &OwnedColumn, expected: &OwnedColumn) {
    assert_eq!(
        actual.as_view().value_type(),
        expected.as_view().value_type()
    );
    assert_eq!(actual.len(), expected.len());
    let actual = actual.as_view();
    let expected = expected.as_view();
    for row in 0..actual.len() {
        match actual.value_type() {
            ValueType::F64 => assert_eq!(
                actual.f64_at(row).map(f64::to_bits),
                expected.f64_at(row).map(f64::to_bits),
                "f64 row {row}"
            ),
            ValueType::I64 => assert_eq!(actual.i64_at(row), expected.i64_at(row)),
            ValueType::Bool => assert_eq!(actual.bool_at(row), expected.bool_at(row)),
            ValueType::Text => assert_eq!(actual.text_at(row), expected.text_at(row)),
            ValueType::TimestampMs => {
                assert_eq!(actual.timestamp_ms_at(row), expected.timestamp_ms_at(row));
            }
        }
    }
}

fn parse_f64_token(value: &str) -> u64 {
    u64::from_str_radix(value.strip_prefix("0x").expect("f64 token prefix"), 16)
        .expect("f64 token digits")
}
