use arrow2::array::{Array, PrimitiveArray};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Field, Schema};
use serde_json::json;

use super::*;
use crate::sink::DiscardSink;

fn shift_program() -> IndicatorProgram {
    let location =
        || json!({"path":"strategy.py","line":7,"column":4,"end_line":7,"end_column":20});
    let zero = || json!({"kind":"finite","candles":0,"expression":null,"causal":true});
    let two = || json!({"kind":"finite","candles":2,"expression":null,"causal":true});
    let mut program = json!({
        "schema_version":"indicator-program-v1",
        "source":{"path":"ShiftContract.py","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        "selected_class":"ShiftContract",
        "entrypoint":"f1",
        "functions":[{"id":"f1","source_name":"populate_indicators","kind":"entrypoint","parameters":[{"name":"dataframe","node":"n1","value_type":"dataframe"}],"node_ids":["n1","n2","n3","n4","n5"],"return_node":"n5"}],
        "nodes":[
            {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":zero()},
            {"id":"n2","function":"f1","source_order":1,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"close"},"lookback":zero()},
            {"id":"n3","function":"f1","source_order":2,"op":"shift","value_type":"f64-column","inputs":["n2"],"parameters":{"periods":2},"lookback":two()},
            {"id":"n4","function":"f1","source_order":3,"op":"column-write","value_type":"dataframe","inputs":["n1","n3"],"parameters":{"column":"previous"},"lookback":two()},
            {"id":"n5","function":"f1","source_order":4,"op":"return","value_type":"dataframe","inputs":["n4"],"parameters":{},"lookback":two()}
        ],
        "required_input_columns":["close"],
        "produced_columns":["previous"],
        "informative_nodes":[],
        "opcodes":["column-read","column-write","parameter","return","shift"],
        "max_lookback":two(),
        "source_map":{"n1":location(),"n2":location(),"n3":location(),"n4":location(),"n5":location()},
        "fingerprint":""
    });
    program["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&program)
            .expect("test program identity is serializable"),
    );
    IndicatorProgram::from_json(&program.to_string()).expect("valid shift contract")
}

fn batch(values: Vec<f64>) -> (Schema, Chunk<Box<dyn Array>>) {
    (
        Schema::from(vec![Field::new("close", DataType::Float64, false)]),
        Chunk::new(vec![
            Box::new(PrimitiveArray::from_vec(values)) as Box<dyn Array>
        ]),
    )
}

#[test]
fn shift_state_crosses_batch_boundaries_and_memory_stays_bounded() {
    let program = shift_program();
    let mut engine =
        VectorEngine::new(&program, &["previous".to_owned()]).expect("output-specific engine");

    let (schema, first) = batch(vec![1.0, 2.0, 3.0]);
    let first =
        BatchView::project(&schema, &first, engine.input_requests()).expect("project first batch");
    let first_output = engine.execute_batch(&first).expect("execute first batch");
    let first_values = first_output.columns()["previous"].as_view();
    assert_eq!(
        (0..3)
            .map(|row| first_values.f64_at(row))
            .collect::<Vec<_>>(),
        vec![None, None, Some(1.0)]
    );

    let (_, second) = batch(vec![4.0, 5.0, 6.0]);
    let second = BatchView::project(&schema, &second, engine.input_requests())
        .expect("project second batch");
    let second_output = engine.execute_batch(&second).expect("execute second batch");
    let second_values = second_output.columns()["previous"].as_view();
    assert_eq!(
        (0..3)
            .map(|row| second_values.f64_at(row))
            .collect::<Vec<_>>(),
        vec![Some(2.0), Some(3.0), Some(4.0)]
    );
    assert_eq!(engine.profile().retained_state_values, 2);
    assert!(engine.profile().peak_live_values <= 3 * 3 + 2);
}

#[test]
fn long_discarded_execution_is_bounded_by_batch_and_shift_state() {
    let program = shift_program();
    let mut engine =
        VectorEngine::new(&program, &["previous".to_owned()]).expect("output-specific engine");
    let mut sink = DiscardSink::default();
    for batch_index in 0..1_000 {
        let values = (0..32)
            .map(|row| f64::from(batch_index * 32 + row))
            .collect();
        let (schema, source) = batch(values);
        let projected = BatchView::project(&schema, &source, engine.input_requests())
            .expect("project bounded batch");
        engine
            .execute_to_sink(&projected, &mut sink)
            .expect("stream batch");
    }
    assert_eq!(engine.profile().rows, 32_000);
    assert_eq!(engine.profile().retained_state_values, 2);
    assert!(engine.profile().peak_live_values <= 32 * 3 + 2);
    assert_eq!(sink.profile().retained_bytes, 0);
}

#[test]
fn unimplemented_kernel_fails_closed_with_source_location() {
    let program = shift_program();
    let mut encoded = serde_json::to_value(program).expect("program serializes");
    encoded["nodes"][2]["op"] = Value::String("indicator-call".to_owned());
    encoded["nodes"][2]["parameters"] = Value::Object(serde_json::Map::from_iter([(
        "name".to_owned(),
        Value::String("RSI".to_owned()),
    )]));
    encoded["opcodes"] = json!([
        "column-read",
        "column-write",
        "indicator-call",
        "parameter",
        "return"
    ]);
    encoded["fingerprint"] = Value::String(
        crate::program::validation::canonical_fingerprint(&encoded)
            .expect("mutated test program identity is serializable"),
    );
    let program = IndicatorProgram::from_json(&encoded.to_string())
        .expect("mutated program remains structurally valid");
    let mut engine = VectorEngine::new(&program, &["previous".to_owned()])
        .expect("structurally valid unimplemented program");
    let (schema, source) = batch(vec![1.0]);
    let projected =
        BatchView::project(&schema, &source, engine.input_requests()).expect("project input");
    assert!(matches!(
        engine.execute_batch(&projected),
        Err(VectorCoreError::UnsupportedOpcode { opcode, location })
            if opcode == "indicator-call" && location == "strategy.py:7:4"
    ));
}
