//! Global chronological scheduling and aggregate result contracts.

use super::*;

#[test]
fn futures_ignores_simultaneous_long_and_short_entries() {
    let signal = EntrySignal {
        tag: Some("conflict".to_owned()),
        leverage: None,
        liquidation_price: None,
    };
    let mut conflict = candle(1, 100.0, 100.0);
    conflict.enter_long = Some(signal.clone());
    conflict.enter_short = Some(signal);
    let mut futures_config = config(1);
    futures_config.is_futures = true;
    futures_config.leverage = Some(3.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: futures_config,
        pairs: vec![nfi_pair(
            vec![conflict, candle(2, 101.0, 101.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("Freqtrade suppresses a conflicting entry candle");

    assert!(result.trades.is_empty());
}

#[test]
fn same_side_exit_signal_suppresses_entry() {
    let mut conflict = candle(1, 100.0, 100.0);
    conflict.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    conflict.exit_long = Some(ExitSignal {
        reason: "exit_signal".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![nfi_pair(
            vec![conflict, candle(2, 101.0, 101.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("Freqtrade suppresses entry beside a same-side exit");

    assert!(result.trades.is_empty());
}

#[test]
fn futures_reopens_the_opposite_side_after_a_same_candle_exit() {
    let mut short_entry = candle(0, 100.0, 100.0);
    short_entry.enter_short = Some(EntrySignal {
        tag: Some("short".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut reversal = candle(1, 95.0, 95.0);
    reversal.enter_long = Some(EntrySignal {
        tag: Some("long".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    reversal.exit_short = Some(ExitSignal {
        reason: "short-exit".to_owned(),
    });
    let mut long_exit = candle(2, 96.0, 96.0);
    long_exit.exit_long = Some(ExitSignal {
        reason: "long-exit".to_owned(),
    });
    let mut futures_config = config(1);
    futures_config.is_futures = true;
    futures_config.leverage = Some(2.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: futures_config,
        pairs: vec![nfi_pair(
            vec![short_entry, reversal, long_exit],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("Freqtrade futures same-candle reversal");

    assert_eq!(result.trades.len(), 2);
    assert!(result.trades[0].is_short);
    assert_eq!(result.trades[0].close_timestamp_ms, 1);
    assert!(!result.trades[1].is_short);
    assert_eq!(result.trades[1].open_timestamp_ms, 1);
    assert_eq!(result.trades[1].entry_tag.as_deref(), Some("long"));
    assert_eq!(result.rejected_signals, 0);
}

#[test]
fn adjustment_minimum_stake_uses_unleveraged_freqtrade_boundary() {
    let mut pair = nfi_pair(Vec::new(), BTreeMap::new());
    pair.minimum_amount = Some(1.0);
    pair.minimum_cost = Some(5.0);

    let adjustment_minimum = adjustment_minimum_pair_stake(&pair, 17.213, 0.05);
    let leverage_aware_minimum = minimum_pair_stake(&pair, 17.213, -0.1, 3.0, 0.05);

    // The APE futures market is amount-limited at one contract. Freqtrade
    // exposes 17.213 * 1.05 to adjust_trade_position even on a 3x trade.
    assert!((adjustment_minimum - 18.07365).abs() < 1e-12);
    assert!((leverage_aware_minimum * 3.0 - adjustment_minimum).abs() < 1e-12);
}

#[test]
fn ft_precise_partial_exit_division_preserves_integer_contract() {
    let raw_amount =
        precise_product_quotient(2_913.868_487_754_348_3, 2_616.0, 2_927.296_453_135_704_3)
            .expect("valid Freqtrade partial-exit conversion");

    // These are the pinned X7 trade values immediately before order 145.
    // Unlimited rational division lands just below 2604 and loses one
    // integer contract; CCXT Precise's 18-place division lands above it.
    assert_eq!(floor_step(raw_amount, 1.0), Ok(2_604.0));
}

#[test]
fn nfi_grind_wallet_rejection_stops_source_order_evaluation() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("141 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let adjustment_candle = candle(7 * HOUR, 90.0, 90.0);
    let mut force_exit = candle(8 * HOUR, 90.0, 90.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    let adjustment = manager
        .position_adjustment
        .as_mut()
        .expect("test manager has position adjustment");
    adjustment.enabled = true;
    // With 900 USDT already tied up, the first source-ordered grind asks
    // for 180 USDT while the wallet has less than 100 USDT available.
    // Grind 4 would fit at 45 USDT, but NFI returns None at grind 1 and
    // never evaluates that later branch.
    adjustment.constants.grinds[0].enabled = true;
    adjustment.constants.grinds[0].stakes_spot = vec![0.2];
    adjustment.constants.grinds[3].enabled = true;
    adjustment.constants.grinds[3].stakes_spot = vec![0.05];

    let mut portfolio = config(1);
    portfolio.starting_balance = 1_000.0;
    portfolio.stake_amount = 900.0;
    enable_nfi_manager(&mut portfolio, manager);
    let values = |value| vec![Value::from(value), Value::from(value), Value::from(value)];
    let mut pair = nfi_pair(
        vec![entry, adjustment_candle, force_exit],
        BTreeMap::from([
            ("RSI_3".to_owned(), values(50.0)),
            ("RSI_3_15m".to_owned(), values(50.0)),
            ("RSI_14".to_owned(), values(50.0)),
            ("close".to_owned(), values(90.0)),
            ("EMA_20".to_owned(), values(90.0)),
        ]),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![pair],
    })
    .expect("wallet rejection is a normal NFI callback result");

    assert_eq!(result.trades[0].orders.len(), 2);
    assert_eq!(result.trades[0].orders[0].tag.as_deref(), Some("141 "));
    assert_eq!(
        result.trades[0].orders[1].tag.as_deref(),
        Some("force_exit")
    );
}

#[test]
fn global_slot_competition_uses_pair_order() {
    let mut first = candle(1, 100.0, 100.0);
    first.enter_long = Some(EntrySignal {
        tag: Some("first".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut second = first.clone();
    second.enter_long = Some(EntrySignal {
        tag: Some("second".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![
            PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![first, candle(2, 100.0, 100.0)].into(),
            },
            PairSeries {
                pair: "BBB/USDT".to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![second, candle(2, 100.0, 100.0)].into(),
            },
        ],
    };

    let result = simulate(&input).expect("valid simulation");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].pair, "AAA/USDT");
    assert_eq!(result.rejected_signals, 1);
}

#[test]
fn final_force_exits_export_newest_open_trade_first() {
    let pair = |name: &str, entry_timestamp_ms: i64| {
        let mut entry = candle(entry_timestamp_ms, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some(name.to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        PairSeries {
            pair: name.to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, candle(2, 100.0, 100.0)].into(),
        }
    };
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(2),
        pairs: vec![pair("OLDER/USDT", 0), pair("NEWER/USDT", 1)],
    };

    let result = simulate(&input).expect("valid force-exit ordering simulation");

    assert_eq!(result.trades.len(), 2);
    assert_eq!(result.trades[0].pair, "NEWER/USDT");
    assert_eq!(result.trades[1].pair, "OLDER/USDT");
    assert!(result
        .trades
        .iter()
        .all(|trade| trade.exit_reason == "force_exit"));
}

#[test]
fn open_trade_pairs_run_before_configured_pair_order() {
    let mut later_entry = candle(1, 100.0, 100.0);
    later_entry.enter_long = Some(EntrySignal {
        tag: Some("after-close".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut initial_entry = candle(0, 100.0, 100.0);
    initial_entry.enter_long = Some(EntrySignal {
        tag: Some("initial".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut same_candle_exit = candle(1, 101.0, 101.0);
    same_candle_exit.exit_long = Some(ExitSignal {
        reason: "scheduled-exit".to_owned(),
    });
    let pair = |name: &str, candles: Vec<Candle>| PairSeries {
        pair: name.to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: candles.into(),
    };
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        // AAA is deliberately first in configured order. Freqtrade still
        // processes BBB first at timestamp 1 because BBB has an open
        // trade, freeing the sole slot before AAA's entry is evaluated.
        pairs: vec![
            pair(
                "AAA/USDT",
                vec![
                    candle(0, 100.0, 100.0),
                    later_entry,
                    candle(2, 100.0, 100.0),
                ],
            ),
            pair(
                "BBB/USDT",
                vec![initial_entry, same_candle_exit, candle(2, 101.0, 101.0)],
            ),
        ],
    };

    let result = simulate(&input).expect("valid open-trade-first simulation");

    assert_eq!(result.trades.len(), 2);
    assert_eq!(result.trades[0].pair, "BBB/USDT");
    assert_eq!(result.trades[1].pair, "AAA/USDT");
    assert_eq!(result.rejected_signals, 0);
}

#[test]
fn profiled_simulation_preserves_result_and_counts_only_visible_rows() {
    let mut first = candle(1, 100.0, 100.0);
    first.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 1,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(0, 100.0, 100.0), first, candle(2, 101.0, 101.0)].into(),
        }],
    };

    let ordinary = simulate(&input).expect("valid ordinary simulation");
    let (profiled, profile) = simulate_profiled(&input).expect("valid profiled simulation");

    assert_eq!(profiled, ordinary);
    assert_eq!(profile.timestamp_batches, 2);
    assert_eq!(profile.pair_events, 2);
}

#[test]
fn sparse_profile_counts_distinct_visible_timestamps_during_validation() {
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![
            PairSeries {
                pair: "AAA/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![
                    candle(0, 100.0, 100.0),
                    candle(1, 100.0, 100.0),
                    candle(2, 100.0, 100.0),
                ]
                .into(),
            },
            PairSeries {
                pair: "BBB/USDT".to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![
                    candle(0, 100.0, 100.0),
                    candle(2, 100.0, 100.0),
                    candle(3, 100.0, 100.0),
                ]
                .into(),
            },
        ],
    };

    let (_, profile) = simulate_profiled(&input).expect("valid profiled simulation");

    assert_eq!(profile.timestamp_batches, 3);
    assert_eq!(profile.pair_events, 4);
}

#[test]
fn timerange_stop_boundary_does_not_open_a_new_trade() {
    let mut boundary = candle(2, 101.0, 100.0);
    boundary.enter_long = Some(EntrySignal {
        tag: Some("boundary".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![boundary].into(),
        }],
    };

    let result = simulate(&input).expect("valid stop-boundary candle");

    assert!(result.trades.is_empty());
    assert_eq!(result.rejected_signals, 0);
}

#[test]
fn callback_context_rows_are_visible_but_never_executed() {
    let mut context = candle(1, 90.0, 90.0);
    context.enter_long = Some(EntrySignal {
        tag: Some("context-only".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut executable = candle(2, 100.0, 100.0);
    executable.enter_long = Some(EntrySignal {
        tag: Some("executable".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 1,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![context, executable, candle(3, 101.0, 101.0)].into(),
        }],
    };

    let result = simulate(&input).expect("context boundary is valid");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].open_timestamp_ms, 2);
    assert_eq!(result.trades[0].entry_tag.as_deref(), Some("executable"));
    assert_eq!(result.rejected_signals, 0);
}

#[test]
fn execution_start_index_must_point_to_a_candle() {
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 1,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![candle(1, 100.0, 100.0)].into(),
        }],
    };

    assert_eq!(
        simulate(&input),
        Err(SimError::InvalidExecutionStart {
            pair: "AAA/USDT".to_owned(),
            index: 1,
            rows: 1,
        })
    );
}

#[test]
fn checked_ieee_families_preserve_left_to_right_bits_and_reject_overflow() {
    let families: &[&[f64]] = &[
        &[0.1, 0.2, 0.3],
        &[1.0e16, 1.0, -1.0e16],
        &[-0.0, 2.0, -0.5, 0.25],
        &[f64::MIN_POSITIVE, 0.5, 2.0],
    ];
    for values in families {
        let expected_sum = values.iter().fold(0.0, |total, value| total + value);
        let expected_product = values.iter().fold(1.0, |total, value| total * value);
        assert_eq!(
            checked_float_sum(values, "family-sum")
                .expect("finite family sum")
                .to_bits(),
            expected_sum.to_bits()
        );
        assert_eq!(
            checked_float_product(values, "family-product")
                .expect("finite family product")
                .to_bits(),
            expected_product.to_bits()
        );
    }
    assert_eq!(
        checked_float_sum(&[f64::MAX, f64::MAX], "family-sum"),
        Err(SimError::ExactArithmetic {
            operation: "family-sum"
        })
    );
    assert_eq!(
        checked_float_product(&[f64::MAX, 2.0], "family-product"),
        Err(SimError::ExactArithmetic {
            operation: "family-product"
        })
    );
}

#[test]
fn pairwise_profit_sum_matches_numpy_reduction_order() {
    let profits = [
        13.433_598_31,
        5.716_389_78,
        8.516_438_52,
        1.152_679_260_000_020_2,
        2.817_485_03,
        2.228_106_82,
        0.982_624_96,
        0.735_159,
        2.030_196_569_999_998,
        2.782_651_25,
        2.093_312_4,
        0.941_256_3,
    ];

    assert_eq!(pairwise_sum(&profits), 43.429_898_200_000_025);
    assert_eq!(
        checked_pairwise_sum(&profits, "test-profit-total"),
        Ok(43.429_898_200_000_025)
    );
}

#[test]
fn pairwise_profit_sum_matches_x7_annual_pandas_token() {
    let profits = [
        145.507_105_8,
        1_169.701_240_65,
        753.539_616,
        382.422_002_739_998_7,
        627.860_778,
        284.871_360_94,
        576.035_552,
        417.658_364_52,
        248.585_082_58,
        541.245_411_6,
        -4_831.775_913_230_002_5,
    ];

    assert_eq!(pairwise_sum(&profits), 315.650_601_599_995_75);
}

#[test]
fn total_volume_uses_cpython_compensated_sum() {
    // Costs are the exact serialized values from the latest X7 tag-120
    // ZEC fixture. A naive Rust fold ends in ...00004; CPython/Freqtrade
    // exports ...9999.
    let costs = [
        32.994_561_599_999_99,
        24.689_464_799_999_996,
        39.540_500_999_999_99,
        40.349_809_499_999_99,
        39.569_630_1,
        32.969_036_1,
        33.636_302_699_999_995,
        40.446_706_299_999_995,
        39.507_467_999_999_99,
        40.566_726_199_999_99,
        39.462_122_7,
        40.322_482_199_999_996,
        12.908_996_1,
    ];

    assert_eq!(
        checked_python_float_sum(costs, "test-total-volume"),
        Ok(456.963_807_299_999_9)
    );
    assert_ne!(costs.into_iter().sum::<f64>(), 456.963_807_299_999_9);
}
