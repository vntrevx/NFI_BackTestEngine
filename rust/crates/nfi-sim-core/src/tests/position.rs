//! Generic position adjustment and exit-ordering contracts.

use super::*;

#[test]
fn entry_adjustment_stop_and_fees_are_accounted_in_order() {
    let mut entry = candle(1, 100.0, 99.5);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut adjustment = candle(2, 99.5, 99.2);
    adjustment.adjustment = Some(AdjustmentSignal {
        stake_amount: 50.0,
        tag: "rebuy".to_owned(),
    });
    let stop = candle(3, 99.0, 98.0);
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
            candles: vec![entry, adjustment, stop].into(),
        }],
    };

    let result = simulate(&input).expect("valid simulation");
    let trade = &result.trades[0];

    assert_eq!(trade.exit_reason, "stop_loss");
    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("rebuy"));
    assert!(trade.profit_abs < 0.0);
    assert!((trade.close_rate - 99.0).abs() < f64::EPSILON);
}

#[test]
fn same_candle_adjustment_is_applied_before_stop_exit() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut adjustment_and_stop = candle(2, 99.5, 98.0);
    adjustment_and_stop.adjustment = Some(AdjustmentSignal {
        stake_amount: 50.0,
        tag: "grind".to_owned(),
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
            candles: vec![entry, adjustment_and_stop].into(),
        }],
    };

    let result = simulate(&input).expect("adjustment precedes the same-candle stop check");
    let trade = &result.trades[0];

    assert_eq!(trade.exit_reason, "stop_loss");
    assert_eq!(trade.close_timestamp_ms, 2);
    assert_eq!(trade.orders.len(), 3);
    assert!(trade.orders[1].is_entry);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("grind"));
    assert!(!trade.orders[2].is_entry);
}

#[test]
fn futures_entry_adjustment_replays_funding_at_the_fill_timestamp() {
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.leverage = Some(2.0);
    portfolio.fee_rate = 0.0;
    portfolio.fee_open_rate = Some(0.0);
    portfolio.fee_close_rate = Some(0.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;

    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut adjustment = candle(2, 100.0, 100.0);
    adjustment.funding_rate = Some(0.001);
    adjustment.funding_mark_price = Some(100.0);
    adjustment.adjustment = Some(AdjustmentSignal {
        stake_amount: 100.0,
        tag: "rebuy".to_owned(),
    });
    let mut exit = candle(3, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.01),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, adjustment, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid same-timestamp funding adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[0].amount, 2.0);
    assert_eq!(trade.orders[1].amount, 2.0);
    assert_eq!(trade.orders[1].funding_fee, -0.2);
    // Pinned Freqtrade first moves the pre-fill funding to the adjustment
    // order, then force-recalculates the inclusive funding range with the
    // new position amount: -(0.001 * 100 * 2) - (0.001 * 100 * 4).
    assert_eq!(trade.orders[2].funding_fee, -0.4);
    assert_eq!(trade.funding_fees, -0.600_000_000_000_000_1);
    assert_eq!(trade.profit_abs, -0.6);
}

#[test]
fn futures_initial_entry_seeds_funding_at_the_fill_timestamp() {
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.leverage = Some(2.0);
    portfolio.fee_rate = 0.0;
    portfolio.fee_open_rate = Some(0.0);
    portfolio.fee_close_rate = Some(0.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;

    let mut entry = candle(1, 100.0, 100.0);
    entry.funding_rate = Some(0.001);
    entry.funding_mark_price = Some(100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.01),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid same-timestamp entry funding");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[0].amount, 2.0);
    assert_eq!(trade.orders[1].funding_fee, -0.2);
    assert_eq!(trade.funding_fees, -0.2);
    assert_eq!(trade.profit_abs, -0.2);
}

#[test]
fn futures_partial_exit_rebases_inclusive_funding_on_the_next_tick() {
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.leverage = Some(2.0);
    portfolio.stake_amount = 200.0;
    portfolio.fee_rate = 0.0;
    portfolio.fee_open_rate = Some(0.0);
    portfolio.fee_close_rate = Some(0.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;

    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut partial_exit = candle(2, 100.0, 100.0);
    partial_exit.funding_rate = Some(0.001);
    partial_exit.funding_mark_price = Some(100.0);
    partial_exit.adjustment = Some(AdjustmentSignal {
        stake_amount: -100.0,
        tag: "derisk".to_owned(),
    });
    let mut next_funding = candle(3, 100.0, 100.0);
    next_funding.funding_rate = Some(0.002);
    next_funding.funding_mark_price = Some(100.0);
    let mut exit = candle(4, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.01),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, partial_exit, next_funding, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid partial-exit funding rebase");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[0].amount, 4.0);
    assert_eq!(trade.orders[1].amount, 2.0);
    assert_eq!(trade.orders[1].funding_fee, -0.4);
    // The next segment is recalculated with the reduced amount and remains
    // inclusive of the partial-fill row: -0.2 + -0.4.
    assert_eq!(trade.orders[2].funding_fee, -0.600_000_000_000_000_1);
    assert_eq!(trade.funding_fees, -1.0);
    assert_eq!(trade.profit_abs, -1.0);
}

#[test]
fn futures_partial_exit_rebases_on_a_refresh_without_a_new_funding_event() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.funding_fee_interval_ms = Some(HOUR);
    portfolio.leverage = Some(2.0);
    portfolio.stake_amount = 200.0;
    portfolio.fee_rate = 0.0;
    portfolio.fee_open_rate = Some(0.0);
    portfolio.fee_close_rate = Some(0.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;

    let mut entry = candle(7 * HOUR, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut partial_exit = candle(8 * HOUR, 100.0, 100.0);
    partial_exit.funding_rate = Some(0.001);
    partial_exit.funding_mark_price = Some(100.0);
    partial_exit.adjustment = Some(AdjustmentSignal {
        stake_amount: -100.0,
        tag: "derisk".to_owned(),
    });
    // Freqtrade refreshes its inclusive segment at this scheduled hour,
    // even though the sparse funding dataframe has no new event.
    let refresh = candle(9 * HOUR, 100.0, 100.0);
    let mut exit = candle(10 * HOUR, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.01),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, partial_exit, refresh, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid scheduled funding rebase");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[1].funding_fee, -0.4);
    assert_eq!(trade.orders[2].funding_fee, -0.2);
    assert_eq!(trade.funding_fees, -0.600_000_000_000_000_1);
    assert_eq!(trade.profit_abs, -0.600_000_000_000_000_1);
}

#[test]
fn futures_profit_uses_python_eight_decimal_ties_to_even() {
    let pair_name = "BAND/USDT:USDT";
    let mut portfolio = config(1);
    portfolio.starting_balance = 1_000.0;
    portfolio.stake_amount = 216.343_926_67;
    portfolio.fee_rate = 0.0005;
    portfolio.fee_open_rate = Some(0.0005);
    portfolio.fee_close_rate = Some(0.0005);
    portfolio.leverage = Some(3.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 0.1;
    portfolio.price_step = 0.0001;
    portfolio.is_futures = true;

    let mut entry = candle(1, 1.8094, 1.8033);
    entry.enter_long = Some(EntrySignal {
        tag: Some("104 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 1.8951, 1.89);
    exit.exit_long = Some(ExitSignal {
        reason: "exit_long_rapid_d_4_71 ( 104 )".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: pair_name.to_owned(),
            execution_start_index: 0,
            amount_step: Some(0.1),
            price_step: Some(0.0001),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: Some(5.0),
            feature_columns: BTreeMap::new(),
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid decimal tie boundary trade");
    let trade = &result.trades[0];

    assert_eq!(trade.amount, 358.7);
    assert_eq!(trade.profit_abs, 30.076_187_92);
}

#[test]
fn negative_adjustment_realizes_a_partial_exit() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut derisk = candle(2, 110.0, 109.0);
    derisk.adjustment = Some(AdjustmentSignal {
        stake_amount: -40.0,
        tag: "derisk".to_owned(),
    });
    let mut exit = candle(3, 120.0, 119.0);
    exit.exit_long = Some(ExitSignal {
        reason: "signal_exit".to_owned(),
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
            candles: vec![entry, derisk, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid partial exit simulation");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert!(!trade.orders[1].is_entry);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk"));
    assert!(trade.stake_amount < trade.max_stake_amount);
    assert!(trade.profit_abs > 0.0);
}

#[test]
fn explicit_exit_is_filled_at_candle_open() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 105.0, 104.0);
    exit.exit_long = Some(ExitSignal {
        reason: "custom_exit".to_owned(),
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
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid simulation");

    assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
    assert_eq!(result.trades[0].exit_reason, "custom_exit");
    assert!(result.final_balance > result.starting_balance);
}

#[test]
fn overlapping_trades_are_exported_in_freqtrade_closure_order() {
    let mut first_entry = candle(1, 100.0, 100.0);
    first_entry.enter_long = Some(EntrySignal {
        tag: Some("first".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut first_exit = candle(4, 103.0, 103.0);
    first_exit.exit_long = Some(ExitSignal {
        reason: "late_exit".to_owned(),
    });

    let mut second_entry = candle(2, 100.0, 100.0);
    second_entry.enter_long = Some(EntrySignal {
        tag: Some("second".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut second_exit = candle(3, 102.0, 102.0);
    second_exit.exit_long = Some(ExitSignal {
        reason: "early_exit".to_owned(),
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
        config: config(2),
        pairs: vec![
            pair(
                "AAA/USDT",
                vec![
                    first_entry,
                    candle(2, 101.0, 101.0),
                    candle(3, 102.0, 102.0),
                    first_exit,
                ],
            ),
            pair(
                "BBB/USDT",
                vec![
                    candle(1, 100.0, 100.0),
                    second_entry,
                    second_exit,
                    candle(4, 103.0, 103.0),
                ],
            ),
        ],
    };

    let result = simulate(&input).expect("valid overlapping trade simulation");

    assert_eq!(result.trades.len(), 2);
    assert_eq!(result.trades[0].pair, "BBB/USDT");
    assert_eq!(result.trades[0].sequence, 0);
    assert_eq!(result.trades[1].pair, "AAA/USDT");
    assert_eq!(result.trades[1].sequence, 1);
}

#[test]
fn strategy_exit_precedes_stoploss_on_the_same_freqtrade_candle() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 105.0, 90.0);
    exit.exit_long = Some(ExitSignal {
        reason: "custom_exit".to_owned(),
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
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid collision simulation");

    assert_eq!(result.trades[0].exit_reason, "custom_exit");
    assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
}

#[test]
fn compiled_custom_exit_bundle_runs_inside_the_native_trade_loop() {
    let mut config = config(1);
    config.custom_exit_program = Some(
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "entry": "custom_exit",
            "programs": {
                "custom_exit": {
                    "schema_version": "1.1.0",
                    "opcode": "scalar-decision-program-v1",
                    "parameters": [
                        "pair",
                        "trade",
                        "current_time",
                        "current_rate",
                        "current_profit"
                    ],
                    "expressions": [
                        ["variable", "current_profit"],
                        ["literal", 0.01],
                        ["compare", 0, [["greater", 1]]],
                        ["literal", "native_custom_exit"],
                        ["literal", null]
                    ],
                    "statements": [
                        ["if", 2, [["return", 3]], []],
                        ["return", 4]
                    ]
                }
            }
        }))
        .expect("valid custom exit bundle"),
    );
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("test".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let exit = candle(2, 105.0, 104.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config,
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
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid compiled custom exit");

    assert_eq!(result.trades[0].exit_reason, "native_custom_exit");
    assert!((result.trades[0].close_rate - 105.0).abs() < f64::EPSILON);
}

#[test]
fn compiled_position_adjustment_bundle_adds_a_tagged_entry() {
    let mut portfolio = config(1);
    portfolio.stoploss_ratio = -0.99;
    portfolio.adjust_trade_position_program = Some(
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "entry": "adjust_trade_position",
            "programs": {
                "adjust_trade_position": {
                    "schema_version": "1.1.0",
                    "opcode": "scalar-decision-program-v1",
                    "parameters": [
                        "trade",
                        "current_time",
                        "current_rate",
                        "current_profit",
                        "min_stake",
                        "max_stake",
                        "current_entry_rate",
                        "current_exit_rate",
                        "current_entry_profit",
                        "current_exit_profit"
                    ],
                    "expressions": [
                        ["variable", "current_profit"],
                        ["literal", -0.01],
                        ["compare", 0, [["less", 1]]],
                        ["literal", 50.0],
                        ["literal", "compiled_rebuy"],
                        ["tuple", [3, 4]],
                        ["literal", null]
                    ],
                    "statements": [
                        ["if", 2, [["return", 5]], []],
                        ["return", 6]
                    ]
                }
            }
        }))
        .expect("valid adjustment bundle"),
    );
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("test".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let adjustment = candle(2, 90.0, 90.0);
    let mut exit = candle(3, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
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
            candles: vec![entry, adjustment, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid compiled adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("compiled_rebuy"));
    assert!(trade.orders[1].is_entry);
}

#[test]
fn generic_state_machine_emits_dynamic_grind_adjustment_and_exit() {
    let portfolio = config(1);
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: vec![candle(1, 100.0, 99.0), candle(2, 90.0, 80.0)].into(),
    };
    let signal = EntrySignal {
        tag: Some("63".to_owned()),
        leverage: None,
        liquidation_price: None,
    };
    let entry_candle = pair.candles.get(0).expect("entry candle");
    let mut trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1_000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &portfolio,
    )
    .expect("valid entry")
    .expect("sized entry");
    let program: StateMachineProgram = serde_json::from_value(serde_json::json!({
        "schema_version": "state-machine-program-v1",
        "entrypoints": {
            "adjust_trade_position": {
                "max_steps": 8,
                "instructions": [{
                    "opcode": "action",
                    "id": "i1",
                    "kind": "add_entry",
                    "stake": {"kind": "literal", "value": 25.0},
                    "tag": {"kind": "literal", "value": "grind_12_entry"}
                }]
            },
            "custom_exit": {
                "max_steps": 8,
                "instructions": [{
                    "opcode": "action",
                    "id": "i2",
                    "kind": "exit",
                    "stake": null,
                    "tag": {"kind": "literal", "value": "signal_63_exit"}
                }]
            }
        },
        "required_reads": [],
        "required_columns": [],
        "required_state_keys": [],
        "opcodes": ["action"],
        "source_map": {
            "i1": {"path": "strategy.py", "line": 1, "column": 0, "end_line": 1, "end_column": 1},
            "i2": {"path": "strategy.py", "line": 2, "column": 0, "end_line": 2, "end_column": 1}
        }
    }))
    .expect("valid generic state machine");
    let callback_candle = pair.candles.get(1).expect("callback candle");

    let adjustment = evaluate_state_machine_adjustment(
        &program,
        &mut trade,
        &pair,
        1,
        &callback_candle,
        &portfolio,
        500.0,
    )
    .expect("valid state-machine adjustment")
    .expect("adjustment exists");
    let exit =
        evaluate_state_machine_exit(&program, &mut trade, &pair, 1, &callback_candle, &portfolio)
            .expect("valid state-machine exit");

    assert_eq!(adjustment.stake_amount, 25.0);
    assert_eq!(adjustment.tag, "grind_12_entry");
    assert_eq!(exit.as_deref(), Some("signal_63_exit"));
}

#[test]
fn generic_state_machine_adjusts_on_the_entry_candle() {
    let mut portfolio = config(1);
    portfolio.stoploss_ratio = -0.99;
    portfolio.state_machine_program = Some(
        serde_json::from_value(serde_json::json!({
            "schema_version": "state-machine-program-v1",
            "entrypoints": {
                "adjust_trade_position": {
                    "max_steps": 8,
                    "instructions": [{
                        "opcode": "action",
                        "id": "i1",
                        "kind": "add_entry",
                        "stake": {"kind": "literal", "value": 25.0},
                        "tag": {"kind": "literal", "value": "same_candle_grind"}
                    }]
                }
            },
            "required_reads": [],
            "required_columns": [],
            "required_state_keys": [],
            "opcodes": ["action"],
            "source_map": {
                "i1": {
                    "path": "strategy.py",
                    "line": 1,
                    "column": 0,
                    "end_line": 1,
                    "end_column": 1
                }
            }
        }))
        .expect("valid generic state machine"),
    );
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
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
            candles: vec![candle(1, 100.0, 100.0), entry, candle(3, 100.0, 100.0)].into(),
        }],
    };

    let result = simulate(&input).expect("valid same-candle generic adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[0].filled_timestamp_ms, 2);
    assert_eq!(trade.orders[1].filled_timestamp_ms, 2);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("same_candle_grind"));
}

#[test]
fn position_adjustment_receives_tradable_balance_limited_max_stake() {
    let mut portfolio = config(1);
    portfolio.starting_balance = 1_000.0;
    portfolio.stake_amount = 100.0;
    portfolio.tradable_balance_ratio = 0.99;
    portfolio.stoploss_ratio = -0.99;
    portfolio.adjust_trade_position_program = Some(
        serde_json::from_value(serde_json::json!({
            "schema_version": "1.0.0",
            "entry": "adjust_trade_position",
            "programs": {
                "adjust_trade_position": {
                    "schema_version": "1.1.0",
                    "opcode": "scalar-decision-program-v1",
                    "parameters": [
                        "trade",
                        "current_time",
                        "current_rate",
                        "current_profit",
                        "min_stake",
                        "max_stake",
                        "current_entry_rate",
                        "current_exit_rate",
                        "current_entry_profit",
                        "current_exit_profit"
                    ],
                    "expressions": [
                        ["variable", "max_stake"],
                        ["literal", 895.0],
                        ["compare", 0, [["greater", 1]]],
                        ["literal", 50.0],
                        ["literal", "must_not_run"],
                        ["tuple", [3, 4]],
                        ["literal", null]
                    ],
                    "statements": [
                        ["if", 2, [["return", 5]], []],
                        ["return", 6]
                    ]
                }
            }
        }))
        .expect("valid adjustment bundle"),
    );
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("test".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let adjustment = candle(2, 99.0, 99.0);
    let mut exit = candle(3, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
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
            candles: vec![entry, adjustment, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid tradable-balance adjustment");

    assert_eq!(result.trades[0].orders.len(), 2);
    assert!(result.trades[0]
        .orders
        .iter()
        .all(|order| order.tag.as_deref() != Some("must_not_run")));
}
