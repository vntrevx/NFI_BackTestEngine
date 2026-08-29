use std::collections::BTreeMap;

use serde_json::Value;

use super::{
    merge, FrameIdentity, MergeSpec, MergeStream, NumericFrame, SourceLocation, Timeframe,
};
use crate::VectorCoreError;

const ORACLE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../benchmarks/reference/informative/freqtrade-2026.5.1.json"
));

#[test]
fn pinned_freqtrade_oracle_is_exact() {
    let document: Value = serde_json::from_str(ORACLE).expect("valid oracle");
    for case in document["cases"].as_array().expect("oracle cases") {
        let name = case["name"].as_str().expect("case name");
        let base = frame(
            &case["base"],
            case["base_pair"].as_str().expect("base pair"),
            case["call"]["timeframe"].as_str().expect("base timeframe"),
        );
        let informative = frame(
            &case["informative"],
            case["informative_pair"].as_str().expect("informative pair"),
            case["call"]["timeframe_inf"]
                .as_str()
                .expect("informative timeframe"),
        );
        let spec = spec(case);
        if case.get("error").is_some() {
            assert!(merge(&base, &informative, &spec).is_err(), "{name}");
        } else {
            let actual = merge(&base, &informative, &spec)
                .unwrap_or_else(|error| panic!("{name}: {error:?}"));
            assert_expected(&actual, &case["output"], name);
        }
    }
}

#[test]
fn stream_is_exact_at_the_five_minute_hour_boundary() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![
            ms("2024-01-01T00:50:00Z"),
            ms("2024-01-01T00:55:00Z"),
            ms("2024-01-01T01:00:00Z"),
        ],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0), Some(2.0), Some(3.0)])]),
    };
    let informative = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![ms("2024-01-01T00:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(9.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: informative.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let expected = merge(&base, &informative, &spec).expect("batch result");
    let empty = NumericFrame {
        identity: informative.identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("info".to_owned(), Vec::new())]),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    let first = stream
        .execute(&slice(&base, 0, 2), &informative)
        .expect("first chunk");
    let second = stream
        .execute(&slice(&base, 2, 3), &empty)
        .expect("second chunk");
    assert_eq!(stream.retained(), 1);
    assert_merged_bits(&concatenate(&first, &second), &expected);
}

#[test]
fn future_informative_mutation_does_not_change_pre_visibility_rows() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![
            ms("2024-01-01T00:55:00Z"),
            ms("2024-01-01T01:00:00Z"),
            ms("2024-01-01T01:05:00Z"),
        ],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0), Some(2.0), Some(3.0)])]),
    };
    let informative = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![ms("2024-01-01T00:00:00Z"), ms("2024-01-01T01:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(9.0), Some(10.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: informative.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let expected = merge(&base, &informative, &spec).expect("baseline merge");
    let mut mutated = informative;
    mutated.columns.get_mut("info").expect("informative column")[1] = Some(999.0);
    let actual = merge(&base, &mutated, &spec).expect("mutated merge");

    assert_merged_bits(&actual, &expected);
}

#[test]
fn empty_base_informative_chunk_is_retained_for_the_next_exact_chunk() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![ms("2024-01-01T00:50:00Z"), ms("2024-01-01T00:55:00Z")],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0), Some(2.0)])]),
    };
    let historical = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![ms("2023-12-31T23:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(5.0)])]),
    };
    let current = NumericFrame {
        identity: historical.identity.clone(),
        timestamps_ms: vec![ms("2024-01-01T00:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(9.0)])]),
    };
    let all_informative = NumericFrame {
        identity: historical.identity.clone(),
        timestamps_ms: vec![ms("2023-12-31T23:00:00Z"), ms("2024-01-01T00:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(5.0), Some(9.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: historical.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let expected = merge(&base, &all_informative, &spec).expect("batch result");
    let empty_base = NumericFrame {
        identity: base.identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("base".to_owned(), Vec::new())]),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    let empty = stream
        .execute(&empty_base, &historical)
        .expect("empty base chunk");
    assert!(empty.timestamps_ms.is_empty());
    assert_eq!(stream.retained(), 1);
    let actual = stream.execute(&base, &current).expect("exact chunk");
    assert_merged_bits(&actual, &expected);
    assert_eq!(stream.retained(), 1);
}

#[test]
fn ffill_stream_rejects_current_historical_rows_without_an_exact_match() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![ms("2024-01-01T00:50:00Z")],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let historical = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![ms("2023-12-31T23:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(5.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: historical.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    assert!(matches!(
        stream.execute(&base, &historical),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
}

#[test]
fn ffill_stream_rejects_a_leading_base_chunk_without_informative_events() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![ms("2024-01-01T00:50:00Z")],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let empty_informative = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("info".to_owned(), Vec::new())]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: empty_informative.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    assert!(matches!(
        stream.execute(&base, &empty_informative),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
}

#[test]
fn stream_rejects_unresolved_leading_history_and_timestamp_regression() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![ms("2024-01-01T00:00:00Z")],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let historical = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![ms("2023-12-31T23:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(5.0)])]),
    };
    let empty_base = NumericFrame {
        identity: base.identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("base".to_owned(), Vec::new())]),
    };
    let empty_info = NumericFrame {
        identity: historical.identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("info".to_owned(), Vec::new())]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: historical.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec.clone()).expect("stream");
    stream
        .execute(&empty_base, &historical)
        .expect("historical seed");
    assert!(matches!(
        stream.execute(&base, &empty_info),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));

    let newer = NumericFrame {
        identity: historical.identity.clone(),
        timestamps_ms: vec![ms("2024-01-01T00:00:00Z")],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(9.0)])]),
    };
    let mut regression = MergeStream::new(spec).expect("stream");
    regression
        .execute(&empty_base, &newer)
        .expect("new informative chunk");
    assert!(matches!(
        regression.execute(&empty_base, &historical),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
}

#[test]
fn ffill_leading_repair_uses_one_latest_historical_row_for_the_whole_prefix() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![
            ms("2024-01-01T00:00:00Z"),
            ms("2024-01-01T00:20:00Z"),
            ms("2024-01-01T00:30:00Z"),
            ms("2024-01-01T00:55:00Z"),
            ms("2024-01-01T01:00:00Z"),
        ],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(0.0); 5])]),
    };
    let informative = NumericFrame {
        identity: identity("ETH/USDT", "1h"),
        timestamps_ms: vec![
            ms("2023-12-31T23:00:00Z"),
            ms("2023-12-31T23:30:00Z"),
            ms("2024-01-01T00:00:00Z"),
        ],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(1.0), Some(2.0), Some(3.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: informative.identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let merged = merge(&base, &informative, &spec).expect("merge");
    assert_eq!(
        merged.columns["info_1h"],
        vec![Some(2.0), Some(2.0), Some(2.0), Some(3.0), Some(3.0)]
    );
    assert_eq!(
        merged.informative_dates_ms["date_1h"],
        vec![
            Some(ms("2023-12-31T23:30:00Z")),
            Some(ms("2023-12-31T23:30:00Z")),
            Some(ms("2023-12-31T23:30:00Z")),
            Some(ms("2024-01-01T00:00:00Z")),
            Some(ms("2024-01-01T00:00:00Z")),
        ]
    );
}

#[test]
fn stream_retention_is_constant_across_a_long_informative_only_run() {
    let base_identity = identity("ETH/USDT", "5m");
    let informative_identity = identity("ETH/USDT", "1h");
    let empty_base = NumericFrame {
        identity: base_identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("base".to_owned(), Vec::new())]),
    };
    let spec = MergeSpec {
        base: base_identity,
        informative: informative_identity.clone(),
        ffill: true,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    for hour in 0_i64..256 {
        let value = f64::from(u32::try_from(hour).expect("test hour fits u32"));
        let informative = NumericFrame {
            identity: informative_identity.clone(),
            timestamps_ms: vec![hour * 3_600_000],
            columns: BTreeMap::from([("info".to_owned(), vec![Some(value)])]),
        };
        stream
            .execute(&empty_base, &informative)
            .expect("ordered informative chunk");
        assert_eq!(stream.retained(), 1);
    }
}

#[test]
fn stream_rejects_numeric_schema_drift_across_calls() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let informative = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(2.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: informative.identity.clone(),
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    stream.execute(&base, &informative).expect("first chunk");
    let second_base = NumericFrame {
        identity: base.identity.clone(),
        timestamps_ms: vec![300_000],
        columns: base.columns.clone(),
    };
    let drifted_informative = NumericFrame {
        identity: informative.identity,
        timestamps_ms: vec![300_000],
        columns: BTreeMap::from([("renamed".to_owned(), vec![Some(2.0)])]),
    };
    assert!(matches!(
        stream.execute(&second_base, &drifted_informative),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
}

#[test]
fn non_ffill_stream_rejects_informative_only_chunks_without_losing_exact_rows() {
    let base_identity = identity("ETH/USDT", "5m");
    let informative_identity = identity("ETH/USDT", "5m");
    let empty_base = NumericFrame {
        identity: base_identity.clone(),
        timestamps_ms: Vec::new(),
        columns: BTreeMap::from([("base".to_owned(), Vec::new())]),
    };
    let informative = NumericFrame {
        identity: informative_identity.clone(),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(9.0)])]),
    };
    let spec = MergeSpec {
        base: base_identity.clone(),
        informative: informative_identity,
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let mut stream = MergeStream::new(spec).expect("stream");
    assert!(matches!(
        stream.execute(&empty_base, &informative),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
    let base = NumericFrame {
        identity: base_identity,
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let replayed = stream
        .execute(&base, &informative)
        .expect("replayed exact row");
    assert_eq!(replayed.columns["info_5m"], vec![Some(9.0)]);
}

#[test]
fn merge_uses_freqtrade_floor_minutes_not_resample_milliseconds() {
    let slow_base = NumericFrame {
        identity: identity("ETH/USDT", "30s"),
        timestamps_ms: vec![60_000],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(1.0)])]),
    };
    let one_minute = NumericFrame {
        identity: identity("ETH/USDT", "1m"),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(2.0)])]),
    };
    let slow_spec = MergeSpec {
        base: slow_base.identity.clone(),
        informative: one_minute.identity.clone(),
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let visible = merge(&slow_base, &one_minute, &slow_spec).expect("30s <- 1m");
    assert_eq!(visible.columns["info_1m"], vec![Some(2.0)]);

    let second_base = NumericFrame {
        identity: identity("ETH/USDT", "1s"),
        timestamps_ms: vec![1_000],
        columns: BTreeMap::from([("base".to_owned(), vec![Some(3.0)])]),
    };
    let thirty_seconds = NumericFrame {
        identity: identity("ETH/USDT", "30s"),
        timestamps_ms: vec![1_000],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(4.0)])]),
    };
    let equal_minutes = MergeSpec {
        base: second_base.identity.clone(),
        informative: thirty_seconds.identity.clone(),
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    let equal = merge(&second_base, &thirty_seconds, &equal_minutes).expect("0m == 0m");
    assert_eq!(equal.columns["info_30s"], vec![Some(4.0)]);

    let faster = MergeSpec {
        base: one_minute.identity.clone(),
        informative: slow_base.identity.clone(),
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    assert!(matches!(
        merge(&one_minute, &slow_base, &faster),
        Err(VectorCoreError::Execution { node, .. }) if node == "n7"
    ));
}

#[test]
fn timeframe_supports_ccxt_seconds_years_and_rejects_i64_overflow() {
    assert_eq!(
        Timeframe::parse("1s")
            .expect("seconds")
            .resample_duration_ms(),
        1_000
    );
    assert_eq!(
        Timeframe::parse("1y")
            .expect("years")
            .resample_duration_ms(),
        365 * 86_400_000
    );
    assert_eq!(Timeframe::parse("30s").expect("seconds").merge_minutes(), 0);
    assert!(matches!(
        Timeframe::parse("9223372036854776s"),
        Err(VectorCoreError::InvalidProgram(_))
    ));
}

#[test]
fn collisions_and_cross_pair_identity_fail_at_the_declared_source() {
    let base = NumericFrame {
        identity: identity("ETH/USDT", "5m"),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("info_1h".to_owned(), vec![Some(1.0)])]),
    };
    let informative = NumericFrame {
        identity: identity("BTC/USDT", "1h"),
        timestamps_ms: vec![0],
        columns: BTreeMap::from([("info".to_owned(), vec![Some(2.0)])]),
    };
    let spec = MergeSpec {
        base: base.identity.clone(),
        informative: informative.identity.clone(),
        ffill: false,
        append_timeframe: true,
        suffix: None,
        date_column: "date".to_owned(),
        source: source(),
    };
    assert!(
        matches!(merge(&base, &informative, &spec), Err(VectorCoreError::Execution { node, .. }) if node == "n7")
    );
    let wrong_pair = NumericFrame {
        identity: identity("SOL/USDT", "1h"),
        ..informative
    };
    assert!(
        matches!(merge(&base, &wrong_pair, &spec), Err(VectorCoreError::Execution { node, .. }) if node == "n7")
    );
}

fn spec(case: &Value) -> MergeSpec {
    MergeSpec {
        base: identity(
            case["base_pair"].as_str().expect("base pair"),
            case["call"]["timeframe"].as_str().expect("base timeframe"),
        ),
        informative: identity(
            case["informative_pair"].as_str().expect("informative pair"),
            case["call"]["timeframe_inf"]
                .as_str()
                .expect("informative timeframe"),
        ),
        ffill: case["call"]["ffill"].as_bool().expect("ffill"),
        append_timeframe: case["call"]["append_timeframe"]
            .as_bool()
            .expect("append timeframe"),
        suffix: case["call"]
            .get("suffix")
            .and_then(Value::as_str)
            .map(str::to_owned),
        date_column: case["call"]
            .get("date_column")
            .and_then(Value::as_str)
            .unwrap_or("date")
            .to_owned(),
        source: source(),
    }
}

fn source() -> SourceLocation {
    SourceLocation::new("n7", "strategy.py", 7, 3)
}

fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
    FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
}

fn frame(value: &Value, pair: &str, timeframe: &str) -> NumericFrame {
    let names = value["columns"].as_array().expect("columns");
    let rows = value["rows"].as_array().expect("rows");
    let mut columns = names
        .iter()
        .skip(1)
        .map(|name| (name.as_str().expect("name").to_owned(), Vec::new()))
        .collect::<BTreeMap<_, _>>();
    let mut timestamps_ms = Vec::new();
    for row in rows {
        let row = row.as_array().expect("row");
        timestamps_ms.push(ms(row[0].as_str().expect("date")));
        for (index, name) in names.iter().enumerate().skip(1) {
            columns
                .get_mut(name.as_str().expect("name"))
                .expect("column")
                .push(number(&row[index]));
        }
    }
    NumericFrame {
        identity: identity(pair, timeframe),
        timestamps_ms,
        columns,
    }
}

fn assert_expected(actual: &super::MergedFrame, expected: &Value, case: &str) {
    let names = expected["columns"].as_array().expect("output columns");
    let rows = expected["rows"].as_array().expect("output rows");
    assert_eq!(actual.timestamps_ms.len(), rows.len(), "{case}");
    for (index, row) in rows.iter().enumerate() {
        let row = row.as_array().expect("output row");
        assert_eq!(
            actual.timestamps_ms[index],
            ms(row[0].as_str().expect("date")),
            "{case} row {index}"
        );
        for (column_index, name) in names.iter().enumerate().skip(1) {
            let name = name.as_str().expect("output name");
            if actual.informative_dates_ms.contains_key(name) {
                assert_eq!(
                    actual.informative_dates_ms[name][index],
                    row[column_index].as_str().map(ms),
                    "{case} {name} row {index}"
                );
            } else {
                assert_number(
                    actual.columns[name][index],
                    number(&row[column_index]),
                    case,
                    name,
                    index,
                );
            }
        }
    }
}

fn concatenate(first: &super::MergedFrame, second: &super::MergedFrame) -> super::MergedFrame {
    let mut result = first.clone();
    result
        .timestamps_ms
        .extend_from_slice(&second.timestamps_ms);
    for (name, values) in &second.columns {
        result
            .columns
            .get_mut(name)
            .expect("shared column")
            .extend_from_slice(values);
    }
    for (name, values) in &second.informative_dates_ms {
        result
            .informative_dates_ms
            .get_mut(name)
            .expect("shared date column")
            .extend_from_slice(values);
    }
    result
}

fn slice(frame: &NumericFrame, start: usize, end: usize) -> NumericFrame {
    NumericFrame {
        identity: frame.identity.clone(),
        timestamps_ms: frame.timestamps_ms[start..end].to_vec(),
        columns: frame
            .columns
            .iter()
            .map(|(name, values)| (name.clone(), values[start..end].to_vec()))
            .collect(),
    }
}

fn assert_merged_bits(actual: &super::MergedFrame, expected: &super::MergedFrame) {
    assert_eq!(actual.timestamps_ms, expected.timestamps_ms);
    assert_eq!(actual.informative_dates_ms, expected.informative_dates_ms);
    for (name, expected) in &expected.columns {
        for (actual, expected) in actual.columns[name].iter().zip(expected) {
            assert_number(*actual, *expected, "stream", name, 0);
        }
    }
}

fn assert_number(actual: Option<f64>, expected: Option<f64>, case: &str, name: &str, row: usize) {
    match (actual, expected) {
        (None, None) => {}
        (Some(actual), Some(expected)) => assert_eq!(
            actual.to_bits(),
            expected.to_bits(),
            "{case} {name} row {row}"
        ),
        _ => panic!("{case} {name} row {row}: nullable value differs"),
    }
}

fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Null => None,
        Value::String(encoded) => Some(match encoded.as_str() {
            "f64:nan" => f64::NAN,
            "f64:inf" => f64::INFINITY,
            "f64:-inf" => f64::NEG_INFINITY,
            _ => f64::from_bits(
                u64::from_str_radix(encoded.strip_prefix("f64:0x").expect("f64 token"), 16)
                    .expect("f64 bits"),
            ),
        }),
        _ => panic!("expected nullable f64 token"),
    }
}

fn ms(value: &str) -> i64 {
    let year = value[0..4].parse::<i64>().expect("year");
    let month = value[5..7].parse::<u8>().expect("month");
    let day = value[8..10].parse::<u8>().expect("day");
    let hour = value[11..13].parse::<i64>().expect("hour");
    let minute = value[14..16].parse::<i64>().expect("minute");
    let second = value[17..19].parse::<i64>().expect("second");
    super::days_from_civil(year, month, day).expect("date") * 86_400_000
        + hour * 3_600_000
        + minute * 60_000
        + second * 1_000
}
