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

    state.after_trade_close(&program, &closed[0], &closed, 1_000.0);

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

    state.after_trade_close(&program, &closed[0], &closed[..1], 1_000.0);
    state.after_trade_close(&program, &closed[1], &closed, 1_000.0);

    assert_eq!(state.locks().len(), 1);
    assert_eq!(state.locks()[0].pair, "*");
    assert_eq!(state.locks()[0].side, "long");
    assert!(state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Long));
    assert!(!state.is_pair_locked("CCC/USDT", 700_000, TradeSide::Short));
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
            },
            ProtectionHandler::MaxDrawdown {
                timing: protection_timing(3_600_000, 600_000, "60 minutes", "for 10 minutes"),
                trade_limit: 2,
                maximum_allowed_drawdown: 0.2,
                calculation_mode: DrawdownMode::Ratios,
            },
        ],
    };
    let closed = vec![
        protection_trade(1, "AAA/USDT", 300_000, 0.1, "exit_signal", TradeSide::Long),
        protection_trade(2, "AAA/USDT", 600_000, -0.3, "exit_signal", TradeSide::Long),
    ];
    let mut state = ProtectionState::default();

    state.after_trade_close(&program, &closed[0], &closed[..1], 1_000.0);
    state.after_trade_close(&program, &closed[1], &closed, 1_000.0);

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
