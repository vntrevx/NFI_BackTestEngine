//! File-backed storage and column projection contracts.

use super::*;
use crate::calculations::precise_sum;
use crate::execution::CloseTradeContext;
use crate::portfolio::OpenTrade;
use std::fs::File;
use std::io::Write as _;
use std::rc::Rc;

fn open_trade_file_backed_fixture() -> (SimulationInput, Rc<FileBackedRows>, File, i64) {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES;
    let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
    let row_count = rows_per_window + 1;
    for index in 0..row_count {
        let timestamp = i64::try_from(index + 1).expect("test timestamp fits i64");
        let mut row = vec![0_u8; row_stride];
        row[..8].copy_from_slice(&timestamp.to_le_bytes());
        for offset in [8, 16, 24, 32] {
            row[offset..offset + 8].copy_from_slice(&100.0_f64.to_le_bytes());
        }
        row[40..48].copy_from_slice(&1.0_f64.to_le_bytes());
        if index == 0 {
            row[72] |= 1 << 3;
        }
        file.write_all(&row).expect("write normalized test row");
    }
    let mutator = file.try_clone().expect("clone spool mutation handle");
    let rows = FileBackedRows::new(file, row_count, 0, Vec::new())
        .expect("open verified file-backed rows");
    let mut pair = nfi_pair(vec![candle(1, 100.0, 100.0)], BTreeMap::new());
    pair.candles = CandleSeries::file_backed(Rc::clone(&rows));
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![pair],
    };
    (
        input,
        rows,
        mutator,
        i64::try_from(row_count).expect("test row count fits i64"),
    )
}

fn ordinary_open_trade() -> (OpenTrade, PortfolioConfig) {
    let portfolio = config(1);
    let mut entry = candle(1, 1.0, 1.0);
    entry.high = 1.0;
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let pair = nfi_pair(vec![entry, candle(2, 1.0, 1.0)], BTreeMap::new());
    let entry_candle = pair.candles.get(0).expect("entry candle");
    let signal = entry_candle.enter_long.as_ref().expect("entry signal");
    let trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal,
            stake: EntryStake {
                proposed: 1.0,
                maximum: 1.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &portfolio,
    )
    .expect("representable entry")
    .expect("sized entry");
    (trade, portfolio)
}

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
        assert_eq!(rows.timestamp_ms(index), Ok(i64::try_from(index).ok()));
        assert_eq!(
            rows.feature_number(index, 0),
            Ok(index.to_f64().map(|value| value + 0.5))
        );
    }
}

#[test]
fn truncated_file_backing_returns_a_typed_read_error_without_panicking() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let mut row = vec![0_u8; FILE_BACKED_ROW_HEADER_BYTES];
    row[..8].copy_from_slice(&42_i64.to_le_bytes());
    file.write_all(&row).expect("write normalized test row");
    let mutator = file.try_clone().expect("clone spool handle");
    let rows = FileBackedRows::new(file, 1, 0, Vec::new()).expect("open verified test spool");
    mutator.set_len(0).expect("truncate spool after validation");

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| rows.timestamp_ms(0)));

    assert!(outcome.is_ok(), "file-backed read must not panic");
    assert!(matches!(
        outcome.expect("checked non-panic outcome"),
        Err(SimError::SpoolIo {
            operation: "read",
            row: 0,
            kind: "unexpected-eof",
        })
    ));
}

#[test]
fn injected_spool_faults_preserve_stable_operation_and_kind_context() {
    for (operation, kind) in [
        ("seek", "invalid-input"),
        ("read", "permission-denied"),
        ("storage", "storage-full"),
        ("read", "closed"),
    ] {
        let mut file = tempfile::tempfile().expect("create private row spool");
        file.write_all(&[0_u8; FILE_BACKED_ROW_HEADER_BYTES])
            .expect("write normalized test row");
        let rows = FileBackedRows::new(file, 1, 0, Vec::new()).expect("open verified test spool");
        rows.inject_failure(operation, 0, kind);

        assert_eq!(
            rows.timestamp_ms(0),
            Err(SimError::SpoolIo {
                operation,
                row: 0,
                kind,
            })
        );
    }
}

#[test]
fn simulation_preserves_the_underlying_spool_fault_over_generic_validation_errors() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    file.write_all(&[0_u8; FILE_BACKED_ROW_HEADER_BYTES])
        .expect("write normalized test row");
    let rows = FileBackedRows::new(file, 1, 0, Vec::new()).expect("open verified test spool");
    rows.inject_failure("read", 0, "permission-denied");
    let mut pair = nfi_pair(vec![candle(1, 100.0, 100.0)], BTreeMap::new());
    pair.candles = CandleSeries::file_backed(rows);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![pair],
    };

    assert_eq!(
        simulate(&input),
        Err(SimError::SpoolIo {
            operation: "read",
            row: 0,
            kind: "permission-denied",
        })
    );
}

#[test]
fn post_validation_truncation_invalidates_a_previously_readable_window() {
    let mut file = tempfile::tempfile().expect("create private row spool");
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES;
    let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
    let row_count = rows_per_window + 1;
    file.write_all(&vec![0_u8; row_count * row_stride])
        .expect("write normalized test rows");
    let mutator = file.try_clone().expect("clone spool handle");
    let rows =
        FileBackedRows::new(file, row_count, 0, Vec::new()).expect("open verified test spool");
    assert_eq!(rows.timestamp_ms(row_count - 1), Ok(Some(0)));
    mutator.set_len(0).expect("truncate validated spool");

    assert_eq!(
        rows.timestamp_ms(0),
        Err(SimError::SpoolIo {
            operation: "read",
            row: 0,
            kind: "unexpected-eof",
        })
    );
}

#[test]
fn open_trade_force_close_propagates_real_post_validation_truncation() {
    let (input, rows, mutator, final_timestamp) = open_trade_file_backed_fixture();
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        simulate_with_observer(&input, |event| {
            if event.timestamp_ms == final_timestamp {
                assert_eq!(rows.timestamp_ms(0), Ok(Some(1)));
                mutator
                    .set_len(0)
                    .expect("truncate spool at final observer event");
            }
        })
    }));

    assert!(outcome.is_ok(), "force-close spool read must not panic");
    assert_eq!(
        outcome.expect("checked non-panic result"),
        Err(SimError::SpoolIo {
            operation: "read",
            row: rows.len() - 1,
            kind: "unexpected-eof",
        })
    );
}

#[test]
fn open_trade_force_close_propagates_event_synchronized_spool_faults() {
    for (operation, kind) in [
        ("seek", "invalid-input"),
        ("read", "permission-denied"),
        ("storage", "storage-full"),
        ("read", "closed"),
    ] {
        let (input, rows, _mutator, final_timestamp) = open_trade_file_backed_fixture();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            simulate_with_observer(&input, |event| {
                if event.timestamp_ms == final_timestamp {
                    rows.inject_failure(operation, rows.len() - 1, kind);
                }
            })
        }));

        assert!(outcome.is_ok(), "injected force-close fault must not panic");
        assert_eq!(
            outcome.expect("checked non-panic result"),
            Err(SimError::SpoolIo {
                operation,
                row: rows.len() - 1,
                kind,
            })
        );
    }
}

#[test]
fn close_trade_propagates_total_entry_value_overflow() {
    let (mut trade, mut portfolio) = ordinary_open_trade();
    portfolio.fee_open_rate = Some(0.001);
    portfolio.fee_close_rate = Some(0.0);
    trade.leverage = 2.0;
    trade.amount = f64::MAX;
    trade.open_rate = 1.0;
    trade.price_step = 1.0;
    trade.stake_amount = 1.0;
    trade.max_stake_amount = 1.0;
    trade.entry_cost_with_fees = 1.0;
    trade.orders[0].amount = f64::MAX;
    trade.orders[0].price = 1.0;

    assert!(matches!(
        close_trade(
            trade,
            2,
            1.0,
            "force_exit".to_owned(),
            &portfolio,
            CloseTradeContext {
                sequence: 0,
                order_id: 2,
                executable_callbacks: None,
                wallet_available_before: 0.0,
            },
        ),
        Err(SimError::ExactArithmetic {
            operation: "precise-product"
        })
    ));
}

#[test]
fn spot_replay_rejects_overflowing_exact_totals() {
    let (mut trade, mut portfolio) = ordinary_open_trade();
    portfolio.fee_rate = 0.0;
    trade.orders[0].amount = f64::MAX;
    trade.orders[0].price = 0.75;
    let mut second = trade.orders[0].clone();
    second.id = 2;
    second.sequence = 1;
    trade.orders.push(second);

    assert!(matches!(
        replay_spot_profit(&trade, &portfolio),
        Err(SimError::ExactArithmetic {
            operation: "spot-total-entry-value"
        })
    ));
}

#[test]
fn generated_finite_exponent_boundaries_round_trip_or_reject_derived_overflow() {
    for exponent in -308..=308 {
        let value = format!("1e{exponent}")
            .parse::<f64>()
            .expect("generated finite exponent");
        assert_eq!(
            exact_rational(value).and_then(|number| number.to_f64()),
            Some(value)
        );
    }
    for value in [
        f64::from_bits(1),
        f64::MIN_POSITIVE,
        f64::from_bits(f64::MAX.to_bits() - 1),
        f64::MAX,
    ] {
        assert_eq!(
            exact_rational(value).and_then(|number| number.to_f64()),
            Some(value)
        );
    }
    assert!(matches!(
        precise_sum(&[f64::MAX, f64::MAX]),
        Err(SimError::ExactArithmetic {
            operation: "precise-sum"
        })
    ));
}

#[test]
fn exact_arithmetic_rejects_finite_derived_overflow() {
    assert!(matches!(
        precise_product(&[f64::MAX, f64::MAX]),
        Err(SimError::ExactArithmetic {
            operation: "precise-product"
        })
    ));
}

#[test]
fn public_simulation_rejects_finite_inputs_that_overflow_final_wallet() {
    let mut portfolio = config(1);
    portfolio.starting_balance = f64::MAX;
    portfolio.stake_amount = f64::MAX / 4.0;
    portfolio.fee_rate = 0.0;
    portfolio.amount_step = 1.0;
    portfolio.price_step = 1.0;
    let mut entry = candle(1, 1.0, 1.0);
    entry.high = 1.0;
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 2.0, 2.0);
    exit.high = 2.0;
    exit.exit_long = Some(ExitSignal {
        reason: "wallet-overflow".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![nfi_pair(vec![entry, exit], BTreeMap::new())],
    };

    assert_eq!(
        simulate(&input),
        Err(SimError::ExactArithmetic {
            operation: "wallet-final-balance"
        })
    );
}

#[test]
fn serializer_rejects_non_finite_results_instead_of_emitting_null() {
    let mut entry = candle(1, 1.0, 1.0);
    entry.high = 1.0;
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![nfi_pair(vec![entry, candle(2, 1.0, 1.0)], BTreeMap::new())],
    };
    let mut result = simulate(&input).expect("finite fixture result");
    result.final_balance = f64::INFINITY;

    let error = serialize_simulation_result(&result).expect_err("non-finite result must fail");

    assert!(error.to_string().contains("non-finite number"));
}

#[test]
fn exact_exit_arithmetic_rejects_extreme_finite_derived_overflow() {
    let mut entry = candle(1, 1.0, 1.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, f64::MAX, 1.0);
    exit.high = f64::MAX;
    exit.exit_long = Some(ExitSignal {
        reason: "extreme-exit".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![nfi_pair(vec![entry, exit], BTreeMap::new())],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::ExactArithmetic {
            operation: "exit-order-gross-proceeds"
        })
    ));
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
        Ok(i64::try_from(rows_per_window).ok())
    );
    let retained_start = rows.buffered_window_start();
    assert_eq!(
        retained_start,
        rows_per_window - CALLBACK_FEATURE_LOOKBACK_ROWS
    );
    for offset in 1..=CALLBACK_FEATURE_LOOKBACK_ROWS {
        assert_eq!(
            rows.timestamp_ms(rows_per_window - offset),
            Ok(i64::try_from(rows_per_window - offset).ok())
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

    assert_eq!(rows.next_entry_index(0), Ok(Some(2)));
    rows.install_entry_indices(vec![2, 7]);
    assert_eq!(rows.next_entry_index(3), Ok(Some(7)));
    assert_eq!(rows.next_entry_index(8), Ok(None));
    assert_eq!(rows.installed_entry_indices(), Some(&[2, 7][..]));
}

#[test]
fn callback_feature_index_selects_the_last_closed_analyzed_row() {
    assert_eq!(callback_feature_index(0), None);
    assert_eq!(callback_feature_index(1), Some(0));
    assert_eq!(callback_feature_index(42), Some(41));
}

#[test]
fn valid_exact_decimal_calculations_preserve_freqtrade_values() {
    assert_eq!(
        exact_rational(8.45).and_then(|value| value.to_f64()),
        Some(8.45)
    );
    assert_eq!(precise_product(&[12.5, 8.0, 1.001]), Ok(100.1));
    assert_eq!(floor_step(8.45, 0.01), Ok(8.45));
    assert_eq!(floor_step(0.459_999_999_999_999_1, 0.01), Ok(0.45));
    assert_eq!(ceil_step(0.044_361, 0.0001), Ok(0.0444));
    assert_eq!(round_step(20.562_49, 0.0001), Ok(20.5625));
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
