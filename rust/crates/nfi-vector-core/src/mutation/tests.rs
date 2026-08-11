//! Pinned Python/Rust vector-shadow oracle tests.

use std::collections::BTreeMap;

use arrow2::array::Array;
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Field, Schema};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::*;
use crate::batch::BatchView;
use crate::column::{OwnedColumn, ValueType};
use crate::engine::VectorEngine;
use crate::program::IndicatorProgram;

const FIXTURE: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/freqtrade-2026.5.1.json");
const INDICATOR_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/indicator-program.json");
const SIGNAL_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/signal-program.json");
const TAG_PROGRAM: &str =
    include_str!("../../../../../benchmarks/reference/vector-shadow/tag-program.json");

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
