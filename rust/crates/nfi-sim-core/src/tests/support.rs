//! Shared builders for simulator contract tests.

use super::*;
use std::sync::OnceLock;

pub(super) fn candle(timestamp_ms: i64, open: f64, low: f64) -> Candle {
    Candle {
        timestamp_ms,
        open,
        high: open + 10.0,
        low,
        close: open,
        volume: 1.0,
        previous_close: None,
        enter_long: None,
        enter_short: None,
        exit_long: None,
        exit_short: None,
        funding_rate: None,
        funding_mark_price: None,
        adjustment: None,
    }
}

pub(super) fn config(max_open_trades: usize) -> PortfolioConfig {
    PortfolioConfig {
        starting_balance: 1_000.0,
        max_open_trades,
        stake_amount: 100.0,
        fee_rate: 0.001,
        fee_open_rate: None,
        fee_close_rate: None,
        leverage: None,
        nfi_leverage_program: None,
        maximum_leverage_by_pair: BTreeMap::new(),
        liquidation_model: None,
        protection_program: None,
        stoploss_ratio: -0.01,
        amount_step: 0.00001,
        price_step: 0.01,
        custom_exit_after_ms: None,
        adjustment_rule: None,
        callback_program: None,
        state_machine_program: None,
        stake_program: None,
        amount_reserve_percent: 0.05,
        unlimited_stake: false,
        tradable_balance_ratio: 1.0,
        entry_confirmation_program: None,
        exit_confirmation_program: None,
        custom_exit_program: None,
        adjust_trade_position_program: None,
        nfi_x7_trade_manager: None,
        max_entry_position_adjustment: -1,
        is_futures: false,
        funding_fee_interval_ms: None,
    }
}

pub(super) fn remaining_after_partial_exit(orders: &[FilledOrder], exit_index: usize) -> f64 {
    orders[..exit_index]
        .iter()
        .filter(|order| order.is_entry)
        .map(|order| order.amount)
        .sum::<f64>()
        - orders[exit_index].amount
}

pub(super) fn isolated_model(pair: &str, tiers: Vec<LeverageTier>) -> IsolatedLiquidationModel {
    IsolatedLiquidationModel {
        exchange: "binance".to_owned(),
        margin_mode: "isolated".to_owned(),
        buffer: 0.05,
        tiers_by_pair: BTreeMap::from([(pair.to_owned(), tiers)]),
    }
}

pub(super) fn leverage_tier(
    min_notional: f64,
    max_notional: Option<f64>,
    maximum_leverage: f64,
    maintenance_margin_rate: f64,
    maintenance_amount: f64,
) -> LeverageTier {
    LeverageTier {
        min_notional,
        max_notional,
        maximum_leverage,
        maintenance_margin_rate,
        maintenance_amount: Some(maintenance_amount),
    }
}

pub(super) fn buffered_liquidation_price(
    side: TradeSide,
    stake_amount: f64,
    amount: f64,
    open_rate: f64,
    maintenance_margin_rate: f64,
    maintenance_amount: f64,
    buffer: f64,
) -> f64 {
    let direction = if side == TradeSide::Short { -1.0 } else { 1.0 };
    let raw = (stake_amount + maintenance_amount - direction * amount * open_rate)
        / (amount * maintenance_margin_rate - direction * amount);
    let offset = (open_rate - raw).abs() * buffer;
    if side == TradeSide::Short {
        raw - offset
    } else {
        raw + offset
    }
}

pub(super) fn protection_timing(
    lookback_ms: i64,
    duration_ms: i64,
    lookback_text: &str,
    lock_text: &str,
) -> ProtectionTiming {
    ProtectionTiming {
        lookback_ms,
        lookback_text: lookback_text.to_owned(),
        duration_ms: Some(duration_ms),
        unlock_at_minute_utc: None,
        lock_text: lock_text.to_owned(),
    }
}

pub(super) fn protection_trade(
    id: u64,
    pair: &str,
    close_timestamp_ms: i64,
    profit_ratio: f64,
    exit_reason: &str,
    side: TradeSide,
) -> ClosedTrade {
    ClosedTrade {
        sequence: usize::try_from(id - 1).expect("small fixture id"),
        id,
        pair: pair.to_owned(),
        is_short: side == TradeSide::Short,
        leverage: 1.0,
        open_timestamp_ms: close_timestamp_ms - 60_000,
        close_timestamp_ms,
        open_rate: 100.0,
        close_rate: 100.0 * (1.0 + profit_ratio),
        amount: 1.0,
        stake_amount: 100.0,
        max_stake_amount: 100.0,
        entry_tag: None,
        exit_reason: exit_reason.to_owned(),
        fee_open: 0.0,
        fee_close: 0.0,
        funding_fees: 0.0,
        liquidation_price: None,
        profit_abs: profit_ratio * 100.0,
        profit_ratio,
        initial_stop_loss: 1.0,
        stop_loss: 1.0,
        minimum_rate: 100.0,
        maximum_rate: 100.0,
        orders: Vec::new(),
    }
}

pub(super) fn nfi_false_program() -> ScalarDecisionProgram {
    serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": [],
        "expressions": [
            ["literal", false],
            ["literal", null],
            ["tuple", [0, 1]]
        ],
        "statements": [["return", 2]]
    }))
    .expect("valid false decision")
}

pub(super) fn nfi_boolean_false_program() -> ScalarDecisionProgram {
    serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": [],
        "expressions": [["literal", false]],
        "statements": [["return", 0]]
    }))
    .expect("valid false predicate")
}

pub(super) fn nfi_boolean_true_program() -> ScalarDecisionProgram {
    serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": [],
        "expressions": [["literal", true]],
        "statements": [["return", 0]]
    }))
    .expect("valid true predicate")
}

pub(super) fn nfi_profit_program(threshold: f64, reason: &str) -> ScalarDecisionProgram {
    serde_json::from_value(serde_json::json!({
        "schema_version": "1.1.0",
        "opcode": "scalar-decision-program-v1",
        "parameters": ["current_profit"],
        "expressions": [
            ["variable", "current_profit"],
            ["literal", threshold],
            ["compare", 0, [["greater", 1]]],
            ["literal", true],
            ["literal", reason],
            ["tuple", [3, 4]],
            ["literal", false],
            ["literal", null],
            ["tuple", [6, 7]]
        ],
        "statements": [
            ["if", 2, [["return", 5]], []],
            ["return", 8]
        ]
    }))
    .expect("valid profit decision")
}

pub(super) fn nfi_managed_route(
    key: &str,
    profile: NfiManagedLongProfile,
    mode_name: &str,
    entry_tags: &[&str],
) -> NfiManagedLongRoute {
    let has_dedicated_stop = matches!(
        profile,
        NfiManagedLongProfile::Rebuy | NfiManagedLongProfile::Rapid | NfiManagedLongProfile::Scalp
    );
    NfiManagedLongRoute {
        key: key.to_owned(),
        profile,
        mode_name: mode_name.to_owned(),
        entry_tags: entry_tags.iter().map(ToString::to_string).collect(),
        stop_threshold_futures: has_dedicated_stop.then_some(0.35),
        stop_threshold_spot: has_dedicated_stop.then_some(0.12),
        terminal_exit: None,
    }
}

pub(super) fn nfi_legacy_grind_constants() -> NfiLegacyGrindConstants {
    let tags = [
        ("gd1", "dd1"),
        ("gd2", "dd2"),
        ("gd3", "dd3"),
        ("gd4", "dd4"),
        ("gd5", "dd5"),
        ("gd6", "dd6"),
        ("dl1", "ddl1"),
        ("dl2", "ddl2"),
    ];
    NfiLegacyGrindConstants {
        max_stake_multiplier: 1.0,
        stake_multipliers_futures: vec![0.2, 0.3, 0.4, 0.5],
        stake_multipliers_spot: vec![0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        derisk_1_reentry_futures: -0.08,
        derisk_1_reentry_spot: -0.08,
        clusters: tags
            .into_iter()
            .map(|(entry_tag, stop_tag)| NfiLegacyGrindCluster {
                entry_tag: entry_tag.to_owned(),
                stop_tag: stop_tag.to_owned(),
                stakes_futures: vec![0.2, 0.24, 0.28],
                stakes_spot: vec![0.2, 0.24, 0.28],
                thresholds_futures: vec![-0.12, -0.16, -0.20],
                thresholds_spot: vec![-0.12, -0.16, -0.20],
                stop_threshold_futures: -0.06,
                stop_threshold_spot: -0.06,
                profit_threshold_futures: 0.018,
                profit_threshold_spot: 0.018,
            })
            .collect(),
    }
}

pub(super) fn nfi_regular_adjustment_constants() -> NfiRegularAdjustmentConstants {
    NfiRegularAdjustmentConstants {
        use_grind_stops: true,
        derisk_enable: true,
        rebuy_stakes_futures: vec![0.2, 0.25],
        rebuy_stakes_spot: vec![0.2, 0.25],
        rebuy_thresholds_futures: vec![-0.08, -0.12],
        rebuy_thresholds_spot: vec![-0.08, -0.12],
        derisk_threshold_futures: -0.6,
        derisk_threshold_spot: -0.6,
        derisk_level_1_threshold_futures: -0.4,
        derisk_level_1_threshold_spot: -0.4,
        grinds: (1..=6)
            .map(|level| NfiRegularGrind {
                entry_tag: format!("g{level}"),
                stop_tag: format!("sg{level}"),
                stakes_futures: vec![0.2, 0.25],
                stakes_spot: vec![0.2, 0.25],
                thresholds_futures: vec![-0.08, -0.12],
                thresholds_spot: vec![-0.08, -0.12],
                stop_threshold_futures: -0.2,
                stop_threshold_spot: -0.2,
                profit_threshold_futures: 0.018,
                profit_threshold_spot: 0.018,
            })
            .collect(),
        policy: NfiRegularAdjustmentPolicy {
            entry_retry_ms: 10 * 60 * 1_000,
            grind_force_order_age_ms: 2 * 60 * 60 * 1_000,
            grind_order_age_ms: 6 * 60 * 60 * 1_000,
            rebuy_order_age_ms: 12 * 60 * 60 * 1_000,
            grind_entry_profit_gate: -0.02,
            additional_grind_profit_gate: -0.03,
            forced_age_profit_gate: -0.06,
            minimum_entry_multiplier: 1.5,
            minimum_remaining_multiplier: 1.55,
        },
    }
}

pub(super) fn enable_test_long_btc(
    manager: &mut NfiX7TradeManager,
    constants: NfiRegularAdjustmentConstants,
    regular_program: ScalarDecisionProgram,
) {
    manager
        .programs
        .insert("long_grind_entry".to_owned(), regular_program);
    manager.long_btc = Some(NfiLongGrindRoute {
        mode_name: "long_btc".to_owned(),
        entry_tags: vec!["121".to_owned()],
        exit_profit_threshold: 0.25,
        adjustment_scope: "regular-backtest-v2".to_owned(),
        grind_mode: false,
        decision_program: "long_grind_entry_v3".to_owned(),
        first_entry_profit_threshold_spot: 0.018,
        first_entry_stop_threshold_spot: -0.2,
        futures_fallback_loss_threshold: None,
        derisk_use_grind_stops: true,
        stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
        constants: nfi_legacy_grind_constants(),
        regular_decision_program: Some("long_grind_entry".to_owned()),
        regular_constants: Some(constants),
    });
    manager.route_order.insert(6, "long_btc".to_owned());
}

#[allow(clippy::too_many_lines)] // Full valid manager fixture is intentionally explicit.
pub(super) fn nfi_top_coins_manager(first: ScalarDecisionProgram) -> NfiX7TradeManager {
    let false_program = nfi_false_program();
    let managed_long_routes = vec![
        nfi_managed_route(
            "long_normal",
            NfiManagedLongProfile::Normal,
            "long_normal",
            &["1"],
        ),
        nfi_managed_route(
            "long_pump",
            NfiManagedLongProfile::Pump,
            "long_pump",
            &["21"],
        ),
        nfi_managed_route(
            "long_quick",
            NfiManagedLongProfile::Quick,
            "long_quick",
            &["41"],
        ),
        nfi_managed_route(
            "long_rebuy",
            NfiManagedLongProfile::Rebuy,
            "long_rebuy",
            &["61", "62", "63", "64", "65"],
        ),
        nfi_managed_route(
            "long_high_profit",
            NfiManagedLongProfile::HighProfit,
            "long_hp",
            &["81"],
        ),
        nfi_managed_route(
            "long_rapid",
            NfiManagedLongProfile::Rapid,
            "long_rapid",
            &["101"],
        ),
        nfi_managed_route(
            "long_top_coins",
            NfiManagedLongProfile::TopCoins,
            "long_tc",
            &["141", "142", "143", "144", "145"],
        ),
        nfi_managed_route(
            "long_scalp",
            NfiManagedLongProfile::Scalp,
            "long_scalp",
            &["161"],
        ),
    ];
    let adjustment_tags = managed_long_routes
        .iter()
        .flat_map(|route| route.entry_tags.clone())
        .collect();
    let mut short_rebuy_route = nfi_managed_route(
        "short_rebuy",
        NfiManagedLongProfile::Rebuy,
        "short_rebuy",
        &["561", "562", "563"],
    );
    short_rebuy_route.stop_threshold_futures = Some(1.4);
    short_rebuy_route.stop_threshold_spot = Some(0.48);
    let rebuy_constants = NfiX7RebuyConstants {
        derisk_enable: true,
        stakes_futures: vec![1.0, 1.0, 1.0, 1.0],
        stakes_spot: vec![1.0, 1.0, 1.0, 1.0],
        thresholds_futures: vec![-0.08, -0.12, -0.16, -0.20],
        thresholds_spot: vec![-0.08, -0.12, -0.16, -0.20],
        derisk_futures: -1.40,
        derisk_spot: -0.48,
    };
    NfiX7TradeManager {
        schema_version: "0.13.0".to_owned(),
        source_sha256: "a".repeat(64),
        route_order: [
            "long_normal",
            "long_pump",
            "long_quick",
            "long_rebuy",
            "long_high_profit",
            "long_rapid",
            "long_top_coins",
            "long_scalp",
        ]
        .into_iter()
        .map(ToOwned::to_owned)
        .collect(),
        managed_long_routes,
        managed_exit_program: None,
        short_route_order: vec!["short_rebuy".to_owned()],
        managed_short_routes: vec![short_rebuy_route],
        long_grind: None,
        long_btc: None,
        rebuy_adjustment: NfiX7RebuyAdjustment {
            enabled: true,
            entry_tags: ["61", "62", "63", "64", "65"]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
            system_version: "system_v3_2".to_owned(),
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: rebuy_constants.clone(),
        },
        short_rebuy_adjustment: NfiX7ShortRebuyAdjustment {
            enabled: true,
            entry_tags: ["561", "562", "563"]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
            system_version: "system_v3_2".to_owned(),
            execution_scope: "pre-derisk-only-v1".to_owned(),
            post_derisk_action: "fail-simulation".to_owned(),
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: rebuy_constants,
        },
        position_adjustment: Some(NfiX7PositionAdjustment {
            enabled: false,
            entry_tags: adjustment_tags,
            system_version: "system_v3_2".to_owned(),
            decision_program: "long_grind_entry_v3".to_owned(),
            program_order: [
                "derisk_level_1",
                "derisk_level_2",
                "derisk_level_3",
                "grind_1_entry",
                "grind_1_exit",
                "grind_1_derisk",
                "grind_2_entry",
                "grind_2_exit",
                "grind_2_derisk",
                "grind_3_entry",
                "grind_3_exit",
                "grind_3_derisk",
                "grind_4_entry",
                "grind_4_exit",
                "grind_4_derisk",
                "grind_5_entry",
                "grind_5_exit",
                "grind_5_derisk",
            ]
            .into_iter()
            .map(ToOwned::to_owned)
            .collect(),
            stateful_input_contract: serde_json::json!({"indexed_fields": {}}),
            constants: NfiX7AdjustmentConstants {
                derisk_enable: false,
                max_stake_multiplier: 1.0,
                rebuy_stake_multiplier: Some(0.25),
                derisk_levels: (1..=3)
                    .map(|level| NfiX7DeriskLevel {
                        level,
                        enabled: false,
                        threshold_futures: -0.1,
                        threshold_spot: -0.1,
                        stake_futures: 0.1,
                        stake_spot: 0.1,
                    })
                    .collect(),
                grinds: (1..=5)
                    .map(|level| NfiX7GrindLevel {
                        level,
                        enabled: false,
                        use_derisk: false,
                        derisk_futures: -0.2,
                        derisk_spot: -0.2,
                        profit_threshold_futures: 0.02,
                        profit_threshold_spot: 0.02,
                        stakes_futures: vec![0.1],
                        stakes_spot: vec![0.1],
                        thresholds_futures: vec![-0.1],
                        thresholds_spot: vec![-0.1],
                    })
                    .collect(),
                policy: Some(nfi_adjustment_policy()),
            },
        }),
        short_position_adjustment: None,
        constants: NfiManagedLongConstants {
            stops_enable: true,
            stop_threshold_futures: 0.1,
            stop_threshold_spot: 0.1,
            system_name_use: "system_v3_2".to_owned(),
            system_v3_2_name: "system_v3_2".to_owned(),
            system_v3_2_stop_threshold_doom_futures: 0.35,
            system_v3_2_stop_threshold_doom_spot: 0.12,
            system_v3_2_stops_enable: false,
            u_e_stops_enable: false,
        },
        programs: BTreeMap::from([
            ("long_exit_signals".to_owned(), first),
            ("long_exit_main".to_owned(), false_program.clone()),
            ("long_exit_williams_r".to_owned(), false_program.clone()),
            ("long_exit_dec".to_owned(), false_program.clone()),
            ("short_exit_signals".to_owned(), false_program.clone()),
            ("short_exit_main".to_owned(), false_program.clone()),
            ("short_exit_williams_r".to_owned(), false_program.clone()),
            ("short_exit_dec".to_owned(), false_program),
            (
                "long_grind_entry_v3".to_owned(),
                nfi_boolean_false_program(),
            ),
        ]),
        feature_projections: OnceLock::new(),
        feature_projection_unions: OnceLock::new(),
    }
}

pub(super) fn enable_test_full_short_manager(manager: &mut NfiX7TradeManager) {
    let routes = vec![
        nfi_managed_route(
            "short_normal",
            NfiManagedLongProfile::Normal,
            "short_normal",
            &["501", "502"],
        ),
        nfi_managed_route(
            "short_pump",
            NfiManagedLongProfile::Pump,
            "short_pump",
            &["521"],
        ),
        nfi_managed_route(
            "short_quick",
            NfiManagedLongProfile::Quick,
            "short_quick",
            &["542"],
        ),
        {
            let mut route = nfi_managed_route(
                "short_rebuy",
                NfiManagedLongProfile::Rebuy,
                "short_rebuy",
                &["561", "562", "563"],
            );
            route.stop_threshold_futures = Some(1.4);
            route.stop_threshold_spot = Some(0.48);
            route
        },
        nfi_managed_route(
            "short_high_profit",
            NfiManagedLongProfile::HighProfit,
            "short_hp",
            &["581"],
        ),
        nfi_managed_route(
            "short_rapid",
            NfiManagedLongProfile::Rapid,
            "short_rapid",
            &["601"],
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
            &["641"],
        ),
    ];
    let regular_tags = routes
        .iter()
        .filter(|route| route.key != "short_rebuy")
        .flat_map(|route| route.entry_tags.clone())
        .collect();
    let mut short_adjustment = manager
        .position_adjustment
        .clone()
        .expect("test manager has source adjustment constants");
    short_adjustment.enabled = true;
    short_adjustment.entry_tags = regular_tags;
    short_adjustment.decision_program = "short_grind_entry_v3".to_owned();

    manager.schema_version = "0.15.0".to_owned();
    manager.short_route_order = routes.iter().map(|route| route.key.clone()).collect();
    manager.managed_short_routes = routes;
    manager.short_rebuy_adjustment.execution_scope = "rebuy-and-grind-v2".to_owned();
    manager.short_rebuy_adjustment.post_derisk_action = "short-position-adjustment".to_owned();
    manager.short_position_adjustment = Some(short_adjustment);
    manager.programs.insert(
        "short_grind_entry_v3".to_owned(),
        nfi_boolean_false_program(),
    );
}

pub(super) fn enable_test_basic_exit_shadow(
    manager: &mut NfiX7TradeManager,
    route_key: &str,
    initial_profit_gate: Option<ManagedExitProfitGate>,
) {
    let route = manager
        .managed_long_routes
        .iter()
        .find(|route| route.key == route_key)
        .expect("test manager has the shadowed route");
    let mut decision_program_order = vec![
        "long_exit_signals".to_owned(),
        "long_exit_main".to_owned(),
        "long_exit_williams_r".to_owned(),
    ];
    if route.profile != NfiManagedLongProfile::HighProfit {
        decision_program_order.push("long_exit_dec".to_owned());
    }
    manager.managed_exit_program = Some(ManagedExitProgram {
        schema_version: "managed-exit-program-v1".to_owned(),
        execution_mode: ManagedExitExecutionMode::Shadow,
        routes: vec![ManagedExitRoute {
            id: route.key.clone(),
            source_order: 0,
            matcher: ManagedExitTagMatcher {
                operator: ManagedExitTagOperator::Any,
                entry_tags: route.entry_tags.clone(),
            },
            initial_profit_gate,
            mode_name: route.mode_name.clone(),
            decision_program_order,
            location: ManagedExitSourceLocation {
                line: 1,
                column: 0,
                end_line: 1,
                end_column: 1,
            },
        }],
        fingerprint: "b".repeat(64),
    });
}

pub(super) fn nfi_adjustment_policy() -> NfiX7AdjustmentPolicy {
    let variable = |name: &str| NfiX7AdjustmentOperand::Variable {
        name: name.to_owned(),
    };
    let feature = |name: &str, multiplier: f64| NfiX7AdjustmentOperand::Feature {
        name: name.to_owned(),
        multiplier,
    };
    let literal = |value| NfiX7AdjustmentOperand::Literal { value };
    let condition = |left, operator, right| NfiX7AdjustmentCondition {
        left,
        operator,
        right,
    };
    let mut fallbacks = (1..=5)
        .map(|level| NfiX7GrindFallbackLevel {
            level,
            predicates: Vec::new(),
        })
        .collect::<Vec<_>>();
    fallbacks[3].predicates = vec![NfiX7AdjustmentPredicate {
        any_derisk_levels: Vec::new(),
        conditions: vec![
            condition(
                variable("slice_profit_entry"),
                NfiX7AdjustmentComparison::Lt,
                literal(-0.06),
            ),
            condition(
                variable("num_open_grinds_and_buybacks"),
                NfiX7AdjustmentComparison::Eq,
                literal(0.0),
            ),
            condition(
                feature("RSI_14", 1.0),
                NfiX7AdjustmentComparison::Lt,
                literal(30.0),
            ),
            condition(
                feature("close", 1.0),
                NfiX7AdjustmentComparison::Lt,
                feature("EMA_20", 0.98),
            ),
        ],
    }];
    fallbacks[4].predicates = vec![NfiX7AdjustmentPredicate {
        any_derisk_levels: vec![1, 2, 3],
        conditions: vec![
            condition(
                variable("slice_profit_entry"),
                NfiX7AdjustmentComparison::Lt,
                literal(-0.06),
            ),
            condition(
                feature("RSI_3", 1.0),
                NfiX7AdjustmentComparison::Gt,
                literal(10.0),
            ),
            condition(
                feature("RSI_3_15m", 1.0),
                NfiX7AdjustmentComparison::Gt,
                literal(20.0),
            ),
            condition(
                feature("AROONU_14", 1.0),
                NfiX7AdjustmentComparison::Lt,
                literal(50.0),
            ),
        ],
    }];
    NfiX7AdjustmentPolicy {
        entry_retry_ms: 5 * 60 * 1_000,
        stale_order_ms: 6 * 60 * 60 * 1_000,
        extra_entry_profit_condition: condition(
            variable("slice_profit"),
            NfiX7AdjustmentComparison::Lt,
            literal(-0.06),
        ),
        extra_entry_derisk_levels: vec![3],
        grind_entry_fallbacks: fallbacks,
    }
}

pub(super) fn enable_nfi_manager(config: &mut PortfolioConfig, manager: NfiX7TradeManager) {
    config.stoploss_ratio = -0.99;
    config.callback_program = Some(CallbackProgram {
        order_filled: Some(OrderFilledProgram {
            initial_successful_entry_writes: vec![CustomDataWrite {
                key: "system_version".to_owned(),
                value: Value::String("system_v3_2".to_owned()),
            }],
            order_tag_actions: BTreeMap::new(),
        }),
    });
    config.nfi_x7_trade_manager = Some(manager);
}

pub(super) fn nfi_pair(
    candles: Vec<Candle>,
    feature_columns: BTreeMap<String, Vec<Value>>,
) -> PairSeries {
    PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: feature_columns
            .into_iter()
            .map(|(name, values)| {
                let encoded = serde_json::to_value(values).expect("test feature values encode");
                let column = serde_json::from_value(encoded)
                    .expect("test feature values form one homogeneous column");
                (name, column)
            })
            .collect(),
        candles: candles.into(),
    }
}
