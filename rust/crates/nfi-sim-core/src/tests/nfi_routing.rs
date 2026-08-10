//! Compiled NFI manager routing, exits, and validation contracts.

use super::*;
use crate::nfi::NFI_LONG_EXIT_PROGRAMS;

#[test]
fn retired_execution_modes_still_deserialize_for_evidence_replay() {
    let managed: ManagedExitExecutionMode =
        serde_json::from_str("\"primary-with-legacy-shadow\"").expect("managed mode");
    let regular: CompiledRegularExecutionMode =
        serde_json::from_str("\"primary-with-legacy-shadow\"").expect("regular mode");
    let grind: CompiledLegacyGrindExecutionMode =
        serde_json::from_str("\"primary-with-legacy-shadow\"").expect("grind mode");
    let adjustment: CompiledSystemAdjustmentExecutionMode =
        serde_json::from_str("\"primary-with-legacy-shadow\"").expect("adjustment mode");

    assert_eq!(managed, ManagedExitExecutionMode::PrimaryWithLegacyShadow);
    assert_eq!(
        regular,
        CompiledRegularExecutionMode::PrimaryWithLegacyShadow
    );
    assert_eq!(
        grind,
        CompiledLegacyGrindExecutionMode::PrimaryWithLegacyShadow
    );
    assert_eq!(
        adjustment,
        CompiledSystemAdjustmentExecutionMode::PrimaryWithLegacyShadow
    );
}

#[test]
fn nfi_dispatch_plan_derives_tag_ids_route_indexes_and_program_handles() {
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    let source_tag = "source-defined-tag".to_owned();
    manager.managed_long_routes[0].entry_tags = vec![source_tag.clone()];
    let original_route_order = manager.route_order.clone();

    let dispatch = manager.runtime_dispatch().expect("derived dispatch plan");
    let entry_tag = format!("{source_tag} unknown-companion");
    let parsed = dispatch.intern_tag_ids(&entry_tag);

    assert!(parsed[0].is_some());
    assert!(parsed[1].is_none());
    assert_eq!(manager.managed_long_routes[0].entry_tags, [source_tag]);
    let dispatched_keys = dispatch
        .long_steps
        .iter()
        .map(|step| match step {
            NfiLongDispatchStep::Managed(step) => {
                manager.managed_long_routes[step.route_index].key.as_str()
            }
            NfiLongDispatchStep::LongGrind => "long_grind",
            NfiLongDispatchStep::LongBtc => "long_btc",
        })
        .collect::<Vec<_>>();
    assert_eq!(
        dispatched_keys,
        original_route_order
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
    );
    let NfiLongDispatchStep::Managed(first) = &dispatch.long_steps[0] else {
        panic!("first source route must remain managed");
    };
    assert_eq!(
        first.legacy_program_handles.len(),
        NFI_LONG_EXIT_PROGRAMS.len()
    );
    assert!(first
        .legacy_program_handles
        .iter()
        .all(|handle| dispatch.program(*handle, &manager.programs).is_some()));
}

#[test]
fn primary_managed_exit_dispatch_does_not_register_legacy_program_handles() {
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_basic_exit_shadow(&mut manager, "long_top_coins", None);
    manager
        .route_order
        .retain(|route| route == "long_top_coins");
    manager
        .managed_long_routes
        .retain(|route| route.key == "long_top_coins");
    manager
        .managed_exit_program
        .as_mut()
        .expect("source-compiled managed exit")
        .execution_mode = ManagedExitExecutionMode::Primary;

    let dispatch = manager.runtime_dispatch().expect("primary dispatch plan");
    let NfiLongDispatchStep::Managed(step) = &dispatch.long_steps[0] else {
        panic!("first source route must remain managed");
    };

    assert!(step.legacy_program_handles.is_empty());
    assert!(!step.source_program_handles.is_empty());
}

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
    enable_test_short_exit_shadow(&mut manager);
    manager.programs.insert(
        "short_exit_dec".to_owned(),
        nfi_profit_program(0.01, "exit_short_rebuy_d_3_100"),
    );
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.max_entry_position_adjustment = 0;
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
fn generic_managed_exit_shadow_short_preserves_compound_scalp_routing() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("661 562".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut force_exit = candle(3, 99.0, 99.0);
    force_exit.exit_short = Some(ExitSignal {
        reason: "force_exit".to_owned(),
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_full_short_manager(&mut manager);
    enable_test_short_exit_shadow(&mut manager);
    manager.programs.insert(
        "short_exit_signals".to_owned(),
        nfi_profit_program(-1.0, "compound_short_scalp"),
    );
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.max_entry_position_adjustment = 0;
    enable_nfi_manager(&mut manager_config, manager);

    let mut pair = nfi_pair(
        vec![entry, candle(2, 100.0, 100.0), force_exit],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);
    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("source-compiled short scalp matcher agrees with legacy routing");

    assert!(result.trades[0].is_short);
    assert_eq!(
        result.trades[0].exit_reason,
        "compound_short_scalp ( 661 562)"
    );
}

#[test]
fn generic_managed_exit_shadow_short_fails_closed_on_target_state_difference() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_short = Some(EntrySignal {
        tag: Some("501".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_full_short_manager(&mut manager);
    enable_test_short_exit_shadow(&mut manager);
    manager
        .managed_short_exit_program
        .as_mut()
        .expect("short shadow program")
        .routes
        .iter_mut()
        .find(|route| route.id == "short_normal")
        .expect("short normal route")
        .state_program
        .as_mut()
        .expect("short state program")
        .target
        .max_target_floor = 0.50;
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    manager_config.max_entry_position_adjustment = 0;
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(
        vec![
            entry,
            candle(2, 95.0, 95.0),
            candle(3, 95.0, 95.0),
            candle(4, 95.0, 95.0),
        ],
        BTreeMap::from([
            (
                "close".to_owned(),
                vec![
                    serde_json::json!(95.0),
                    serde_json::json!(95.0),
                    serde_json::json!(95.0),
                    serde_json::json!(95.0),
                ],
            ),
            (
                "EMA_200".to_owned(),
                vec![
                    serde_json::json!(200.0),
                    serde_json::json!(200.0),
                    serde_json::json!(200.0),
                    serde_json::json!(200.0),
                ],
            ),
            (
                "RSI_14".to_owned(),
                vec![
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                ],
            ),
            (
                "CMF_20".to_owned(),
                vec![
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                    serde_json::json!(0.0),
                ],
            ),
            (
                "RSI_14_1h".to_owned(),
                vec![
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                    serde_json::json!(50.0),
                ],
            ),
        ]),
    );
    pair.minimum_cost = Some(5.0);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    };

    let result = simulate(&input);
    assert!(
        matches!(result, Err(SimError::InvalidNfiTradeManager)),
        "unexpected result: {result:?}"
    );
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
fn generic_managed_exit_shadow_matches_all_eight_legacy_routes() {
    for (route_key, tag, gate) in [
        ("long_normal", "1", true),
        ("long_pump", "21", true),
        ("long_quick", "41", true),
        ("long_rebuy", "61", false),
        ("long_high_profit", "81", false),
        ("long_rapid", "101", true),
        ("long_top_coins", "141", false),
        ("long_scalp", "161", false),
    ] {
        let mut entry = candle(1, 100.0, 100.0);
        entry.enter_long = Some(EntrySignal {
            tag: Some(tag.to_owned()),
            leverage: None,
            liquidation_price: None,
        });
        let mut manager = nfi_top_coins_manager(nfi_profit_program(0.01, "compiled_basic_exit"));
        enable_test_basic_exit_shadow(
            &mut manager,
            route_key,
            gate.then_some(ManagedExitProfitGate {
                operator: ManagedExitComparison::GreaterThan,
                value: 0.0,
            }),
        );
        let mut manager_config = config(1);
        enable_nfi_manager(&mut manager_config, manager);
        let mut pair = nfi_pair(vec![entry, candle(2, 103.0, 103.0)], BTreeMap::new());
        pair.minimum_cost = Some(5.0);
        let result = simulate(&SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manager_config,
            pairs: vec![pair],
        })
        .expect("source-compiled shadow agrees with the legacy route");

        assert_eq!(
            result.trades[0].exit_reason,
            format!("compiled_basic_exit ( {tag})")
        );
    }
}

#[test]
fn generic_managed_exit_shadow_fails_closed_on_a_decision_difference() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("1".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_profit_program(-1.0, "must_be_gated"));
    // Removing the source-compiled positive-profit gate makes the candidate
    // fire while the legacy route correctly remains idle.
    enable_test_basic_exit_shadow(&mut manager, "long_normal", None);
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 99.0, 99.0)],
            BTreeMap::new(),
        )],
    });

    assert!(matches!(result, Err(SimError::InvalidNfiTradeManager)));
}

#[test]
fn generic_managed_exit_shadow_fails_closed_on_target_state_difference() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("141".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_basic_exit_shadow(&mut manager, "long_top_coins", None);
    manager
        .managed_exit_program
        .as_mut()
        .expect("shadow program")
        .routes[0]
        .state_program
        .as_mut()
        .expect("shadow state")
        .target
        .max_target_floor = 0.50;
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(
            vec![entry, candle(2, 101.0, 101.0)],
            BTreeMap::new(),
        )],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::InvalidNfiTradeManager)
    ));
}

#[test]
fn generic_managed_exit_shadow_fails_closed_on_inline_program_difference() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("41".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_quick_inline_shadow(&mut manager);
    let inline = manager
        .managed_exit_program
        .as_mut()
        .expect("shadow program")
        .routes[0]
        .state_program
        .as_mut()
        .expect("shadow state")
        .inline_exit
        .as_mut()
        .expect("inline program");
    inline.program.expressions[7] = serde_json::json!(["literal", "changed_reason"]);
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
    enable_nfi_manager(&mut manager_config, manager);
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![nfi_pair(vec![entry, candle(2, 103.0, 103.0)], features)],
    };

    assert!(matches!(
        simulate(&input),
        Err(SimError::InvalidNfiTradeManager)
    ));
}

#[test]
fn generic_managed_exit_shadow_executes_a_recursive_source_matcher() {
    let mut entry = candle(1, 100.0, 100.0);
    entry.enter_long = Some(EntrySignal {
        tag: Some("61".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut manager = nfi_top_coins_manager(nfi_profit_program(0.01, "recursive_match"));
    enable_test_basic_exit_shadow(&mut manager, "long_rebuy", None);
    let rebuy_tags = manager
        .managed_long_routes
        .iter()
        .find(|route| route.key == "long_rebuy")
        .expect("rebuy route")
        .entry_tags
        .clone();
    manager
        .managed_exit_program
        .as_mut()
        .expect("shadow program")
        .routes[0]
        .matcher = ManagedExitTagMatcher {
        operator: ManagedExitTagOperator::AnyOf,
        entry_tags: Vec::new(),
        operands: vec![
            ManagedExitTagMatcher {
                operator: ManagedExitTagOperator::All,
                entry_tags: rebuy_tags.clone(),
                operands: Vec::new(),
            },
            ManagedExitTagMatcher {
                operator: ManagedExitTagOperator::AllOf,
                entry_tags: Vec::new(),
                operands: vec![
                    ManagedExitTagMatcher {
                        operator: ManagedExitTagOperator::Any,
                        entry_tags: rebuy_tags.clone(),
                        operands: Vec::new(),
                    },
                    ManagedExitTagMatcher {
                        operator: ManagedExitTagOperator::All,
                        entry_tags: rebuy_tags,
                        operands: Vec::new(),
                    },
                ],
            },
        ],
    };
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
    let mut pair = nfi_pair(vec![entry, candle(2, 103.0, 103.0)], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let result = simulate(&SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: manager_config,
        pairs: vec![pair],
    })
    .expect("recursive source matcher agrees with legacy routing");

    assert_eq!(result.trades[0].exit_reason, "recursive_match ( 61)");
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
    enable_test_basic_exit_shadow(&mut manager, "long_rebuy", None);
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
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_quick_inline_shadow(&mut manager);
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
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
    enable_test_basic_exit_shadow(&mut manager, "long_high_profit", None);
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
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    enable_test_basic_exit_shadow(&mut manager, "long_top_coins", None);
    let mut manager_config = config(1);
    enable_nfi_manager(&mut manager_config, manager);
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
