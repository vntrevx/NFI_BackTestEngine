//! Direct official callback vectors supplement the captured portfolio fixtures.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::domain::{ManagedCustomExitPrefix, ManagedSystemExitPrograms};
use crate::portfolio::TradeSide;
use crate::scalar_vm::{evaluate_scalar_decision_program, evaluate_scalar_program_bundle};

use super::exit::managed_exit_matcher_matches;

#[test]
fn donor_exit_callbacks_match_pinned_official_boundary_vectors() {
    let vectors: Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-system-v4-2026-09-07/official-exit-cases-v2.json"
    ))
    .unwrap();
    let documents: BTreeMap<String, Value> = serde_json::from_str(include_str!(
        "../../../../../benchmarks/fixtures/source-contracts/x8-system-v4/exit-variants.json"
    ))
    .unwrap();
    let programs: BTreeMap<_, (ManagedSystemExitPrograms, ManagedCustomExitPrefix)> = documents
        .into_iter()
        .map(|(name, value)| {
            (
                name,
                (
                    serde_json::from_value(value["exits"].clone()).unwrap(),
                    serde_json::from_value(value["prefix"].clone()).unwrap(),
                ),
            )
        })
        .collect();
    let cases = vectors["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 337);
    for (index, case) in cases.iter().enumerate() {
        let (system, prefix) = &programs[case["variant"].as_str().unwrap()];
        let mut values: BTreeMap<String, Value> =
            serde_json::from_value(case["inputs"].clone()).unwrap();
        let actual = match case["kind"].as_str().unwrap() {
            "prefix" => evaluate_scalar_program_bundle(
                &prefix.bundle.programs,
                &prefix.bundle.entry,
                &values,
            ),
            "stop" => {
                let program = if case["helper"] == "long_exit_stoploss" {
                    &system.long_stop
                } else {
                    &system.short_stop
                };
                evaluate_scalar_decision_program(program, &values)
            }
            "target" => {
                target_inputs(system, case, &mut values);
                evaluate_scalar_decision_program(&system.profit_target.program, &values)
            }
            _ => panic!("unknown vector kind"),
        };
        assert_eq!(
            actual.as_ref(),
            Some(&case["expected"]),
            "case {index}: {case}"
        );
    }
}

fn target_inputs(
    system: &ManagedSystemExitPrograms,
    case: &Value,
    values: &mut BTreeMap<String, Value>,
) {
    let tags: Vec<String> = serde_json::from_value(case["tags"].clone()).unwrap();
    let side = if values["trade"]["is_short"] == true {
        TradeSide::Short
    } else {
        TradeSide::Long
    };
    values.extend(
        system
            .profit_target
            .predicates
            .iter()
            .map(|(name, matcher)| {
                (
                    name.clone(),
                    Value::Bool(managed_exit_matcher_matches(matcher, &tags, side)),
                )
            }),
    );
    if let Some(tags) = case["exit_tags"].as_array() {
        let derisk = super::source_system_exit::derisked(
            system,
            case["first_amount"].as_f64().unwrap(),
            values["trade"]["amount"].as_f64().unwrap(),
            tags.iter().map(|tag| tag.as_str().unwrap()),
        );
        values.insert("is_derisk".to_owned(), Value::Bool(derisk));
    }
}
