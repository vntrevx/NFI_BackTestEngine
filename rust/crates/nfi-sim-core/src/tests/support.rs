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
        ("gd1", "dd1", false),
        ("gd2", "dd2", false),
        ("gd3", "dd3", false),
        ("gd4", "dd4", false),
        ("gd5", "dd5", false),
        ("gd6", "dd6", false),
        ("dl1", "ddl1", true),
        ("dl2", "ddl2", true),
    ];
    NfiLegacyGrindConstants {
        max_stake_multiplier: 1.0,
        stake_multipliers_futures: vec![0.2, 0.3, 0.4, 0.5],
        stake_multipliers_spot: vec![0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        derisk_1_reentry_futures: -0.08,
        derisk_1_reentry_spot: -0.08,
        clusters: tags
            .into_iter()
            .map(|(entry_tag, stop_tag, post_derisk)| NfiLegacyGrindCluster {
                entry_tag: entry_tag.to_owned(),
                stop_tag: stop_tag.to_owned(),
                post_derisk,
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

pub(super) fn nfi_legacy_grind_program(
    constants: &NfiLegacyGrindConstants,
) -> CompiledLegacyGrindProgram {
    let location = || ManagedExitSourceLocation {
        line: 1,
        column: 0,
        end_line: 1,
        end_column: 1,
    };
    let known_clusters = constants
        .clusters
        .iter()
        .map(|cluster| CompiledLegacyGrindCluster {
            entry_tag: cluster.entry_tag.clone(),
            stop_tag: cluster.stop_tag.clone(),
            post_derisk: cluster.post_derisk,
        })
        .collect();
    let first_ordinary_tag = constants
        .clusters
        .iter()
        .find(|cluster| !cluster.post_derisk)
        .expect("ordinary Grind cluster")
        .entry_tag
        .clone();
    let mut source_order = vec![CompiledLegacyGrindTransition::FirstEntry {
        profit_tag: "gm0".to_owned(),
        stop_tag: "gmd0".to_owned(),
        append_entry_ids_from: first_ordinary_tag.clone(),
        profit_threshold: 0.018,
        stop_threshold: -0.2,
        location: location(),
    }];
    source_order.extend(
        constants
            .clusters
            .iter()
            .filter(|cluster| cluster.post_derisk)
            .chain(
                constants
                    .clusters
                    .iter()
                    .filter(|cluster| !cluster.post_derisk),
            )
            .map(|cluster| CompiledLegacyGrindTransition::Cluster {
                entry_tag: cluster.entry_tag.clone(),
                stop_tag: cluster.stop_tag.clone(),
                post_derisk: cluster.post_derisk,
                append_entry_ids: true,
                futures_fallback_loss_threshold: (cluster.entry_tag == first_ordinary_tag)
                    .then_some(-0.65),
                location: location(),
            }),
    );
    source_order.push(CompiledLegacyGrindTransition::DeriskBuyback {
        tag: "d1".to_owned(),
        entry_threshold_futures: constants.derisk_1_reentry_futures,
        entry_threshold_spot: constants.derisk_1_reentry_spot,
        entry_feature_columns: vec![
            "global_protections_long_dump".to_owned(),
            "global_protections_long_pump".to_owned(),
        ],
        entry_retry_policy: CompiledLegacyRetryPolicy::BoundedGrindPolicy,
        entry_stake_basis: CompiledLegacyEntryStakeBasis::DeriskExitCost,
        entry_minimum_multiplier: 1.5,
        entry_wallet_guard: CompiledLegacyWalletGuard::ReturnNone,
        exit_threshold_divisor: CompiledLegacyThresholdDivisor::ModeLeverage,
        exit_stake_basis: CompiledLegacyExitStakeBasis::ReentryAmountAtCurrentRate,
        exit_minimum_remaining_multiplier: 1.55,
        location: location(),
    });
    CompiledLegacyGrindProgram {
        schema_version: "grind-transition-program-v3".to_owned(),
        execution_mode: CompiledLegacyGrindExecutionMode::PrimaryWithLegacyShadow,
        source_callback: "long_grind_adjust_trade_position".to_owned(),
        source_order,
        order_scan: CompiledLegacyGrindOrderScan {
            sequence: CompiledOrderSequence::Reverse,
            entry_order_side: CompiledOrderSide::Buy,
            exit_order_side: CompiledOrderSide::Sell,
            exclude_first_entry: true,
            known_clusters,
            level_one_entry_excluded_tags: [
                "r", "d1", "dl1", "ddl1", "dl2", "ddl2", "gd2", "dd2", "gd3", "dd3", "gd4", "dd4",
                "gd5", "dd5", "gd6", "dd6", "gm0", "gmd0", "gdr",
            ]
            .into_iter()
            .map(ToOwned::to_owned)
            .collect(),
            level_one_exit_excluded_tags: [
                "dl1", "ddl1", "dl2", "ddl2", "gd2", "dd2", "gd3", "dd3", "gd4", "dd4", "gd5",
                "dd5", "gd6", "dd6", "gm0", "gmd0", "gdr",
            ]
            .into_iter()
            .map(ToOwned::to_owned)
            .collect(),
            close_all_exit_tags: ["p", "r", "d", "dd0", "partial_exit", "force_exit", ""]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
            first_entry_closed_tags: ["gm0", "gmd0"].into_iter().map(ToOwned::to_owned).collect(),
            derisk_entry_tag: "d1".to_owned(),
            partial_fill_policy: CompiledPartialFillPolicy::FilledOrdersHaveZeroRemaining,
        },
        policy: CompiledLegacyGrindPolicy {
            entry_retry_ms: 10 * 60 * 1_000,
            order_age_ms: 6 * 60 * 60 * 1_000,
            force_order_age_ms: 24 * 60 * 60 * 1_000,
            forced_entry_loss_gate: -0.06,
            minimum_entry_multiplier: 1.5,
            minimum_remaining_multiplier: 1.55,
            derisk_amount_ratio: 0.95,
        },
        location: location(),
        fingerprint: "d".repeat(64),
    }
}

pub(super) fn nfi_legacy_stateful_input_contract() -> serde_json::Value {
    serde_json::json!({
        "indexed_fields": {
            "last_candle": [
                "global_protections_long_dump",
                "global_protections_long_pump"
            ],
            "previous_candle": []
        }
    })
}

pub(super) fn enable_test_compiled_legacy_grind(
    manager: &mut NfiX7TradeManager,
    constants: NfiLegacyGrindConstants,
) {
    let program = nfi_legacy_grind_program(&constants);
    manager
        .programs
        .insert("long_grind_entry_v3".to_owned(), nfi_boolean_true_program());
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
    });
    manager.route_order.insert(6, "long_grind".to_owned());
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
        program: None,
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
        managed_short_exit_program: None,
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
            program: None,
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
            program: None,
        },
        position_adjustment: Some(NfiX7PositionAdjustment {
            enabled: false,
            entry_tags: adjustment_tags,
            system_version: "system_v3_2".to_owned(),
            source_callback: None,
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
            program: None,
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
                operands: Vec::new(),
            },
            initial_profit_gate,
            profit_basis: if route.profile == NfiManagedLongProfile::Rebuy {
                ManagedExitProfitBasis::CurrentStake
            } else {
                ManagedExitProfitBasis::InitialStake
            },
            mode_name: route.mode_name.clone(),
            decision_program_order,
            state_program: Some(test_managed_exit_state(route, "long_exit_stoploss")),
            terminal_exit: route.terminal_exit.clone(),
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

fn test_managed_exit_state(
    route: &NfiManagedLongRoute,
    source_helper: &str,
) -> ManagedExitStateProgram {
    let special_stop = matches!(
        route.profile,
        NfiManagedLongProfile::Rebuy | NfiManagedLongProfile::Rapid | NfiManagedLongProfile::Scalp
    );
    ManagedExitStateProgram {
        stateful_order: vec![
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
        inline_exit: None,
        stop: if special_stop {
            ManagedExitStopPolicy::StakeThreshold {
                enabled: true,
                futures_threshold: route.stop_threshold_futures.unwrap_or(0.35),
                spot_threshold: route.stop_threshold_spot.unwrap_or(0.12),
                divide_by_leverage: true,
            }
        } else {
            ManagedExitStopPolicy::SourceHelper {
                helper: source_helper.to_owned(),
            }
        },
        target: test_managed_exit_target(route),
    }
}

fn test_managed_exit_target(route: &NfiManagedLongRoute) -> ManagedExitTargetPolicy {
    ManagedExitTargetPolicy {
        u_e_raise_delta: if matches!(
            route.profile,
            NfiManagedLongProfile::Normal
                | NfiManagedLongProfile::Pump
                | NfiManagedLongProfile::TopCoins
                | NfiManagedLongProfile::Scalp
        ) {
            0.005
        } else {
            0.001
        },
        profit_raise_delta: 0.001,
        max_target_floor: if route.profile == NfiManagedLongProfile::HighProfit {
            0.03
        } else {
            0.005
        },
        protected_reentry_guard: matches!(
            route.profile,
            NfiManagedLongProfile::Normal
                | NfiManagedLongProfile::Quick
                | NfiManagedLongProfile::Rapid
                | NfiManagedLongProfile::TopCoins
        ),
        suppress_protected_exit: route.profile != NfiManagedLongProfile::HighProfit,
        pure_scalp_trailing: route.profile == NfiManagedLongProfile::Scalp,
        pure_scalp_matcher: (route.profile == NfiManagedLongProfile::Scalp).then(|| {
            ManagedExitTagMatcher {
                operator: ManagedExitTagOperator::All,
                entry_tags: route.entry_tags.clone(),
                operands: Vec::new(),
            }
        }),
    }
}

pub(super) fn enable_test_short_exit_shadow(manager: &mut NfiX7TradeManager) {
    let known_explicit_tags = manager
        .managed_short_routes
        .iter()
        .filter(|route| route.key != "short_top_coins_fallback")
        .flat_map(|route| route.entry_tags.clone())
        .chain(["620".to_owned()])
        .collect::<Vec<_>>();
    let rebuy_tags = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")
        .expect("test manager has short rebuy")
        .entry_tags
        .clone();
    let routes = manager
        .managed_short_routes
        .iter()
        .enumerate()
        .map(|(source_order, route)| ManagedExitRoute {
            id: route.key.clone(),
            source_order,
            matcher: test_short_exit_matcher(route, &known_explicit_tags, &rebuy_tags),
            initial_profit_gate: matches!(
                route.profile,
                NfiManagedLongProfile::Normal
                    | NfiManagedLongProfile::Pump
                    | NfiManagedLongProfile::Quick
                    | NfiManagedLongProfile::Rapid
            )
            .then_some(ManagedExitProfitGate {
                operator: ManagedExitComparison::GreaterThan,
                value: 0.0,
            }),
            profit_basis: if route.profile == NfiManagedLongProfile::Rebuy {
                ManagedExitProfitBasis::CurrentStake
            } else {
                ManagedExitProfitBasis::InitialStake
            },
            mode_name: route.mode_name.clone(),
            decision_program_order: test_short_program_order(route.profile),
            state_program: Some(test_managed_exit_state(route, "short_exit_stoploss")),
            terminal_exit: None,
            location: ManagedExitSourceLocation {
                line: source_order + 1,
                column: 0,
                end_line: source_order + 1,
                end_column: 1,
            },
        })
        .collect();
    manager.managed_short_exit_program = Some(ManagedExitProgram {
        schema_version: "managed-exit-program-v1".to_owned(),
        execution_mode: ManagedExitExecutionMode::Shadow,
        routes,
        fingerprint: "c".repeat(64),
    });
}

fn test_short_program_order(profile: NfiManagedLongProfile) -> Vec<String> {
    let mut order = [
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
    ]
    .iter()
    .map(ToString::to_string)
    .collect::<Vec<_>>();
    if profile != NfiManagedLongProfile::HighProfit {
        order.push("short_exit_dec".to_owned());
    }
    order
}

fn test_short_exit_matcher(
    route: &NfiManagedLongRoute,
    known_explicit_tags: &[String],
    rebuy_tags: &[String],
) -> ManagedExitTagMatcher {
    match route.key.as_str() {
        "short_rebuy" => test_tag_matcher(ManagedExitTagOperator::All, &route.entry_tags),
        "short_scalp" => test_short_scalp_matcher(route, rebuy_tags),
        "short_top_coins_fallback" => ManagedExitTagMatcher {
            operator: ManagedExitTagOperator::AllOf,
            entry_tags: Vec::new(),
            operands: vec![
                ManagedExitTagMatcher {
                    operator: ManagedExitTagOperator::IsShort,
                    entry_tags: Vec::new(),
                    operands: Vec::new(),
                },
                ManagedExitTagMatcher {
                    operator: ManagedExitTagOperator::Not,
                    entry_tags: Vec::new(),
                    operands: vec![test_tag_matcher(
                        ManagedExitTagOperator::Any,
                        known_explicit_tags,
                    )],
                },
            ],
        },
        _ => test_tag_matcher(ManagedExitTagOperator::Any, &route.entry_tags),
    }
}

fn test_short_scalp_matcher(
    route: &NfiManagedLongRoute,
    rebuy_tags: &[String],
) -> ManagedExitTagMatcher {
    let compound_tags = route
        .entry_tags
        .iter()
        .cloned()
        .chain(rebuy_tags.iter().cloned())
        .chain(["620".to_owned()])
        .collect::<Vec<_>>();
    ManagedExitTagMatcher {
        operator: ManagedExitTagOperator::AnyOf,
        entry_tags: Vec::new(),
        operands: vec![
            test_tag_matcher(ManagedExitTagOperator::All, &route.entry_tags),
            ManagedExitTagMatcher {
                operator: ManagedExitTagOperator::AllOf,
                entry_tags: Vec::new(),
                operands: vec![
                    test_tag_matcher(ManagedExitTagOperator::Any, &route.entry_tags),
                    test_tag_matcher(ManagedExitTagOperator::All, &compound_tags),
                ],
            },
        ],
    }
}

fn test_tag_matcher(
    operator: ManagedExitTagOperator,
    entry_tags: &[String],
) -> ManagedExitTagMatcher {
    ManagedExitTagMatcher {
        operator,
        entry_tags: entry_tags.to_vec(),
        operands: Vec::new(),
    }
}

pub(super) fn enable_test_quick_inline_shadow(manager: &mut NfiX7TradeManager) {
    enable_test_basic_exit_shadow(
        manager,
        "long_quick",
        Some(ManagedExitProfitGate {
            operator: ManagedExitComparison::GreaterThan,
            value: 0.0,
        }),
    );
    let route = &mut manager
        .managed_exit_program
        .as_mut()
        .expect("shadow program")
        .routes[0];
    let state = route.state_program.as_mut().expect("shadow state program");
    state.stateful_order = vec![
        ManagedExitStateOperation::Stop,
        ManagedExitStateOperation::InlineExit,
        ManagedExitStateOperation::ExistingTarget,
        ManagedExitStateOperation::TargetUpdate,
        ManagedExitStateOperation::FinalFilter,
        ManagedExitStateOperation::TerminalExit,
    ];
    state.inline_exit = Some(ManagedExitInlineExit {
        position: ManagedExitInlinePosition::AfterStop,
        minimum_profit: 0.02,
        minimum_inclusive: false,
        maximum_profit: 0.09,
        maximum_inclusive: true,
        program: serde_json::from_value(serde_json::json!({
            "schema_version": "1.2.0",
            "opcode": "scalar-decision-program-v1",
            "parameters": ["mode_name", "profit_init_ratio", "last_candle"],
            "expressions": [
                ["variable", "last_candle"],
                ["literal", "RSI_14"],
                ["index", 0, 1],
                ["literal", 78.0],
                ["compare", 2, [["greater", 3]]],
                ["literal", true],
                ["variable", "mode_name"],
                ["format", [["text", "exit_"], ["value", 6], ["text", "_q_1"]]],
                ["tuple", [5, 7]],
                ["literal", false],
                ["literal", null],
                ["tuple", [9, 10]]
            ],
            "statements": [
                ["if", 4, [["return", 8]], []],
                ["return", 11]
            ]
        }))
        .expect("valid inline scalar program"),
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
