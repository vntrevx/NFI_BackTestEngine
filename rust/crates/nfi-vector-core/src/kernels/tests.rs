use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use serde::Deserialize;
use serde_json::{Map, Value};

use super::{execute_rolling, execute_talib};

const ORACLE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../benchmarks/reference/indicator-kernels/",
    "nfi-x7-e857f9b6-talib-v0.6.4.json"
));

#[derive(Deserialize)]
struct Fixture {
    rows: usize,
    inputs: BTreeMap<String, Vec<String>>,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    id: String,
    family: String,
    name: String,
    arguments: Map<String, Value>,
    input_columns: Vec<String>,
    outputs: Vec<ExpectedColumn>,
}

#[derive(Deserialize)]
struct ExpectedColumn {
    name: String,
    values: Vec<String>,
}

#[test]
fn every_latest_nfi_talib_case_is_column_exact() {
    let fixture: Fixture = serde_json::from_str(ORACLE).expect("valid committed oracle");
    assert_eq!(fixture.rows, 2_200);
    assert_eq!(fixture.cases.len(), 80);
    let inputs = fixture
        .inputs
        .iter()
        .map(|(name, values)| {
            (
                name.clone(),
                values.iter().map(|token| decode(token)).collect::<Vec<_>>(),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let mut covered = BTreeSet::new();
    for case in fixture.cases {
        if case.family != "talib" {
            continue;
        }
        covered.insert(case.name.clone());
        let selected = case
            .input_columns
            .iter()
            .map(|name| inputs.get(name).expect("oracle input").as_slice())
            .collect::<Vec<_>>();
        let actual = execute_talib(&case.name, &selected, &case.arguments)
            .unwrap_or_else(|error| panic!("{} failed: {error}", case.id));
        assert_eq!(actual.names().len(), case.outputs.len(), "{}", case.id);
        for expected in case.outputs {
            let values = actual
                .column(&expected.name)
                .unwrap_or_else(|| panic!("{} missing {}", case.id, expected.name));
            assert_eq!(values.len(), expected.values.len(), "{}", case.id);
            for (row, (actual, expected)) in values.iter().zip(&expected.values).enumerate() {
                assert_token(*actual, expected, &case.id, row);
            }
        }
    }
    assert_eq!(covered.len(), 19);
}

#[test]
fn every_latest_nfi_pandas_rolling_case_is_column_exact() {
    let fixture: Fixture = serde_json::from_str(ORACLE).expect("valid committed oracle");
    let inputs = fixture
        .inputs
        .iter()
        .map(|(name, values)| {
            (
                name.clone(),
                values.iter().map(|token| decode(token)).collect::<Vec<_>>(),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let mut covered = BTreeSet::new();
    let mut cases = 0;
    for case in fixture.cases {
        if case.family != "pandas" {
            continue;
        }
        cases += 1;
        let reducer = case.name.strip_prefix("rolling.").expect("rolling name");
        covered.insert(reducer.to_owned());
        let input = inputs.get(&case.input_columns[0]).expect("oracle input");
        let actual = execute_rolling(reducer, input, &case.arguments)
            .unwrap_or_else(|error| panic!("{} failed: {error}", case.id));
        let expected = &case.outputs[0];
        for (row, (actual, expected)) in actual.iter().zip(&expected.values).enumerate() {
            assert_token(*actual, expected, &case.id, row);
        }
    }
    assert_eq!(cases, 24);
    assert_eq!(
        covered,
        BTreeSet::from_iter(["max", "mean", "min", "sum"].map(str::to_owned))
    );
}

#[test]
fn finite_signal_boundaries_never_use_a_tolerance() {
    let fixture: Fixture = serde_json::from_str(ORACLE).expect("valid committed oracle");
    for case in fixture.cases {
        for output in case.outputs {
            for token in output.values {
                let threshold = decode(&token);
                if !threshold.is_finite() {
                    continue;
                }
                let below = adjacent(threshold, false);
                let above = adjacent(threshold, true);
                assert_eq!(threshold.partial_cmp(&threshold), Some(Ordering::Equal));
                assert_eq!(below.partial_cmp(&threshold), Some(Ordering::Less));
                assert_eq!(above.partial_cmp(&threshold), Some(Ordering::Greater));
            }
        }
    }
}

fn decode(token: &str) -> f64 {
    match token {
        "nan" => f64::NAN,
        "inf" => f64::INFINITY,
        "-inf" => f64::NEG_INFINITY,
        encoded => f64::from_bits(
            u64::from_str_radix(encoded.strip_prefix("0x").expect("f64 token"), 16)
                .expect("f64 bits"),
        ),
    }
}

fn assert_token(actual: f64, expected: &str, case: &str, row: usize) {
    if expected == "nan" {
        assert!(
            actual.is_nan(),
            "{case} row {row}: expected NaN, got {actual:?}"
        );
    } else {
        assert_eq!(
            actual.to_bits(),
            decode(expected).to_bits(),
            "{case} row {row}: expected {expected}, got 0x{:016x}",
            actual.to_bits()
        );
    }
}

fn adjacent(value: f64, upward: bool) -> f64 {
    if value == 0.0 {
        return if upward {
            f64::from_bits(1)
        } else {
            -f64::from_bits(1)
        };
    }
    let bits = value.to_bits();
    let increment = upward == value.is_sign_positive();
    f64::from_bits(if increment { bits + 1 } else { bits - 1 })
}
