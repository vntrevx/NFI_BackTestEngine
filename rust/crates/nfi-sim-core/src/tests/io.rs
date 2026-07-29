//! File-backed storage and column projection contracts.

use super::*;
use std::io::Write as _;

#[test]
fn file_backed_rows_preserve_forward_and_backward_window_access() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES + FILE_BACKED_FEATURE_BYTES;
    let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
    let row_count = rows_per_window * 2 + 3;
    for index in 0..row_count {
        let feature_value = index.to_f64().expect("test row index fits f64") + 0.5;
        let mut row = vec![0_u8; row_stride];
        row[..8].copy_from_slice(
            &i64::try_from(index)
                .expect("test row index fits i64")
                .to_le_bytes(),
        );
        row[FILE_BACKED_ROW_HEADER_BYTES..].copy_from_slice(&feature_value.to_le_bytes());
        file.write_all(&row).expect("write normalized test row");
    }
    let rows =
        FileBackedRows::new(file, row_count, 1, Vec::new()).expect("open verified test spool");

    for index in [
        0,
        rows_per_window - 1,
        rows_per_window + 10,
        2,
        row_count - 1,
    ] {
        assert_eq!(rows.timestamp_ms(index), i64::try_from(index).ok());
        assert_eq!(
            rows.feature_number(index, 0),
            index.to_f64().map(|value| value + 0.5)
        );
    }
}

#[test]
fn file_backed_rows_keep_callback_lookback_in_the_current_window() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES + FILE_BACKED_FEATURE_BYTES;
    let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
    assert!(rows_per_window > CALLBACK_FEATURE_LOOKBACK_ROWS);
    let row_count = rows_per_window + CALLBACK_FEATURE_LOOKBACK_ROWS + 1;
    for index in 0..row_count {
        let mut row = vec![0_u8; row_stride];
        row[..8].copy_from_slice(
            &i64::try_from(index)
                .expect("test row index fits i64")
                .to_le_bytes(),
        );
        file.write_all(&row).expect("write normalized test row");
    }
    let rows =
        FileBackedRows::new(file, row_count, 1, Vec::new()).expect("open verified test spool");

    assert_eq!(
        rows.timestamp_ms(rows_per_window),
        i64::try_from(rows_per_window).ok()
    );
    let retained_start = rows.buffered_window_start();
    assert_eq!(
        retained_start,
        rows_per_window - CALLBACK_FEATURE_LOOKBACK_ROWS
    );
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
        assert_eq!(
            rows.timestamp_ms(rows_per_window - offset),
            i64::try_from(rows_per_window - offset).ok()
        );
        assert_eq!(rows.buffered_window_start(), retained_start);
    }
}

#[test]
fn file_backed_entry_index_reuses_validated_signal_positions() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES;
    let row_count = 12;
    for index in 0..row_count {
        let mut row = vec![0_u8; row_stride];
        row[..8].copy_from_slice(
            &i64::try_from(index)
                .expect("test row index fits i64")
                .to_le_bytes(),
        );
        if matches!(index, 2 | 7) {
            row[72] |= 1 << 3;
        }
        file.write_all(&row).expect("write normalized test row");
    }
    let rows =
        FileBackedRows::new(file, row_count, 0, Vec::new()).expect("open verified test spool");

    assert_eq!(rows.next_entry_index(0), Some(2));
    rows.install_entry_indices(vec![2, 7]);
    assert_eq!(rows.next_entry_index(3), Some(7));
    assert_eq!(rows.next_entry_index(8), None);
    assert_eq!(rows.installed_entry_indices(), Some(&[2, 7][..]));
}

#[test]
fn callback_feature_index_selects_the_last_closed_analyzed_row() {
    assert_eq!(callback_feature_index(0), None);
    assert_eq!(callback_feature_index(1), Some(0));
    assert_eq!(callback_feature_index(42), Some(41));
}

#[test]
fn exchange_step_quantization_uses_decimal_ticks() {
    assert_eq!(floor_step(8.45, 0.01), 8.45);
    assert_eq!(floor_step(0.459_999_999_999_999_1, 0.01), 0.45);
    assert_eq!(ceil_step(0.044_361, 0.0001), 0.0444);
    assert_eq!(round_step(20.562_49, 0.0001), 20.5625);
}

#[test]
fn pair_price_step_selects_the_latest_historical_change() {
    let mut pair = nfi_pair(vec![candle(10, 5.0, 4.0)], BTreeMap::new());
    pair.price_step = Some(0.0001);
    pair.price_steps = vec![
        PriceStepChange {
            timestamp_ms: 1,
            step: 0.0001,
        },
        PriceStepChange {
            timestamp_ms: 9,
            step: 0.001,
        },
    ];

    assert_eq!(
        pair_price_step(&pair, &pair.candles.get(0).expect("fixture candle"), 0.01),
        0.001
    );
    assert_eq!(pair_price_step(&pair, &candle(5, 5.0, 4.0), 0.01), 0.0001);
}

#[test]
fn columnar_features_reconstruct_the_exact_selected_and_previous_rows() {
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::from([
            (
                "RSI_14".to_owned(),
                FeatureColumn::numbers(vec![41.0, f64::NAN]),
            ),
            (
                "protections_long_global".to_owned(),
                FeatureColumn::booleans(vec![false, true]),
            ),
        ]),
        candles: vec![candle(1, 100.0, 100.0), candle(2, 101.0, 101.0)].into(),
    };
    let mut variables = BTreeMap::new();

    insert_feature_window(&mut variables, &pair, 1).expect("aligned feature window");

    assert_eq!(
        variables["last_candle"],
        serde_json::json!({
            "open": 101.0,
            "high": 111.0,
            "low": 101.0,
            "close": 101.0,
            "volume": 1.0,
            "RSI_14": {"$float": "nan"},
            "protections_long_global": true
        })
    );
    assert_eq!(variables["previous_candle"]["RSI_14"], 41.0);
    assert_eq!(variables["previous_candle_1"], variables["previous_candle"]);
    assert_eq!(variables["previous_candle_2"], Value::Null);
}
