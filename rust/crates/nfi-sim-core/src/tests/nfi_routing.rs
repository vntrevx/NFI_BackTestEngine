//! Compiled NFI manager routing, exits, and validation contracts.

use super::*;

#[test]
fn nfi_top_coins_pure_decision_exits_with_the_original_entry_tag() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("141 142".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_profit_program(0.01, "exit_long_tc_test")),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 103.0, 102.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("supported top-coins route");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].exit_reason, "exit_long_tc_test ( 141 142)");
    assert_eq!(result.trades[0].close_timestamp_ms, 2);
}

#[test]
fn nfi_short_rebuy_runs_the_short_program_order_with_leverage() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("562 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_full_short_manager(&mut manager);
    manager.programs.insert(
        "short_exit_dec".to_owned(),
        nfi_profit_program(0.01, "exit_short_rebuy_d_3_100"),
    );
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.leverage = Some(3.0);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, candle(2, 90.0, 90.0)], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input).expect("bounded short-rebuy route");

    assert_eq!(result.trades.len(), 1);
    assert!(result.trades[0].is_short);
    assert_eq!(result.trades[0].leverage, 3.0);
    assert_eq!(
        result.trades[0].exit_reason,
        "exit_short_rebuy_d_3_100 ( 562 )"
    );
    assert_eq!(result.trades[0].close_timestamp_ms, 2);
}

#[test]
fn nfi_short_route_matching_preserves_each_upstream_tag_predicate() {
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.managed_short_routes.extend([
        nfi_managed_route(
            "short_quick",
            NfiManagedLongProfile::Quick,
            "short_quick",
            &["541", "542"],
        ),
        nfi_managed_route(
            "short_scalp",
            NfiManagedLongProfile::Scalp,
            "short_scalp",
            &["661"],
        ),
        nfi_managed_route(
            "short_top_coins_fallback",
            NfiManagedLongProfile::Normal,
            "short_normal",
            &["641", "642"],
        ),
    ]);
    let rebuy = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")
        .expect("test manager has short rebuy");
    let quick = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_quick")
        .expect("test manager has short quick");
    let scalp = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_scalp")
        .expect("test manager has short scalp");
    let top_coins = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_top_coins_fallback")
        .expect("test manager has short top-coins fallback");

    // Quick uses any(...), rebuy uses all(...), scalp permits its explicit
    // rebuy compound, and top-coins reaches normal fallback only when no
    // earlier explicit short family is present.
    assert!(nfi_managed_short_route_supports_tags(
        &manager,
        quick,
        &["542", "141"],
    ));
    assert!(!nfi_managed_short_route_supports_tags(
        &manager,
        rebuy,
        &["562", "141"],
    ));
    assert!(nfi_managed_short_route_supports_tags(
        &manager,
        scalp,
        &["661", "562"],
    ));
    assert!(nfi_managed_short_route_supports_tags(
        &manager,
        top_coins,
        &["641"],
    ));
    assert!(!nfi_managed_short_route_supports_tags(
        &manager,
        top_coins,
        &["641", "542"],
    ));
}

#[test]
fn nfi_short_quick_inline_exit_uses_mirrored_thresholds() {
    let route = nfi_managed_route(
        "short_quick",
        NfiManagedLongProfile::Quick,
        "short_quick",
        &["542"],
    );
    let pair = nfi_pair(
        vec![candle(1, 97.0, 97.0)],
        BTreeMap::from([
            ("RSI_14".to_owned(), vec![serde_json::json!(21.0)]),
            ("MFI_14".to_owned(), vec![serde_json::json!(50.0)]),
            ("WILLR_14".to_owned(), vec![serde_json::json!(-50.0)]),
            ("RSI_3".to_owned(), vec![serde_json::json!(50.0)]),
            ("RSI_3_15m".to_owned(), vec![serde_json::json!(50.0)]),
        ]),
    );
    let snapshot = NfiProfitSnapshot {
        stake: 1.0,
        ratio: 0.03,
        current_stake_ratio: 0.03,
        initial_stake_ratio: 0.03,
    };

    let decision = nfi_inline_profile_exit(&route, &pair, 0, snapshot, TradeSide::Short)
        .expect("short quick inputs are complete");

    assert_eq!(decision, (true, Some("exit_short_quick_q_1".to_owned())));
}

#[test]
fn nfi_normal_skips_profit_programs_while_initial_stake_is_negative() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("1".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 99.0, 99.0);
    force_exit.exit_long = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        // The predicate would return true at -1%, so a custom exit here
        // would prove that the source's positive-profit guard was lost.
        nfi_top_coins_manager(nfi_profit_program(-1.0, "should_not_run")),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 99.0, 99.0), force_exit],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("normal positive-profit guard");

    assert_eq!(result.trades[0].exit_reason, "force_exit");
}

#[test]
fn nfi_rebuy_terminal_exit_uses_source_compiled_policy() {
    const MINUTE: i64 = 60 * 1_000;
    let mut entry = candle(0, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("65 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager
        .managed_long_routes
        .iter_mut()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy)
        .expect("test manager has a rebuy route")
        .terminal_exit = Some(NfiManagedTerminalExit {
        entry_tags: vec!["65".to_owned()],
        minimum_age_ms: 90 * MINUTE,
        minimum_profit_ratio: 0.0125,
        reason: "exit_long_rebuy_signal65_early_recovery".to_owned(),
    });
    assert!(
        manager
            .managed_long_routes
            .iter()
            .all(valid_nfi_managed_long_route),
        "source-compiled managed routes must validate"
    );
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let neutral_values = vec![serde_json::json!(0.0); 4];
    let mut pair = nfi_pair(
        vec![
            entry,
            // Profit is sufficient, but the source age gate is not.
            candle(89 * MINUTE, 102.0, 102.0),
            // Age is sufficient, but the source profit gate is not.
            candle(90 * MINUTE, 101.0, 101.0),
            candle(91 * MINUTE, 102.0, 102.0),
        ],
        BTreeMap::from([
            ("RSI_14".to_owned(), vec![serde_json::json!(50.0); 4]),
            ("CMF_20".to_owned(), neutral_values.clone()),
            ("CMF_20_1h".to_owned(), neutral_values.clone()),
            ("CMF_20_4h".to_owned(), neutral_values.clone()),
            ("ROC_9_4h".to_owned(), neutral_values),
        ]),
    );
    pair.minimum_cost = Some(5.0);

    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source-compiled rebuy terminal exit");

    assert_eq!(result.trades[0].close_timestamp_ms, 91 * MINUTE);
    assert_eq!(
        result.trades[0].exit_reason,
        "exit_long_rebuy_signal65_early_recovery ( 65 )"
    );
}

#[test]
fn nfi_quick_runs_inline_exit_after_the_common_stop_check() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("41".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let features = BTreeMap::from([
        (
            "RSI_14".to_owned(),
            vec![serde_json::json!(79.0), serde_json::json!(50.0)],
        ),
        (
            "MFI_14".to_owned(),
            vec![serde_json::json!(50.0), serde_json::json!(50.0)],
        ),
        (
            "WILLR_14".to_owned(),
            vec![serde_json::json!(-50.0), serde_json::json!(-50.0)],
        ),
        (
            "RSI_3".to_owned(),
            vec![serde_json::json!(50.0), serde_json::json!(50.0)],
        ),
        (
            "RSI_3_15m".to_owned(),
            vec![serde_json::json!(50.0), serde_json::json!(50.0)],
        ),
    ]);
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(vec![entry, candle(2, 103.0, 103.0)], features)],
    };

    let result = simulate(&input).expect("quick inline profile exit");

    assert_eq!(result.trades[0].exit_reason, "exit_long_quick_q_1 ( 41)");
}

#[test]
fn nfi_high_profit_returns_a_doom_stop_without_waiting_for_target_replay() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("81".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.constants.system_v3_2_stops_enable = true;
    manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 94.0, 94.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("high-profit immediate stop policy");

    assert_eq!(
        result.trades[0].exit_reason,
        "exit_long_hp_stoploss_doom ( 81)"
    );
    assert_eq!(result.trades[0].close_timestamp_ms, 2);
}

#[test]
fn nfi_top_coins_profit_target_trails_on_the_next_candle() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("141".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let features = BTreeMap::from([
        (
            "RSI_14".to_owned(),
            vec![
                serde_json::json!(55.0),
                serde_json::json!(60.0),
                serde_json::json!(40.0),
                serde_json::json!(40.0),
            ],
        ),
        (
            "CMF_20".to_owned(),
            vec![
                serde_json::json!(0.1),
                serde_json::json!(0.1),
                serde_json::json!(-0.1),
                serde_json::json!(-0.1),
            ],
        ),
        (
            "CMF_20_1h".to_owned(),
            vec![
                serde_json::json!(0.1),
                serde_json::json!(0.1),
                serde_json::json!(-0.1),
                serde_json::json!(-0.1),
            ],
        ),
        (
            "CMF_20_4h".to_owned(),
            vec![
                serde_json::json!(0.1),
                serde_json::json!(0.1),
                serde_json::json!(-0.1),
                serde_json::json!(-0.1),
            ],
        ),
        (
            "ROC_9_4h".to_owned(),
            vec![
                serde_json::json!(0.0),
                serde_json::json!(0.0),
                serde_json::json!(0.0),
                serde_json::json!(0.0),
            ],
        ),
    ]);
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![
                entry,
                candle(2, 110.0, 109.0),
                candle(3, 106.0, 105.0),
                candle(4, 106.0, 105.0),
            ],
            features,
        )],
    };

    let result = simulate(&input).expect("exact top-coins trailing target");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(
        result.trades[0].exit_reason,
        "exit_profit_long_tc_t_5_1_m ( 141)"
    );
    // Candle 3's indicator values are not visible until candle 4 opens.
    assert_eq!(result.trades[0].close_timestamp_ms, 4);
}

#[test]
fn nfi_top_coins_doom_stop_is_reserved_then_exits_with_m_suffix() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("145".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.constants.system_v3_2_stops_enable = true;
    manager.constants.system_v3_2_stop_threshold_doom_spot = 0.05;
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 94.0, 93.0), candle(3, 94.0, 93.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("two-phase NFI doom stop");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(
        result.trades[0].exit_reason,
        "exit_long_tc_stoploss_doom_m ( 145)"
    );
    assert_eq!(result.trades[0].close_timestamp_ms, 3);
}

#[test]
fn nfi_trade_manager_rejects_unsupported_entry_tags_before_simulation() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("120".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
            if entry_tag == "120"
    ));
}

#[test]
fn nfi_trade_manager_rejects_a_mixed_unknown_tag() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        // Rebuy is compiled, but one unknown word can still select an
        // unreviewed source branch after future strategy changes.
        tag: Some("61 999".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
            if entry_tag == "61 999"
    ));
}

#[test]
fn nfi_trade_manager_accepts_a_compiled_cross_side_compound_noop() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        // X7's shared enter_tag column can contain a simultaneous short
        // label in spot mode. Neither all-tags route matches this pair.
        tag: Some("101 562 ".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("compiled cross-side compound");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].entry_tag.as_deref(), Some("101 562 "));
    assert_eq!(result.trades[0].exit_reason, "force_exit");
}

#[test]
fn nfi_cross_side_compound_keeps_source_callback_order() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        // The source evaluates long-normal's any-tag branch before the
        // short branches, regardless of the opened trade side.
        tag: Some("1 562".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_profit_program(0.01, "exit_long_normal_test")),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 90.0, 90.0)],
            BTreeMap::new(),
        )],
    };

    let result = simulate(&input).expect("source-ordered cross-side callbacks");

    assert!(result.trades[0].is_short);
    assert_eq!(
        result.trades[0].exit_reason,
        "exit_long_normal_test ( 1 562)"
    );
}

#[test]
fn nfi_trade_manager_requires_a_tag_for_the_opened_side() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("562".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
            if entry_tag == "562"
    ));
}

#[test]
fn nfi_validation_rejects_an_unsupported_short_tag_in_the_main_pass() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("999".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::UnsupportedNfiEntryTag { entry_tag, .. })
            if entry_tag == "999"
    ));
}

#[test]
fn nfi_validation_keeps_general_candle_errors_ahead_of_short_tag_errors() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("999".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    enable_nfi_manager(
        &mut manager_config,
        nfi_top_coins_manager(nfi_false_program()),
    );
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(1, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::CandleOrder { index: 1, .. })
    ));
}
