//! Leverage, liquidation, funding, and futures precision contracts.

use super::*;

#[test]
fn leveraged_short_uses_side_specific_orders_and_funding() {
    let mut entry = candle(1, 100.0, 99.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("short".to_owned()),
        leverage: Some(3.0),
        liquidation_price: Some(130.0),
    });
    let mut exit = candle(2, 90.0, 89.0);
    exit.high = 91.0;
    exit.funding_rate = Some(0.001);
    exit.funding_mark_price = Some(90.0);
    exit.exit_short = Some(ExitSignal {
        reason: "signal_exit".to_owned(),
    });
    let mut futures_config = config(1);
    futures_config.is_futures = true;
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: futures_config,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
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

    let result = simulate(&input).expect("valid short simulation");
    let trade = &result.trades[0];

    assert!(trade.is_short);
    assert!((trade.leverage - 3.0).abs() < f64::EPSILON);
    assert_eq!(trade.orders[0].side, OrderSide::Sell);
    assert_eq!(trade.orders[1].side, OrderSide::Buy);
    assert!(trade.funding_fees > 0.0);
    assert!(trade.profit_abs > 0.0);
}

#[test]
fn nfi_entry_leverage_preserves_rule_order_and_exchange_cap() {
    let program = NfiLeverageProgram {
        default: 4.0,
        ordered_tag_overrides: vec![
            NfiLeverageOverride {
                entry_tags: vec!["61".to_owned(), "62".to_owned()],
                leverage: 3.0,
            },
            NfiLeverageOverride {
                entry_tags: vec!["120".to_owned(), "121".to_owned()],
                leverage: 2.0,
            },
        ],
    };
    assert!((evaluate_nfi_leverage(&program, Some("61 62")) - 3.0).abs() < f64::EPSILON);
    assert!((evaluate_nfi_leverage(&program, Some("120")) - 2.0).abs() < f64::EPSILON);
    assert!((evaluate_nfi_leverage(&program, Some("61 120")) - 4.0).abs() < f64::EPSILON);

    let mut portfolio = config(1);
    portfolio.nfi_leverage_program = Some(program);
    portfolio
        .maximum_leverage_by_pair
        .insert("AAA/USDT:USDT".to_owned(), 2.5);
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("61".to_owned()),
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

    let result = simulate(&input).expect("valid tag-dependent leverage");

    assert!((result.trades[0].leverage - 2.5).abs() < f64::EPSILON);
}

#[test]
fn dynamic_leverage_cap_uses_proposed_stake_tier() {
    let pair_name = "AAA/USDT:USDT";
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.leverage = Some(8.0);
    portfolio
        .maximum_leverage_by_pair
        .insert(pair_name.to_owned(), 20.0);
    portfolio.liquidation_model = Some(isolated_model(
        pair_name,
        vec![
            leverage_tier(0.0, Some(500.0), 10.0, 0.005, 0.0),
            leverage_tier(500.0, Some(5_000.0), 5.0, 0.01, 5.0),
        ],
    ));
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
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
            pair: pair_name.to_owned(),
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

    let result = simulate(&input).expect("valid tier-capped leverage");

    assert_eq!(result.trades[0].leverage, 5.0);
}

#[test]
fn futures_partial_exit_keeps_freqtrade_initial_stake_float_boundary() {
    let (amount, stake, _, _) =
        entry_sizing(9_900.0, 20.881, 0.0005, 1.0, 5.0).expect("valid entry sizing");
    assert_eq!(amount, 2_370.0);
    assert_eq!(stake, 9_897.594_000_000_001);

    // This is the arithmetic order used by X7's derisk callback before
    // Freqtrade applies FtPrecise and exchange amount precision.
    let exit_rate = 18.792;
    let sell_amount = amount * 0.2 * exit_rate / 5.0;
    let requested_stake = sell_amount * 5.0 * (stake / amount) / exit_rate;
    let raw_amount =
        precise_product_quotient(requested_stake, amount, stake).expect("valid partial exit");
    assert_eq!(floor_step(raw_amount, 1.0), 474.0);
}

#[test]
fn futures_profit_rounding_matches_python_format_boundary() {
    let open_value = precise_product(&[26_791.1, 0.7768, 1.0005]).expect("valid open value");
    let close_value = precise_product(&[26_791.1, 0.8043, 0.9995]).expect("valid close value");
    let profit = close_value - open_value;
    let amount = exact_rational(26_791.1).expect("valid amount");
    let stake = &amount * exact_rational(0.7768).expect("valid price");
    let average = ft_precise_division(&stake, &amount)
        .and_then(|value| value.to_f64())
        .expect("valid average");

    assert_eq!(open_value, 20_821.732_143_24);
    assert_eq!(close_value, 21_537.307_689_135);
    assert_eq!(profit, 715.575_545_895_000_7);
    assert_eq!(average, 0.7768);
    assert_eq!(round_eight(profit), 715.575_545_9);
}

#[test]
fn variable_leverage_trade_keeps_eight_decimal_profit_boundary() {
    let pair_name = "ALGO/USDT:USDT";
    let mut portfolio = config(1);
    portfolio.starting_balance = 20_000.0;
    portfolio.stake_amount = 10_405.663_24;
    portfolio.unlimited_stake = false;
    portfolio.is_futures = true;
    portfolio.leverage = Some(2.0);
    portfolio.amount_step = 0.1;
    portfolio.price_step = 0.0001;
    portfolio.fee_rate = 0.0005;
    portfolio.fee_open_rate = Some(0.0005);
    portfolio.fee_close_rate = Some(0.0005);

    let mut entry = candle(1, 0.7768, 0.77);
    entry.enter_long = Some(EntrySignal {
        tag: Some("145 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut exit = candle(2, 0.8043, 0.80);
    exit.exit_long = Some(ExitSignal {
        reason: "exit_long_tc_d_3_42 ( 145 )".to_owned(),
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
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid variable-leverage trade");
    let trade = &result.trades[0];

    assert_eq!(trade.amount, 26_791.1);
    assert_eq!(trade.profit_abs, 715.575_545_9);
}

#[test]
fn computed_isolated_liquidation_matches_binance_long_and_short_formula() {
    let pair_name = "AAA/USDT:USDT";
    for side in [TradeSide::Long, TradeSide::Short] {
        let mut portfolio = config(1);
        portfolio.is_futures = true;
        portfolio.leverage = Some(3.0);
        portfolio.stoploss_ratio = -0.99;
        portfolio.liquidation_model = Some(isolated_model(
            pair_name,
            vec![leverage_tier(0.0, None, 20.0, 0.005, 0.0)],
        ));
        let mut entry = candle(1, 100.0, 100.0);
        let signal = EntrySignal {
            tag: None,
            leverage: None,
            liquidation_price: None,
        };
        if side == TradeSide::Short {
            entry.enter_short = Some(signal);
        } else {
            entry.enter_long = Some(signal);
        }
        // Keep this a liquidation-only candle. The stop-loss collision
        // ordering is covered separately because Freqtrade returns the
        // stop-loss candidate first when both boundaries are crossed.
        let mut liquidated = candle(2, 100.0, 100.0);
        if side == TradeSide::Short {
            liquidated.high = 132.0;
        } else {
            liquidated.low = 68.0;
        }
        let input = SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: portfolio,
            pairs: vec![PairSeries {
                pair: pair_name.to_owned(),
                execution_start_index: 0,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: BTreeMap::new(),
                candles: vec![entry, liquidated].into(),
            }],
        };

        let result = simulate(&input).expect("valid computed liquidation");
        let trade = &result.trades[0];
        let expected = buffered_liquidation_price(
            side,
            trade.stake_amount,
            trade.amount,
            trade.open_rate,
            0.005,
            0.0,
            0.05,
        );

        assert_eq!(trade.exit_reason, "liquidation");
        // The calculated liquidation boundary remains exact on the trade,
        // while the synthetic liquidation order is filled at the price
        // precision frozen when the position opened.
        assert_eq!(trade.close_rate, round_step(expected, 0.01));
        assert_eq!(trade.liquidation_price, Some(expected));
    }
}

#[test]
fn isolated_liquidation_recalculates_after_position_adjustment() {
    let pair_name = "AAA/USDT:USDT";
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.leverage = Some(3.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.adjustment_rule = Some(AdjustmentRule {
        profit_below: -0.1,
        stake_ratio: 1.0,
        max_adjustments: 1,
        tag: "rebuy".to_owned(),
    });
    portfolio.liquidation_model = Some(isolated_model(
        pair_name,
        vec![leverage_tier(0.0, None, 20.0, 0.005, 0.0)],
    ));
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let adjustment = candle(2, 80.0, 75.0);
    let mut exit = candle(3, 100.0, 100.0);
    exit.exit_long = Some(ExitSignal {
        reason: "done".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: pair_name.to_owned(),
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

    let result = simulate(&input).expect("valid adjusted liquidation");
    let trade = &result.trades[0];
    let expected = buffered_liquidation_price(
        TradeSide::Long,
        trade.stake_amount,
        trade.amount,
        trade.open_rate,
        0.005,
        0.0,
        0.05,
    );
    let first_order = &trade.orders[0];
    let initial = buffered_liquidation_price(
        TradeSide::Long,
        first_order.cost / trade.leverage,
        first_order.amount,
        first_order.price,
        0.005,
        0.0,
        0.05,
    );

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.liquidation_price, Some(expected));
    assert!((expected - initial).abs() > 1e-6);
}

#[test]
fn partial_exit_liquidation_refresh_precedes_trade_replay() {
    let pair_name = "APE/USDT:USDT";
    let mut portfolio = config(1);
    portfolio.starting_balance = 20_000.0;
    portfolio.stake_amount = 11_588.348_4;
    portfolio.unlimited_stake = false;
    portfolio.is_futures = true;
    portfolio.leverage = Some(5.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;
    portfolio.price_step = 0.001;
    portfolio.liquidation_model = Some(isolated_model(
        pair_name,
        vec![leverage_tier(0.0, None, 5.0, 0.02, 25.0)],
    ));

    let mut entry = candle(1, 21.799, 21.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("141 142 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut first_derisk = candle(2, 20.044, 19.5);
    first_derisk.adjustment = Some(AdjustmentSignal {
        stake_amount: -2_317.669_68,
        tag: "derisk_level_1".to_owned(),
    });
    let mut second_derisk = candle(3, 18.89, 18.5);
    second_derisk.adjustment = Some(AdjustmentSignal {
        stake_amount: -3_476.504_52,
        tag: "derisk_level_2".to_owned(),
    });
    let liquidation = candle(4, 18.0, 17.95);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: pair_name.to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.001),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, first_derisk, second_derisk, liquidation].into(),
        }],
    };

    let result = simulate(&input).expect("valid partial-exit liquidation");
    let trade = &result.trades[0];
    let expected = buffered_liquidation_price(
        TradeSide::Long,
        9_273.294_6,
        2_127.0,
        21.799,
        0.02,
        25.0,
        0.05,
    );

    assert_eq!(trade.exit_reason, "liquidation");
    assert_eq!(trade.orders[1].amount, 531.0);
    assert_eq!(trade.orders[2].amount, 797.0);
    assert_eq!(trade.close_rate, round_step(expected, 0.001));
    assert_eq!(trade.close_rate, 17.984);
}

#[test]
fn ape_short_funding_and_profit_match_freqtrade_2026_5_1() {
    let mut portfolio = config(1);
    portfolio.starting_balance = 10_000.0;
    portfolio.stake_amount = 3_236.574;
    portfolio.fee_rate = 0.0005;
    portfolio.fee_open_rate = Some(0.0005);
    portfolio.fee_close_rate = Some(0.0005);
    portfolio.leverage = Some(3.0);
    portfolio.stoploss_ratio = -0.99;
    portfolio.amount_step = 1.0;
    portfolio.price_step = 0.001;
    portfolio.is_futures = true;

    let mut entry = candle(1_654_801_500_000, 5.742, 5.74);
    entry.high = 5.758;
    entry.enter_short = Some(EntrySignal {
        tag: Some("562 ".to_owned()),
        leverage: Some(3.0),
        liquidation_price: None,
    });
    let mut funding = candle(1_654_819_200_000, 5.721, 5.525_74);
    funding.high = 5.738_839;
    funding.funding_rate = Some(0.000_020_67);
    funding.funding_mark_price = Some(5.721);
    let mut exit = candle(1_654_820_400_000, 5.568, 5.5);
    exit.high = 5.58;
    exit.exit_short = Some(ExitSignal {
        reason: "exit_short_rebuy_d_3_100 ( 562 )".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "APE/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: Some(1.0),
            price_step: Some(0.001),
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: Some(5.0),
            feature_columns: BTreeMap::new(),
            candles: vec![entry, funding, exit].into(),
        }],
    };

    let result = simulate(&input).expect("valid APE short simulation");
    let trade = &result.trades[0];

    assert_eq!(trade.amount, 1_691.0);
    assert_eq!(trade.funding_fees, 0.199_965_941_37);
    assert!((trade.profit_abs - 284.871_360_94).abs() < 1e-10);
    assert!((trade.profit_ratio - 0.088_060_358_846_711_66).abs() < 1e-14);
}

#[test]
fn rejected_stop_loss_collision_does_not_fall_through_to_liquidation() {
    let mut portfolio = config(1);
    portfolio.is_futures = true;
    portfolio.exit_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {"op": "literal", "value": false}
            }],
            "functions": {}
        }))
        .expect("valid rejecting confirmation program"),
    );
    let mut entry = candle(1, 100.0, 99.0);
    entry.enter_short = Some(EntrySignal {
        tag: None,
        leverage: Some(5.0),
        liquidation_price: Some(105.0),
    });
    let mut liquidated = candle(2, 104.0, 103.0);
    liquidated.high = 110.0;
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: portfolio,
        pairs: vec![PairSeries {
            pair: "AAA/USDT:USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![entry, liquidated].into(),
        }],
    };

    let result = simulate(&input).expect("valid liquidation simulation");

    assert_eq!(result.trades[0].exit_reason, "force_exit");
    assert!((result.trades[0].close_rate - 104.0).abs() < f64::EPSILON);
}

#[test]
fn trailing_stop_uses_candle_open_after_a_gap_beyond_the_retained_stop() {
    let portfolio = config(1);
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    });
    let mut trigger = candle(2, 99.0, 98.0);
    trigger.high = 100.0;
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
        candles: vec![entry, trigger].into(),
    };
    let entry_candle = pair.candles.get(0).expect("entry candle");
    let signal = entry_candle.enter_long.as_ref().expect("long signal");
    let mut trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal,
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
    trade.initial_stop_loss = 90.0;
    trade.stop_loss = 105.0;
    let trigger_candle = pair.candles.get(1).expect("trigger candle");

    let exit = exit_decision(
        &trade,
        &pair,
        1,
        &trigger_candle,
        &portfolio,
        &mut BTreeMap::new(),
    )
    .expect("valid exit evaluation")
    .expect("stop reached");

    assert_eq!(exit.reason, "trailing_stop_loss");
    assert_eq!(exit.rate, 99.0);
}
