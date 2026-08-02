//! NFI rebuy, grind, and regular-adjustment contracts.

use super::*;

#[test]
fn nfi_rebuy_adds_the_first_source_ladder_entry() {
    let mut entry = candle(1, 100.0, 100.0);
    // OHLC columns are read from the candle, not duplicated feature
    // storage. This is the analyzed close visible to candle 2.
    entry.close = 90.0;
    entry.enter_long = Some(EntrySignal {
        tag: Some("61".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 100.0, 100.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    // Callback features are shifted by one row: the candle-2 callback
    // reads index 0, exactly as Freqtrade reads its last analyzed candle.
    let features = BTreeMap::from([
        (
            "protections_long_global".to_owned(),
            vec![
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(true),
            ],
        ),
        (
            "RSI_3".to_owned(),
            vec![
                serde_json::json!(20.0),
                serde_json::json!(20.0),
                serde_json::json!(20.0),
            ],
        ),
        (
            "RSI_3_15m".to_owned(),
            vec![
                serde_json::json!(20.0),
                serde_json::json!(20.0),
                serde_json::json!(20.0),
            ],
        ),
        (
            "AROONU_14".to_owned(),
            vec![
                serde_json::json!(10.0),
                serde_json::json!(10.0),
                serde_json::json!(10.0),
            ],
        ),
        (
            "AROONU_14_15m".to_owned(),
            vec![
                serde_json::json!(10.0),
                serde_json::json!(10.0),
                serde_json::json!(10.0),
            ],
        ),
        (
            "close".to_owned(),
            vec![
                serde_json::json!(90.0),
                serde_json::json!(100.0),
                serde_json::json!(100.0),
            ],
        ),
        (
            "EMA_26".to_owned(),
            vec![
                serde_json::json!(100.0),
                serde_json::json!(100.0),
                serde_json::json!(100.0),
            ],
        ),
    ]);
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0), force_exit], features);
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input).expect("reviewed rebuy ladder entry");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("r"));
    assert!(trade.orders[1].is_entry);
    assert_eq!(trade.orders[1].price, 90.0);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn nfi_rebuy_derisk_leaves_the_exchange_minimum_reserve() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("65".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 40.0, 40.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    // A false protection gate disables the entry branch. The de-risk
    // branch is intentionally independent of the indicator predicate.
    let features = BTreeMap::from([
        (
            "protections_long_global".to_owned(),
            vec![
                serde_json::json!(false),
                serde_json::json!(false),
                serde_json::json!(false),
            ],
        ),
        (
            "RSI_3".to_owned(),
            vec![
                serde_json::json!(20.0),
                serde_json::json!(20.0),
                serde_json::json!(20.0),
            ],
        ),
        (
            "RSI_3_15m".to_owned(),
            vec![
                serde_json::json!(20.0),
                serde_json::json!(20.0),
                serde_json::json!(20.0),
            ],
        ),
        (
            "AROONU_14".to_owned(),
            vec![
                serde_json::json!(10.0),
                serde_json::json!(10.0),
                serde_json::json!(10.0),
            ],
        ),
        (
            "AROONU_14_15m".to_owned(),
            vec![
                serde_json::json!(10.0),
                serde_json::json!(10.0),
                serde_json::json!(10.0),
            ],
        ),
        (
            "close".to_owned(),
            vec![
                serde_json::json!(40.0),
                serde_json::json!(40.0),
                serde_json::json!(40.0),
            ],
        ),
        (
            "EMA_26".to_owned(),
            vec![
                serde_json::json!(100.0),
                serde_json::json!(100.0),
                serde_json::json!(100.0),
            ],
        ),
    ]);
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let mut pair = nfi_pair(vec![entry, candle(2, 40.0, 40.0), force_exit], features);
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input).expect("reviewed rebuy de-risk");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk_level_3"));
    assert!(!trade.orders[1].is_entry);
    assert!(trade.stake_amount < trade.max_stake_amount);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn nfi_rebuy_transfer_restores_the_source_slice_before_grind_sizing() {
    const STEP: i64 = 10 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("64".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let derisk = candle(STEP, 40.0, 40.0);
    let grind = candle(2 * STEP, 39.0, 39.0);
    let mut force_exit = candle(3 * STEP, 39.0, 39.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let numeric_values = |value| {
        vec![
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
        ]
    };
    let features = BTreeMap::from([
        (
            "protections_long_global".to_owned(),
            vec![
                serde_json::json!(false),
                serde_json::json!(false),
                serde_json::json!(false),
                serde_json::json!(false),
            ],
        ),
        ("RSI_3".to_owned(), numeric_values(20.0)),
        ("RSI_3_15m".to_owned(), numeric_values(20.0)),
        ("AROONU_14".to_owned(), numeric_values(10.0)),
        ("AROONU_14_15m".to_owned(), numeric_values(10.0)),
        ("RSI_14".to_owned(), numeric_values(20.0)),
        ("close".to_owned(), numeric_values(39.0)),
        ("EMA_20".to_owned(), numeric_values(100.0)),
        ("EMA_26".to_owned(), numeric_values(100.0)),
    ]);
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    let position_adjustment = manager
        .position_adjustment
        .as_mut()
        .expect("test manager has position adjustment");
    position_adjustment.enabled = true;
    position_adjustment.constants.grinds[3].enabled = true;
    position_adjustment.constants.grinds[3].stakes_futures = vec![0.05];
    position_adjustment.constants.grinds[3].stakes_spot = vec![0.05];
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, derisk, grind, force_exit], features);
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed rebuy-to-grind transfer");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 4);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("derisk_level_3"));
    assert_eq!(trade.orders[2].tag.as_deref(), Some("grind_4_entry"));
    // X7 divides the initial rebuy entry by 0.25, then applies grind 4's
    // 0.05 first stake. Precision can floor the filled amount, so assert
    // the exact source-sized region instead of a pre-fill decimal.
    assert!(
        trade.orders[2].cost > trade.orders[0].cost * 0.19
            && trade.orders[2].cost < trade.orders[0].cost * 0.21,
        "initial cost {}, transferred grind cost {}",
        trade.orders[0].cost,
        trade.orders[2].cost
    );
}

#[test]
fn nfi_short_rebuy_transfer_accepts_the_source_rebuy_tag() {
    const STEP: i64 = 10 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("562".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let first_rebuy = candle(STEP, 114.0, 114.0);
    let second_rebuy = candle(2 * STEP, 128.0, 128.0);
    let mut derisk = candle(3 * STEP, 145.0, 145.0);
    derisk.high = 145.0;
    let mut grind = candle(4 * STEP, 140.0, 140.0);
    grind.high = 140.0;
    let mut force_exit = candle(5 * STEP, 140.0, 140.0);
    force_exit.high = 140.0;
    force_exit.exit_short = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let numeric_values = |value| {
        vec![
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
            serde_json::json!(value),
        ]
    };
    let features = BTreeMap::from([
        (
            "protections_short_global".to_owned(),
            vec![
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(true),
            ],
        ),
        (
            "protections_long_global".to_owned(),
            vec![
                serde_json::json!(true),
                serde_json::json!(true),
                serde_json::json!(false),
                serde_json::json!(false),
                serde_json::json!(false),
                serde_json::json!(false),
            ],
        ),
        ("RSI_3".to_owned(), numeric_values(20.0)),
        ("RSI_3_15m".to_owned(), numeric_values(20.0)),
        ("AROOND_14".to_owned(), numeric_values(10.0)),
        ("AROOND_14_15m".to_owned(), numeric_values(10.0)),
        ("close".to_owned(), numeric_values(100.0)),
        ("EMA_26".to_owned(), numeric_values(200.0)),
    ]);
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_full_short_manager(&mut manager);
    manager.programs.insert(
        "short_grind_entry_v3".to_owned(),
        nfi_boolean_true_program(),
    );
    let adjustment = manager
        .short_position_adjustment
        .as_mut()
        .expect("test manager has short position adjustment");
    adjustment.constants.grinds[3].enabled = true;
    adjustment.constants.grinds[3].stakes_futures = vec![0.05];

    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.leverage = Some(2.0);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, first_rebuy, second_rebuy, derisk, grind, force_exit],
        features,
    );
    pair.minimum_cost = Some(5.0);
    pair.minimum_amount = Some(0.1);
    pair.amount_step = Some(0.1);
    pair.price_step = Some(0.001);
    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source short rebuy-to-grind transfer");
    let trade = &result.trades[0];
    let derisk_index = trade
        .orders
        .iter()
        .position(|order| order.tag.as_deref() == Some("derisk_level_3"))
        .expect("short rebuy reaches its level-3 transfer");
    let remaining_amount = remaining_after_partial_exit(&trade.orders, derisk_index);

    let post_derisk_tag = trade.orders[derisk_index + 1].tag.as_deref();
    assert_eq!(post_derisk_tag, Some("grind_4_entry"));
    assert!(trade.orders[derisk_index + 1].is_entry);
    // Dividing the callback minimum by leverage leaves 0.2 contracts;
    // retaining the raw exchange minimum would incorrectly leave 0.4.
    assert!((remaining_amount - 0.2).abs() < 1e-12);
}

#[test]
fn nfi_long_grind_recovers_the_first_entry_once_with_gm0() {
    let mut entry = candle(1, 3.957, 3.957);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let recovery = candle(2, 4.037, 4.037);
    let mut force_exit = candle(3, 4.178, 4.178);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    let constants = nfi_legacy_grind_constants();
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "spot-grind-backtest-v1".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    manager_config.price_step = 0.001;
    manager_config.amount_step = 0.01;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, recovery, force_exit], BTreeMap::new());
    pair.price_step = Some(0.001);
    pair.amount_step = Some(0.01);
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input).expect("reviewed long-grind recovery route");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("gm0"));
    assert_eq!(trade.orders[1].price, 4.037);
    assert!(!trade.orders[1].is_entry);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn compiled_legacy_grind_executes_the_source_defined_first_entry_stop() {
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let stop = candle(1, 70.0, 70.0);
    let mut force_exit = candle(2, 70.0, 70.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_compiled_legacy_grind(&mut manager, nfi_legacy_grind_constants());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, stop, force_exit], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("compiled first-entry stop agrees with the legacy shadow");

    assert_eq!(result.trades[0].orders[1].tag.as_deref(), Some("gmd0"));
    assert!(!result.trades[0].orders[1].is_entry);
}

#[test]
fn nfi_long_grind_dual_mode_scope_accepts_a_futures_trade() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: Some(3.0),
        liquidation_price: None,
    });
    let ordinary_callback = candle(2, 99.0, 99.0);
    let mut force_exit = candle(3, 99.0, 99.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.schema_version = "0.14.0".to_owned();
    let constants = nfi_legacy_grind_constants();
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "grind-backtest-v2".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, ordinary_callback, force_exit], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed tag-120 futures callback route");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].entry_tag.as_deref(), Some("120 "));
    assert_eq!(result.trades[0].exit_reason, "force_exit");
}

#[test]
fn nfi_long_grind_uses_the_source_bound_futures_drawdown_fallback() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: Some(3.0),
        liquidation_price: None,
    });
    // -22% from the last order is beyond -0.65 / 3. The ordinary grind
    // predicate remains false and the candle is far younger than every
    // age gate, so only the source's futures fallback can add this order.
    let fallback = candle(2, 78.0, 78.0);
    let mut force_exit = candle(3, 78.0, 78.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.schema_version = "0.14.0".to_owned();
    let constants = nfi_legacy_grind_constants();
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "grind-backtest-v2".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: false,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, fallback, force_exit], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source-bound futures drawdown fallback");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 3);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
    assert_eq!(trade.orders[1].filled_timestamp_ms, 2);
    assert!(trade.orders[1].is_entry);
}

#[test]
fn compiled_legacy_grind_enters_a_post_derisk_cluster_before_ordinary_levels() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut derisk = candle(HOUR, 100.0, 100.0);
    derisk.adjustment = Some(AdjustmentSignal {
        stake_amount: -30.0,
        tag: "d1".to_owned(),
    });
    let post_derisk = candle(26 * HOUR, 90.0, 90.0);
    let mut force_exit = candle(27 * HOUR, 90.0, 90.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_compiled_legacy_grind(&mut manager, nfi_legacy_grind_constants());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, derisk, post_derisk, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("compiled post-derisk source order agrees with the legacy shadow");
    let orders = &result.trades[0].orders;

    assert_eq!(orders[1].tag.as_deref(), Some("d1"));
    assert!(!orders[1].is_entry);
    assert_eq!(orders[2].tag.as_deref(), Some("dl1"));
    assert!(orders[2].is_entry);
}

#[test]
fn compiled_legacy_grind_executes_one_bounded_derisk_buyback_cycle_atomically() {
    const MINUTE: i64 = 60 * 1_000;
    const HOUR: i64 = 60 * MINUTE;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: Some(3.0),
        liquidation_price: None,
    });
    let mut derisk = candle(HOUR, 100.0, 100.0);
    derisk.adjustment = Some(AdjustmentSignal {
        stake_amount: -30.0,
        tag: "d1".to_owned(),
    });

    let mut constants = nfi_legacy_grind_constants();
    for cluster in &mut constants.clusters {
        cluster.stakes_futures.truncate(1);
        cluster.thresholds_futures.truncate(1);
        cluster.stakes_spot.truncate(1);
        cluster.thresholds_spot.truncate(1);
    }
    let cluster_tags = constants
        .clusters
        .iter()
        .map(|cluster| cluster.entry_tag.clone())
        .collect::<Vec<_>>();
    let mut candles = vec![entry, derisk];
    for (index, tag) in cluster_tags.iter().enumerate() {
        let mut seed = candle(
            HOUR + i64::try_from(index + 1).unwrap() * MINUTE,
            100.0,
            100.0,
        );
        seed.adjustment = Some(AdjustmentSignal {
            stake_amount: 10.0,
            tag: tag.clone(),
        });
        candles.push(seed);
    }
    candles.push(candle(26 * HOUR, 90.0, 90.0));
    candles.push(candle(27 * HOUR, 80.0, 80.0));
    let mut force_exit = candle(28 * HOUR, 80.0, 80.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    candles.push(force_exit);

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.schema_version = "0.14.0".to_owned();
    enable_test_compiled_legacy_grind(&mut manager, constants);
    let route = manager.long_grind.as_mut().expect("compiled Grind route");
    route.adjustment_scope = "grind-backtest-v2".to_owned();
    route.derisk_use_grind_stops = false;
    let mut manager_config = config(1);
    manager_config.starting_balance = 10_000.0;
    manager_config.is_futures = true;
    enable_nfi_manager(&mut manager_config, manager);
    let feature_values = vec![serde_json::json!(true); candles.len()];
    let mut pair = nfi_pair(
        candles,
        BTreeMap::from([
            (
                "global_protections_long_dump".to_owned(),
                feature_values.clone(),
            ),
            ("global_protections_long_pump".to_owned(), feature_values),
        ]),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("compiled Derisk and Buyback cycle agrees exactly with the source shadow");
    let d1_orders = result.trades[0]
        .orders
        .iter()
        .filter(|order| order.tag.as_deref() == Some("d1"))
        .collect::<Vec<_>>();

    assert_eq!(d1_orders.len(), 3);
    assert!(!d1_orders[0].is_entry);
    assert!(d1_orders[1].is_entry);
    assert!(!d1_orders[2].is_entry);
    assert_eq!(d1_orders[1].filled_timestamp_ms, 26 * HOUR);
    assert_eq!(d1_orders[2].filled_timestamp_ms, 27 * HOUR);
    assert!(d1_orders[1].cost > d1_orders[0].cost * 2.9);
}

#[test]
fn compiled_legacy_grind_emits_the_source_defined_cluster_stop_tag() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let grind_entry = candle(25 * HOUR, 90.0, 90.0);
    let grind_stop = candle(26 * HOUR, 50.0, 50.0);
    let mut force_exit = candle(27 * HOUR, 50.0, 50.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_legacy_grind_constants();
    constants.clusters[0].stakes_spot.truncate(1);
    constants.clusters[0].thresholds_spot.truncate(1);
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_compiled_legacy_grind(&mut manager, constants);
    let route = manager.long_grind.as_mut().expect("compiled Grind route");
    route.first_entry_stop_threshold_spot = -1.0;
    let program = route.program.as_mut().expect("compiled Grind program");
    let CompiledLegacyGrindTransition::FirstEntry { stop_threshold, .. } =
        &mut program.source_order[0]
    else {
        panic!("v2 first-entry transition");
    };
    *stop_threshold = -1.0;
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, grind_entry, grind_stop, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("compiled cluster stop agrees with the legacy shadow");
    let orders = &result.trades[0].orders;

    assert_eq!(orders[1].tag.as_deref(), Some("gd1"));
    assert_eq!(
        orders[2].tag.as_deref(),
        Some(format!("dd1 {}", orders[1].id).as_str())
    );
    assert!(!orders[2].is_entry);
}

#[test]
fn compiled_legacy_grind_has_no_fixed_ordinary_level_count() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut constants = nfi_legacy_grind_constants();
    for cluster in &mut constants.clusters {
        cluster.stakes_spot.truncate(1);
        cluster.thresholds_spot.truncate(1);
    }
    let mut extra = constants.clusters[0].clone();
    extra.entry_tag = "future-source-level".to_owned();
    extra.stop_tag = "future-source-stop".to_owned();
    constants.clusters.push(extra);
    constants.max_stake_multiplier = 10.0;

    let mut candles = vec![entry];
    for level in 1..=7 {
        candles.push(candle(
            i64::from(level) * 25 * HOUR,
            91.0 - f64::from(level),
            91.0 - f64::from(level),
        ));
    }
    let mut force_exit = candle(8 * 25 * HOUR, 84.0, 84.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    candles.push(force_exit);

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_compiled_legacy_grind(&mut manager, constants);
    let mut manager_config = config(1);
    manager_config.starting_balance = 5_000.0;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(candles, BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source-defined seventh ordinary Grind level");

    assert!(result.trades[0]
        .orders
        .iter()
        .any(|order| order.tag.as_deref() == Some("future-source-level")));
}

#[test]
fn nfi_long_grind_wallet_rejection_stops_before_smaller_later_clusters() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let oversized_first_cluster = candle(25 * HOUR, 90.0, 90.0);
    let mut force_exit = candle(26 * HOUR, 90.0, 90.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_legacy_grind_constants();
    // The first matching cluster asks for more than the remaining wallet,
    // while gd6 would fit. NFI returns None at the first wallet guard and
    // never lets the smaller later cluster bypass source order.
    constants.clusters[0].stakes_spot = vec![1.0];
    constants.clusters[0].thresholds_spot = vec![-0.12];
    constants.clusters[5].stakes_spot = vec![0.1];
    constants.clusters[5].thresholds_spot = vec![-0.03];

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "spot-grind-backtest-v1".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());

    let mut manager_config = config(1);
    manager_config.starting_balance = 175.0;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, oversized_first_cluster, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("wallet rejection is an ordinary callback no-op");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 2);
    assert_eq!(trade.orders[0].tag.as_deref(), Some("120 "));
    assert_eq!(trade.orders[1].tag.as_deref(), Some("force_exit"));
}

#[test]
fn nfi_long_grind_opens_and_closes_a_gd1_cluster_in_source_order() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let grind_entry = candle(25 * HOUR, 90.0, 90.0);
    let grind_exit = candle(26 * HOUR, 93.0, 93.0);
    let mut force_exit = candle(27 * HOUR, 93.0, 93.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    let constants = nfi_legacy_grind_constants();
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "spot-grind-backtest-v1".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, grind_entry, grind_exit, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);
    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed legacy grind cluster");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 4);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
    assert!(trade.orders[1].is_entry);
    assert_eq!(
        trade.orders[2].tag.as_deref(),
        Some(format!("gd1 {}", trade.orders[1].id).as_str())
    );
    assert!(!trade.orders[2].is_entry);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn compiled_legacy_grind_reconstructs_gd1_before_reaching_gd2() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let grind_one = candle(25 * HOUR, 90.0, 90.0);
    let grind_two = candle(50 * HOUR, 89.0, 89.0);
    let mut force_exit = candle(51 * HOUR, 89.0, 89.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    let constants = nfi_legacy_grind_constants();
    let program = nfi_legacy_grind_program(&constants);
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "spot-grind-backtest-v1".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, grind_one, grind_two, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("compiled gd1/gd2 prefix agrees with the legacy shadow");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 4);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("gd1"));
    assert_eq!(trade.orders[2].tag.as_deref(), Some("gd2"));
    assert!(trade.orders[1].is_entry && trade.orders[2].is_entry);
}

#[test]
fn compiled_legacy_grind_shadow_rejects_a_policy_disagreement() {
    const MINUTE: i64 = 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let reached_boundary = candle(10 * MINUTE + 30_000, 90.0, 90.0);

    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    let constants = nfi_legacy_grind_constants();
    let mut program = nfi_legacy_grind_program(&constants);
    program.policy.entry_retry_ms = 11 * MINUTE;
    manager.long_grind = Some(NfiLongGrindRoute {
        mode_name: "long_grind".to_owned(),
        entry_tags: vec!["120".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "spot-grind-backtest-v1".to_owned(),
        grind_mode: true,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: Some(-0.65),
        derisk_use_grind_stops: true,
        stateful_input_contract: nfi_legacy_stateful_input_contract(),
        constants,
        program: Some(program),
        regular_decision_program: None,
        regular_constants: None,
        regular_program: None,
    });
    manager.route_order.insert(6, "long_grind".to_owned());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, reached_boundary], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    assert!(simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .is_err());
}

#[test]
fn nfi_long_btc_uses_its_source_ordered_profit_exit() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(
        &mut manager,
        nfi_regular_adjustment_constants(),
        nfi_false_program(),
    );
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, candle(2, 126.0, 126.0), candle(3, 126.0, 126.0)],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input).expect("reviewed long-btc route");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].close_timestamp_ms, 2);
    assert_eq!(result.trades[0].exit_reason, "exit_long_btc_g ( 121)");
}

#[test]
fn nfi_long_btc_adjustment_runs_on_the_entry_fill_candle() {
    // Callback-visible analyzed data is shifted by one candle, so keep one
    // warmup row before the entry just as the real vector input does.
    let warmup = candle(1, 100.0, 100.0);
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 100.0, 100.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let mut constants = nfi_regular_adjustment_constants();
    constants.rebuy_thresholds_spot.fill(-1.0);
    for grind in &mut constants.grinds {
        grind.thresholds_spot.fill(-1.0);
    }
    constants.derisk_threshold_spot = -10.0;
    constants.derisk_level_1_threshold_spot = 1.0;
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![warmup, entry, force_exit], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("Freqtrade invokes NFI adjust_trade_position on the entry candle");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[1].tag.as_deref(), Some("d1"));
    assert_eq!(trade.orders[1].filled_timestamp_ms, 2);
    assert!(!trade.orders[1].is_entry);
}

#[test]
fn nfi_long_btc_regular_mode_opens_and_closes_g1_in_source_order() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(5 * HOUR, 93.0, 93.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_regular_adjustment_constants();
    // This case isolates g1. Rebuy and de-risk remain structurally valid
    // but cannot match the selected prices.
    constants.rebuy_thresholds_spot.fill(-1.0);
    constants.derisk_threshold_spot = -1.0;
    constants.derisk_level_1_threshold_spot = -1.0;
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![
            entry,
            candle(3 * HOUR, 90.0, 90.0),
            candle(4 * HOUR, 93.0, 93.0),
            force_exit,
        ],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed tag-121 regular adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.orders.len(), 4);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("g1"));
    assert!(trade.orders[1].is_entry);
    assert_eq!(
        trade.orders[2].tag.as_deref(),
        Some(format!("g1 {}", trade.orders[1].id).as_str())
    );
    assert!(!trade.orders[2].is_entry);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn nfi_long_btc_futures_selects_the_futures_regular_branch() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(14 * HOUR, 90.0, 90.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_regular_adjustment_constants();
    // Only the futures rebuy threshold can match. This proves mode
    // selection without relying on the two branches sharing today's X7
    // values.
    constants.rebuy_thresholds_futures.fill(-0.01);
    constants.rebuy_thresholds_spot.fill(-1.0);
    constants.derisk_threshold_futures = -1.0;
    constants.derisk_level_1_threshold_futures = -1.0;
    for grind in &mut constants.grinds {
        grind.thresholds_futures.fill(-1.0);
        grind.thresholds_spot.fill(-1.0);
    }
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.leverage = Some(3.0);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, candle(13 * HOUR, 90.0, 90.0), force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed tag-121 futures adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.leverage, 3.0);
    assert_eq!(trade.orders[1].tag.as_deref(), Some("r"));
    assert!(trade.orders[1].is_entry);
    assert_eq!(trade.exit_reason, "force_exit");
}

#[test]
fn nfi_long_btc_futures_uses_the_source_compiled_drawdown_fallback() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(4 * HOUR, 70.0, 70.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let mut constants = nfi_regular_adjustment_constants();
    constants.rebuy_thresholds_futures.fill(-1.0);
    constants.derisk_threshold_futures = -1.0;
    constants.derisk_level_1_threshold_futures = -1.0;
    for grind in &mut constants.grinds {
        grind.thresholds_futures.fill(-1.0);
    }
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_false_program());
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.leverage = Some(3.0);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, candle(3 * HOUR, 70.0, 70.0), force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source-compiled regular Futures fallback");

    assert_eq!(result.trades[0].orders[1].tag.as_deref(), Some("g1"));
    assert!(result.trades[0].orders[1].is_entry);
}

#[test]
fn compiled_regular_adjustment_shadow_rejects_a_tag_disagreement() {
    let warmup = candle(1, 100.0, 100.0);
    let mut entry = candle(2, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 100.0, 100.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let mut constants = nfi_regular_adjustment_constants();
    constants.rebuy_thresholds_spot.fill(-1.0);
    for grind in &mut constants.grinds {
        grind.thresholds_spot.fill(-1.0);
    }
    constants.derisk_threshold_spot = -10.0;
    constants.derisk_level_1_threshold_spot = 1.0;
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let route = manager.long_btc.as_mut().expect("regular route");
    let program = route.regular_program.as_mut().expect("compiled program");
    for transition in &mut program.source_order {
        if let CompiledRegularTransition::Derisk {
            tag,
            level_one: true,
            ..
        } = transition
        {
            *tag = "source-level-one".to_owned();
        }
    }
    program.order_scan.derisk_level_one_tag = "source-level-one".to_owned();
    let level_one = program
        .order_scan
        .derisk_exit_tags
        .iter_mut()
        .find(|tag| tag.as_str() == "d1")
        .expect("level-one classification");
    *level_one = "source-level-one".to_owned();
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![warmup, entry, force_exit], BTreeMap::new());
    pair.minimum_cost = Some(5.0);

    assert!(simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .is_err());
}

#[test]
fn nfi_long_btc_futures_funding_precedes_regular_adjustment() {
    const MINUTE: i64 = 60 * 1_000;
    let mut entry = candle(0, 21_084.0, 21_084.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut funding = candle(435 * MINUTE, 20_913.0, 20_913.0);
    funding.funding_rate = Some(0.000_458_47);
    funding.funding_mark_price = Some(20_913.0);
    let adjustment = candle(515 * MINUTE, 20_476.4, 20_476.4);
    let mut force_exit = candle(520 * MINUTE, 20_476.4, 20_476.4);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_regular_adjustment_constants();
    constants.rebuy_thresholds_futures.fill(-1.0);
    constants.derisk_threshold_futures = -1.0;
    constants.derisk_level_1_threshold_futures = -1.0;
    for (grind, threshold) in constants
        .grinds
        .iter_mut()
        .zip([-0.06, -0.04, -0.03, -0.03, -0.03, -0.025])
    {
        grind.thresholds_futures.fill(threshold);
    }
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let mut manager_config = config(1);
    manager_config.starting_balance = 10_000.0;
    manager_config.stake_amount = 1_977.994_266_67;
    manager_config.fee_rate = 0.0005;
    manager_config.fee_open_rate = Some(0.0005);
    manager_config.fee_close_rate = Some(0.0005);
    manager_config.is_futures = true;
    manager_config.leverage = Some(3.0);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![entry, funding, adjustment, force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(50.0);
    pair.amount_step = Some(0.001);
    pair.price_step = Some(0.1);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("funding-aware tag-121 futures adjustment");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[1].tag.as_deref(), Some("g3"));
    assert!(trade.orders[1].is_entry);
}

#[test]
fn nfi_long_btc_derisk_transfers_to_the_legacy_continuation() {
    const HOUR: i64 = 60 * 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("121".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(15 * HOUR, 80.0, 80.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });

    let mut constants = nfi_regular_adjustment_constants();
    constants.rebuy_thresholds_spot.fill(-1.0);
    for grind in &mut constants.grinds {
        grind.thresholds_spot.fill(-1.0);
    }
    constants.derisk_threshold_spot = -0.05;
    constants.derisk_level_1_threshold_spot = -1.0;
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
    enable_test_long_btc(&mut manager, constants, nfi_boolean_true_program());
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![
            entry,
            candle(13 * HOUR, 90.0, 90.0),
            candle(14 * HOUR, 80.0, 80.0),
            force_exit,
        ],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("reviewed tag-121 legacy continuation");
    let trade = &result.trades[0];

    assert_eq!(trade.orders[1].tag.as_deref(), Some("d"));
    assert!(!trade.orders[1].is_entry);
    // A regular `d` (not `d1`) enables the first ordinary legacy grind.
    // The two `dl*` post-level-1 clusters remain source-ordered but require
    // an actual level-1 de-risk tag.
    assert_eq!(trade.orders[2].tag.as_deref(), Some("gd1"));
    assert!(trade.orders[2].is_entry);
    assert_eq!(trade.exit_reason, "force_exit");
}
