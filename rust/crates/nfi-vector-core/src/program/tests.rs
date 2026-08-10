use serde_json::{json, Value};

use super::*;
use crate::VectorCoreError;

fn program_json() -> Value {
    let location = || json!({"path":"strategy.py","line":1,"column":0,"end_line":1,"end_column":1});
    let lookback = || json!({"kind":"finite","candles":0,"expression":null,"causal":true});
    let mut program = json!({
        "schema_version": "indicator-program-v1",
        "source": {"path":"contract.py","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        "selected_class": "Contract",
        "entrypoint": "f1",
        "functions": [{
            "id":"f1", "source_name":"populate_indicators", "kind":"entrypoint",
            "parameters":[{"name":"dataframe","node":"n1","value_type":"dataframe"}],
            "node_ids":["n1","n2","n3","n4","n5","n6","n7","n8"], "return_node":"n8"
        }],
        "nodes": [
            {"id":"n1","function":"f1","source_order":0,"op":"parameter","value_type":"dataframe","inputs":[],"parameters":{"name":"dataframe"},"lookback":lookback()},
            {"id":"n2","function":"f1","source_order":1,"op":"column-read","value_type":"f64-column","inputs":["n1"],"parameters":{"column":"close"},"lookback":lookback()},
            {"id":"n3","function":"f1","source_order":2,"op":"indicator-call","value_type":"f64-column","inputs":["n2"],"parameters":{"family":"unsupported","name":"unused"},"lookback":lookback()},
            {"id":"n4","function":"f1","source_order":3,"op":"column-write","value_type":"dataframe","inputs":["n1","n3"],"parameters":{"column":"unused"},"lookback":lookback()},
            {"id":"n5","function":"f1","source_order":4,"op":"column-write","value_type":"dataframe","inputs":["n1","n2"],"parameters":{"column":"base"},"lookback":lookback()},
            {"id":"n6","function":"f1","source_order":5,"op":"column-read","value_type":"f64-column","inputs":["n5"],"parameters":{"column":"base"},"lookback":lookback()},
            {"id":"n7","function":"f1","source_order":6,"op":"column-write","value_type":"dataframe","inputs":["n5","n6"],"parameters":{"column":"wanted"},"lookback":lookback()},
            {"id":"n8","function":"f1","source_order":7,"op":"return","value_type":"dataframe","inputs":["n7"],"parameters":{},"lookback":lookback()}
        ],
        "required_input_columns":["close"], "produced_columns":["base","unused","wanted"],
        "informative_nodes":[], "opcodes":["column-read","column-write","indicator-call","parameter","return"],
        "max_lookback":lookback(),
        "source_map":{"n1":location(),"n2":location(),"n3":location(),"n4":location(),"n5":location(),"n6":location(),"n7":location(),"n8":location()},
        "fingerprint":""
    });
    program["fingerprint"] = Value::String(
        validation::canonical_fingerprint(&program).expect("test program identity is serializable"),
    );
    program
}

fn parse(value: &Value) -> Result<IndicatorProgram, VectorCoreError> {
    IndicatorProgram::from_json(&serde_json::to_string(value).expect("JSON serializes"))
}

#[test]
fn parses_a_valid_generic_program() {
    let program = parse(&program_json()).expect("program is valid");
    assert_eq!(program.entrypoint, "f1");
}

#[test]
fn rejects_forward_input() {
    let mut value = program_json();
    value["nodes"][1]["inputs"] = json!(["n3"]);
    assert!(
        matches!(parse(&value), Err(VectorCoreError::InvalidProgram(message)) if message.contains("non-prior input"))
    );
}

#[test]
fn rejects_content_mutation_with_a_stale_fingerprint() {
    let mut value = program_json();
    value["nodes"][1]["parameters"]["column"] = json!("open");
    assert!(
        matches!(parse(&value), Err(VectorCoreError::InvalidProgram(message)) if message.contains("fingerprint"))
    );
}

#[test]
fn rejects_invalid_ownership_source_map_and_noncausal_lookback() {
    let mut ownership = program_json();
    ownership["functions"][0]["node_ids"] = json!(["n1", "n2", "n3", "n5", "n6", "n7", "n8"]);
    assert!(
        matches!(parse(&ownership), Err(VectorCoreError::InvalidProgram(message)) if message.contains("ownership"))
    );

    let mut source_map = program_json();
    source_map["source_map"]
        .as_object_mut()
        .expect("object")
        .remove("n4");
    assert!(
        matches!(parse(&source_map), Err(VectorCoreError::InvalidProgram(message)) if message.contains("source map"))
    );

    let mut noncausal = program_json();
    noncausal["nodes"][6]["lookback"]["causal"] = json!(false);
    assert!(
        matches!(parse(&noncausal), Err(VectorCoreError::InvalidProgram(message)) if message.contains("non-causal"))
    );
}

#[test]
fn plans_only_nodes_reachable_from_requested_outputs_and_skips_unused_opcode() {
    let program = parse(&program_json()).expect("program is valid");
    let plan = program
        .execution_plan(&["wanted".to_owned()])
        .expect("output exists");
    assert_eq!(plan.requested_outputs, vec!["wanted".to_owned()]);
    assert_eq!(plan.required_input_columns, vec!["close".to_owned()]);
    assert_eq!(
        plan.nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<Vec<_>>(),
        ["n2", "n5", "n6", "n7"]
    );
    assert!(plan
        .nodes
        .iter()
        .all(|node| node.id != "n3" && node.id != "n4"));
    assert!(matches!(
        program.execution_plan(&["missing".to_owned()]),
        Err(VectorCoreError::MissingOutput(output)) if output == "missing"
    ));
}
