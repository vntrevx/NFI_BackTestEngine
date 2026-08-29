//! Current-source long Grind liquidation-rescue regressions.

use std::sync::OnceLock;

use super::*;
use crate::portfolio::OpenTrade;

#[test]
fn system_adjustment_validator_accepts_level_bound_derisk_inputs() {
    // Given: the four source-compiled derisk inputs that index level 1.
    let derisk_levels = [1, 2];
    let grind_levels = [1, 2, 3, 4, 5, 6];

    // When/Then: validation preserves the level consumed by the runtime.
    for kind in [
        CompiledSystemAdjustmentInputKind::DeriskFound,
        CompiledSystemAdjustmentInputKind::DeriskEnabled,
        CompiledSystemAdjustmentInputKind::DeriskStake,
        CompiledSystemAdjustmentInputKind::DeriskThreshold,
    ] {
        assert!(crate::validation::valid_system_adjustment_binding_level(
            kind,
            Some(1),
            &derisk_levels,
            &grind_levels,
        ));
        assert!(!crate::validation::valid_system_adjustment_binding_level(
            kind,
            None,
            &derisk_levels,
            &grind_levels,
        ));
    }
}

#[test]
fn current_long_grind_liquidation_rescue_is_one_shot() {
    // Given: a long Futures trade beyond the current GD5 loss threshold but
    // still above its explicit liquidation price.
    const MINUTE: i64 = 60 * 1_000;
    let first_rescue = candle(11 * MINUTE, 85.0, 85.0);
    let second_rescue = candle(22 * MINUTE, 85.0, 85.0);
    let mut pair = nfi_pair(
        vec![first_rescue.clone(), second_rescue.clone()],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);
    let mut manager_config = config(1);
    manager_config.is_futures = true;

    let (manager, route) = current_rescue_contract(-0.12);
    let mut trade = long_futures_trade();

    // When: both consecutive callbacks satisfy every market boundary.
    let first = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager,
        &route,
        &mut trade,
        &pair,
        0,
        &first_rescue,
        &manager_config,
        900.0,
    )
    .expect("current GD5 policy is valid")
    .expect("first callback emits the rescue");
    let second = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager,
        &route,
        &mut trade,
        &pair,
        1,
        &second_rescue,
        &manager_config,
        900.0,
    )
    .expect("repeated callback remains valid");

    // Then: the source-defined custom-data guard permits exactly one rescue.
    assert_eq!(first.tag, "gd5");
    assert!(first.stake_amount > 0.0);
    assert!(second.is_none());
    assert_eq!(
        trade
            .custom_data
            .get("gd5_liquidation_rescue_used")
            .and_then(serde_json::Value::as_bool),
        Some(true)
    );
}

#[test]
fn liquidation_rescue_wallet_rejection_consumes_the_one_shot() {
    // Given: the same reached rescue boundary with no available wallet.
    const MINUTE: i64 = 60 * 1_000;
    let first_rescue = candle(11 * MINUTE, 85.0, 85.0);
    let second_rescue = candle(22 * MINUTE, 85.0, 85.0);
    let mut pair = nfi_pair(
        vec![first_rescue.clone(), second_rescue.clone()],
        BTreeMap::new(),
    );
    pair.minimum_cost = Some(5.0);
    let mut config = config(1);
    config.is_futures = true;
    let (manager, route) = current_rescue_contract(-0.12);
    let mut trade = long_futures_trade();

    // When: the first callback reaches GD5 but its requested stake cannot fit.
    let first = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager,
        &route,
        &mut trade,
        &pair,
        0,
        &first_rescue,
        &config,
        0.0,
    )
    .expect("wallet rejection is a supported callback no-op");
    let second = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager,
        &route,
        &mut trade,
        &pair,
        1,
        &second_rescue,
        &config,
        900.0,
    )
    .expect("later callback remains supported");

    // Then: source order consumes the custom-data guard before returning None.
    assert!(first.is_none());
    assert!(second.is_none());
    assert_eq!(
        trade
            .custom_data
            .get("gd5_liquidation_rescue_used")
            .and_then(serde_json::Value::as_bool),
        Some(true)
    );
}

#[test]
fn liquidation_rescue_loss_threshold_mutation_is_detected() {
    // Given: the same reached candle with a stricter mutated source threshold.
    const MINUTE: i64 = 60 * 1_000;
    let rescue = candle(11 * MINUTE, 85.0, 85.0);
    let mut pair = nfi_pair(vec![rescue.clone()], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let mut config = config(1);
    config.is_futures = true;
    let (manager, route) = current_rescue_contract(-0.16);
    let mut trade = long_futures_trade();

    // When: Native evaluates the mutated compiled comparator.
    let outcome = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager, &route, &mut trade, &pair, 0, &rescue, &config, 900.0,
    )
    .expect("mutated policy remains structurally valid");

    // Then: the fixture detects the changed source term as a missing rescue.
    assert!(outcome.is_none());
    assert!(!trade
        .custom_data
        .contains_key("gd5_liquidation_rescue_used"));
}

pub(super) fn current_rescue_contract(
    loss_threshold: f64,
) -> (NfiX7TradeManager, NfiLongGrindRoute) {
    let mut manager = nfi_top_coins_manager(nfi_false_program());
    manager.programs.insert(
        "long_grind_entry_v3".to_owned(),
        nfi_boolean_false_program(),
    );
    let constants = nfi_legacy_grind_constants();
    let mut program = nfi_legacy_grind_program(&constants);
    program.execution_mode = CompiledLegacyGrindExecutionMode::Primary;
    let transition = program
        .source_order
        .iter_mut()
        .find(|transition| {
            matches!(
                transition,
                CompiledLegacyGrindTransition::Cluster { entry_tag, .. }
                    if entry_tag == "gd5"
            )
        })
        .expect("test program has GD5");
    let CompiledLegacyGrindTransition::Cluster {
        liquidation_rescue, ..
    } = transition
    else {
        panic!("GD5 is a cluster");
    };
    *liquidation_rescue = Some(NfiLegacyLiquidationRescuePolicy {
        side: CompiledLegacyGrindSide::Long,
        cluster_level: 5,
        loss_threshold,
        profit_comparison: CompiledLegacyComparison::LessThan,
        liquidation_multiplier: 1.2,
        liquidation_comparison: CompiledLegacyComparison::LessThan,
        used_state_key: "gd5_liquidation_rescue_used".to_owned(),
    });
    let route = NfiLongGrindRoute {
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
    };
    (manager, route)
}

pub(super) fn long_futures_trade() -> OpenTrade {
    OpenTrade {
        id: 1,
        pair_index: 0,
        pair: "TEST/USDT".to_owned(),
        side: TradeSide::Long,
        leverage: 3.0,
        amount_step: 0.001,
        price_step: 0.01,
        open_timestamp_ms: 0,
        open_rate: 100.0,
        amount: 1.0,
        stake_amount: 100.0,
        max_stake_amount: 100.0,
        entry_cost_with_fees: 100.1,
        first_entry_cost_with_fees: 100.1,
        adjustment_count: 0,
        entry_tag: Some("120 ".to_owned()),
        entry_tag_cache: OnceLock::new(),
        funding_fees: 0.0,
        funding_fees_total: 0.0,
        funding_sum_high: 0.0,
        funding_sum_low: 0.0,
        funding_rebase_seed: None,
        realized_partial_profit: 0.0,
        liquidation_price: Some(80.0),
        liquidation_price_is_explicit: true,
        initial_stop_loss: 1.0,
        stop_loss: 1.0,
        custom_stop_loss_ratio: None,
        minimum_rate: 85.0,
        maximum_rate: 100.0,
        orders: vec![FilledOrder {
            id: 1,
            funding_fee: 0.0,
            sequence: 0,
            side: OrderSide::Buy,
            is_entry: true,
            filled_timestamp_ms: 0,
            amount: 1.0,
            price: 100.0,
            cost: 100.0,
            tag: Some("120 ".to_owned()),
        }],
        filled_order_aggregates: OnceLock::new(),
        custom_data: BTreeMap::new(),
        nfi_adjustment_state: None,
    }
}
