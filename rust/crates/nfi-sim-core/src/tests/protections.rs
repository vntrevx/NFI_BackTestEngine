//! Pair-lock and protection contracts.

use super::*;

#[test]
fn cooldown_pair_lock_uses_strict_candle_rounding_and_expiry() {
    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::CooldownPeriod {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
        }],
    };
    let closed = vec![protection_trade(
        1,
        "AAA/USDT",
        300_000,
        -0.01,
        "exit_signal",
        TradeSide::Long,
    )];
    let mut state = ProtectionState::default();

    state
        .after_trade_close(&program, &closed[0], &closed, 1_000.0)
        .expect("finite protection arithmetic");

    assert_eq!(
        state.locks(),
        &[PairLockState {
            pair: "AAA/USDT".to_owned(),
            lock_timestamp_ms: 300_000,
            // Requested end 900_000 is already a boundary. CCXT
            // ROUND_UP advances it to the following 5-minute boundary.
            lock_end_timestamp_ms: 1_200_000,
            reason: "Cooldown period for for 10 minutes.".to_owned(),
            side: "*".to_owned(),
            active: true,
        }]
    );
    assert!(state.is_pair_locked("AAA/USDT", 1_199_999, TradeSide::Long));
    assert!(!state.is_pair_locked("AAA/USDT", 1_200_000, TradeSide::Long));
    assert!(!state.is_pair_locked("BBB/USDT", 600_000, TradeSide::Long));
}

#[test]
fn simulator_skips_locked_entry_without_counting_a_rejection() {
    let mut portfolio = config(1);
    portfolio.stoploss_ratio = -0.99;
    portfolio.protection_program = Some(ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::CooldownPeriod {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
        }],
    });
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(300_000, 101.0, 101.0);
    exit.exit_long = Some(ExitSignal {
        reason: "exit_signal".to_owned(),
    });
    let mut locked_signal = candle(600_000, 102.0, 102.0);
    locked_signal.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
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
            candles: vec![entry, exit, locked_signal, candle(900_000, 102.0, 102.0)].into(),
        }],
    };

    let result = simulate(&input).expect("valid cooldown simulation");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.rejected_signals, 0);
    assert_eq!(result.locks.len(), 1);
    assert_eq!(result.locks[0].lock_end_timestamp_ms, 1_200_000);
}

#[test]
fn stoploss_guard_global_lock_respects_trade_side() {
    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::StoplossGuard {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 2,
            only_per_pair: false,
            only_per_side: true,
            required_profit: 0.0,
        }],
    };
    let closed = vec![
        protection_trade(1, "AAA/USDT", 300_000, -0.1, "stop_loss", TradeSide::Long),
        protection_trade(2, "BBB/USDT", 600_000, -0.1, "liquidation", TradeSide::Long),
    ];
    let mut state = ProtectionState::default();

    state
        .after_trade_close(&program, &closed[0], &closed[..1], 1_000.0)
        .expect("finite protection arithmetic");
    state
        .after_trade_close(&program, &closed[1], &closed, 1_000.0)
        .expect("finite protection arithmetic");

    assert_eq!(state.locks().len(), 1);
    assert_eq!(state.locks()[0].pair, "*");
    assert_eq!(state.locks()[0].side, "long");
    assert!(state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Long));
    assert!(!state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Short));
}

#[test]
fn global_stoploss_guard_emits_pair_and_global_locks_for_same_pair_losses() {
    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::StoplossGuard {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 2,
            only_per_pair: false,
            only_per_side: false,
            required_profit: 0.0,
        }],
    };
    let closed = vec![
        protection_trade(1, "AAA/USDT", 300_000, -0.1, "stop_loss", TradeSide::Long),
        protection_trade(2, "AAA/USDT", 600_000, -0.1, "stop_loss", TradeSide::Long),
    ];
    let mut state = ProtectionState::default();

    state
        .after_trade_close(&program, &closed[0], &closed[..1], 1_000.0)
        .expect("finite protection arithmetic");
    state
        .after_trade_close(&program, &closed[1], &closed, 1_000.0)
        .expect("finite protection arithmetic");

    assert_eq!(state.locks().len(), 2);
    assert_eq!(state.locks()[0].pair, "AAA/USDT");
    assert_eq!(state.locks()[1].pair, "*");
}

#[test]
fn same_candle_global_lock_blocks_a_later_pair_entry() {
    let mut portfolio = config(2);
    portfolio.stoploss_ratio = -0.05;
    portfolio.protection_program = Some(ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::StoplossGuard {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 1,
            only_per_pair: false,
            only_per_side: false,
            required_profit: 0.0,
        }],
    });
    let mut first_entry = candle(0, 100.0, 100.0);
    first_entry.enter_long = Some(EntrySignal {
        tag: Some("first".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut later_entry = candle(300_000, 100.0, 100.0);
    later_entry.enter_long = Some(EntrySignal {
        tag: Some("later".to_owned()),
        leverage: None,
        liquidation_price: None,
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
        config: portfolio,
        pairs: vec![
            pair(
                "AAA/USDT",
                vec![
                    first_entry,
                    candle(300_000, 100.0, 90.0),
                    candle(600_000, 100.0, 100.0),
                ],
            ),
            pair(
                "BBB/USDT",
                vec![
                    candle(0, 100.0, 100.0),
                    later_entry,
                    candle(600_000, 100.0, 100.0),
                ],
            ),
        ],
    };

    let result = simulate(&input).expect("same-candle protection ordering");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].pair, "AAA/USDT");
    assert_eq!(result.trades[0].exit_reason, "stop_loss");
    assert_eq!(result.locks.len(), 2);
    assert_eq!(result.locks[0].pair, "AAA/USDT");
    assert_eq!(result.locks[1].pair, "*");
    assert_eq!(result.rejected_signals, 0);
}

#[test]
fn low_profit_pairs_and_max_drawdown_create_local_and_global_locks() {
    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![
            ProtectionHandler::LowProfitPairs {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                trade_limit: 1,
                only_per_side: false,
                required_profit: -0.02,
                required_profit_repr: None,
            },
            ProtectionHandler::MaxDrawdown {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                trade_limit: 2,
                maximum_allowed_drawdown: 0.2,
                maximum_allowed_drawdown_repr: None,
                calculation_mode: DrawdownMode::Ratios,
            },
        ],
    };
    let closed = vec![
        protection_trade(1, "AAA/USDT", 300_000, 0.1, "exit_signal", TradeSide::Long),
        protection_trade(2, "AAA/USDT", 600_000, -0.3, "exit_signal", TradeSide::Long),
    ];
    let mut state = ProtectionState::default();

    state
        .after_trade_close(&program, &closed[0], &closed[..1], 1_000.0)
        .expect("finite protection arithmetic");
    state
        .after_trade_close(&program, &closed[1], &closed, 1_000.0)
        .expect("finite protection arithmetic");

    assert_eq!(
        state
            .locks()
            .iter()
            .map(|lock| lock.pair.as_str())
            .collect::<Vec<_>>(),
        vec!["AAA/USDT", "*"]
    );
    assert!(state.locks()[0]
        .reason
        .starts_with("-0.19999999999999998 < -0.02"));
    assert!(state.locks()[1].reason.starts_with("0.3 passed 0.2"));
}

#[test]
fn protection_reasons_preserve_integer_and_float_threshold_display() {
    let closed = vec![protection_trade(
        1,
        "AAA/USDT",
        300_000,
        -0.25,
        "exit_signal",
        TradeSide::Long,
    )];
    for (low_repr, drawdown_repr, expected_low, expected_drawdown) in [
        (
            Some("1".to_owned()),
            Some("0".to_owned()),
            "-0.25 < 1 in 60 minutes, locking for 10 minutes.",
            "0.25 passed 0 in 60 minutes, locking for 10 minutes.",
        ),
        (
            None,
            None,
            "-0.25 < 1.0 in 60 minutes, locking for 10 minutes.",
            "0.25 passed 0.0 in 60 minutes, locking for 10 minutes.",
        ),
    ] {
        let program = ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![
                ProtectionHandler::LowProfitPairs {
                    timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                    trade_limit: 1,
                    only_per_side: false,
                    required_profit: 1.0,
                    required_profit_repr: low_repr,
                },
                ProtectionHandler::MaxDrawdown {
                    timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                    trade_limit: 1,
                    maximum_allowed_drawdown: 0.0,
                    maximum_allowed_drawdown_repr: drawdown_repr,
                    calculation_mode: DrawdownMode::Ratios,
                },
            ],
        };
        assert!(program.is_valid());

        let mut state = ProtectionState::default();
        state
            .after_trade_close(&program, &closed[0], &closed, 1_000.0)
            .expect("finite protection arithmetic");

        assert_eq!(state.locks()[0].reason, expected_low);
        assert_eq!(state.locks()[1].reason, expected_drawdown);
    }
}

#[test]
fn protection_threshold_display_rejects_noncanonical_or_mismatched_values() {
    for required_profit_repr in ["1.0", "01", "2"] {
        let program = ProtectionProgram {
            timeframe_ms: 300_000,
            handlers: vec![ProtectionHandler::LowProfitPairs {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                trade_limit: 1,
                only_per_side: false,
                required_profit: 1.0,
                required_profit_repr: Some(required_profit_repr.to_owned()),
            }],
        };

        assert!(!program.is_valid(), "accepted {required_profit_repr:?}");
    }

    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::MaxDrawdown {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 1,
            maximum_allowed_drawdown: 0.0,
            maximum_allowed_drawdown_repr: Some("-0".to_owned()),
            calculation_mode: DrawdownMode::Ratios,
        }],
    };
    assert!(!program.is_valid());

    let rounded_integer = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::LowProfitPairs {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 1,
            only_per_side: false,
            required_profit: 9_007_199_254_740_992.0,
            required_profit_repr: Some("9007199254740993".to_owned()),
        }],
    };
    assert!(!rounded_integer.is_valid());
}

#[test]
fn low_profit_accumulation_overflow_is_typed() {
    let program = ProtectionProgram {
        timeframe_ms: 300_000,
        handlers: vec![ProtectionHandler::LowProfitPairs {
            timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
            trade_limit: 2,
            only_per_side: false,
            required_profit: 0.0,
            required_profit_repr: None,
        }],
    };
    let mut first = protection_trade(1, "AAA/USDT", 300_000, 1.0, "exit_signal", TradeSide::Long);
    let mut second = protection_trade(2, "AAA/USDT", 600_000, 1.0, "exit_signal", TradeSide::Long);
    first.profit_ratio = f64::MAX;
    second.profit_ratio = f64::MAX;
    let closed = vec![first, second];

    assert_eq!(
        ProtectionState::default().after_trade_close(&program, &closed[1], &closed, 1_000.0),
        Err(SimError::ExactArithmetic {
            operation: "protection-low-profit-total"
        })
    );
}
