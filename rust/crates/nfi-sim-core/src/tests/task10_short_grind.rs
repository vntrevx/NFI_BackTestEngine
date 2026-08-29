//! Current-source short Grind liquidation-rescue regressions.

use super::task9_long_grind::{current_rescue_contract, long_futures_trade};
use super::*;
use crate::nfi::PositionAdjustmentRequest;
use crate::portfolio::OpenTrade;

fn short_rescue_contract() -> (NfiX7TradeManager, NfiLongGrindRoute) {
    let (mut manager, mut route) = current_rescue_contract(0.12);
    manager.programs.remove("long_grind_entry_v3");
    manager.programs.insert(
        "short_grind_entry_v3".to_owned(),
        nfi_boolean_false_program(),
    );
    route.mode_name = "short_grind".to_owned();
    route.entry_tags = vec!["620".to_owned()];
    route.decision_program = "short_grind_entry_v3".to_owned();
    route.futures_fallback_loss_threshold = Some(0.65);
    let program = route.program.as_mut().expect("compiled short route");
    program.source_callback = "short_grind_adjust_trade_position".to_owned();
    program.side = CompiledLegacyGrindSide::Short;
    program.order_scan.entry_order_side = CompiledOrderSide::Sell;
    program.order_scan.exit_order_side = CompiledOrderSide::Buy;
    program.policy.forced_entry_loss_gate = 0.06;
    for transition in &mut program.source_order {
        if let CompiledLegacyGrindTransition::Cluster {
            futures_fallback_loss_threshold: Some(threshold),
            ..
        } = transition
        {
            *threshold = threshold.abs();
        }
    }
    let rescue = program
        .source_order
        .iter_mut()
        .find_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                liquidation_rescue,
                ..
            } if entry_tag == "gd5" => liquidation_rescue.as_mut(),
            _ => None,
        })
        .expect("short GD5 rescue");
    rescue.side = CompiledLegacyGrindSide::Short;
    rescue.loss_threshold = 0.12;
    rescue.profit_comparison = CompiledLegacyComparison::GreaterThan;
    rescue.liquidation_multiplier = 0.8;
    rescue.liquidation_comparison = CompiledLegacyComparison::GreaterThan;
    manager.short_grind = Some(route.clone());
    (manager, route)
}

fn short_futures_trade(liquidation_price: f64) -> OpenTrade {
    let mut trade = long_futures_trade();
    trade.side = TradeSide::Short;
    trade.entry_tag = Some("620 ".to_owned());
    trade.entry_tag_cache = std::sync::OnceLock::new();
    trade.liquidation_price = Some(liquidation_price);
    trade.minimum_rate = 100.0;
    trade.maximum_rate = 115.0;
    trade.orders[0].side = OrderSide::Sell;
    trade.orders[0].tag = Some("620 ".to_owned());
    trade.orders.push(FilledOrder {
        id: 2,
        funding_fee: 0.0,
        sequence: 1,
        side: OrderSide::Buy,
        is_entry: false,
        filled_timestamp_ms: 60 * 1_000,
        amount: 0.1,
        price: 110.0,
        cost: 11.0,
        tag: Some("gm0".to_owned()),
    });
    trade
}

fn reached_short_rescue(
    rate: f64,
    liquidation_price: f64,
    available_balance: f64,
) -> (Option<AdjustmentSignal>, OpenTrade) {
    const HOUR: i64 = 60 * 60 * 1_000;
    let reached = candle(25 * HOUR, rate, rate);
    let mut pair = nfi_pair(vec![reached.clone()], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    let (manager, route) = short_rescue_contract();
    let mut trade = short_futures_trade(liquidation_price);
    let signal = crate::nfi::evaluate_nfi_legacy_grind_adjustment(
        &manager,
        &route,
        &mut trade,
        &pair,
        0,
        &reached,
        &manager_config,
        available_balance,
    )
    .expect("short source contract is supported");
    (signal, trade)
}

#[test]
fn current_short_grind_liquidation_rescue_is_one_shot() {
    const HOUR: i64 = 60 * 60 * 1_000;
    // Given/When: short loss and liquidation proximity satisfy strict source predicates.
    let (first, mut trade) = reached_short_rescue(115.0, 120.0, 900.0);

    // Then: Native emits the positive short-entry stake and consumes the key.
    let first = first.expect("short GD5 rescue");
    assert_eq!(first.tag, "gd5");
    assert!(first.stake_amount > 0.0);
    assert_eq!(
        trade
            .custom_data
            .get("gd5_liquidation_rescue_used")
            .and_then(serde_json::Value::as_bool),
        Some(true)
    );

    // And: a second otherwise-eligible callback is an exact no-op.
    let second = candle(50 * HOUR, 115.0, 115.0);
    let mut pair = nfi_pair(vec![second.clone()], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    let (manager, route) = short_rescue_contract();
    assert!(matches!(
        crate::nfi::evaluate_nfi_legacy_grind_adjustment(
            &manager,
            &route,
            &mut trade,
            &pair,
            0,
            &second,
            &manager_config,
            900.0,
        ),
        Some(None)
    ));
}

#[test]
fn tag_620_dispatches_to_the_legacy_short_route() {
    // Given: a reached tag-620 trade and the source-derived manager route.
    const HOUR: i64 = 60 * 60 * 1_000;
    let reached = candle(25 * HOUR, 115.0, 115.0);
    let mut pair = nfi_pair(vec![reached.clone()], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    let mut manager_config = config(1);
    manager_config.is_futures = true;
    let (manager, _) = short_rescue_contract();
    let mut trade = short_futures_trade(120.0);
    let request = PositionAdjustmentRequest {
        pair: &pair,
        candle_index: 0,
        candle: &reached,
        config: &manager_config,
        available_balance: 900.0,
    };

    // When: the public manager dispatcher evaluates the callback.
    let signal = crate::nfi::evaluate_nfi_position_adjustment(&manager, &mut trade, &request)
        .expect("valid short route")
        .expect("manager handles tag 620")
        .expect("reached GD5 action");

    // Then: unmatched-short no-op cannot mask the changed branch.
    assert_eq!(signal.tag, "gd5");
    assert!(signal.stake_amount > 0.0);
}

#[test]
fn short_rescue_strict_boundaries_and_wallet_order_are_exact() {
    // Profit equality and liquidation-boundary equality are both excluded.
    assert!(reached_short_rescue(112.0, 130.0, 900.0).0.is_none());
    assert!(reached_short_rescue(115.0, 143.75, 900.0).0.is_none());

    // Just inside both strict predicates reaches the action.
    assert!(reached_short_rescue(115.01, 143.75, 900.0).0.is_some());

    // The source setter precedes wallet rejection, so a rejected stake consumes the key.
    let (signal, trade) = reached_short_rescue(115.0, 120.0, 0.0);
    assert!(signal.is_none());
    assert!(trade
        .custom_data
        .contains_key("gd5_liquidation_rescue_used"));
}

#[test]
fn schema_030_short_route_accepts_optional_source_defined_rescue() {
    // Given: the valid source-bound schema-0.30 short route.
    let (_, route) = short_rescue_contract();
    assert!(crate::validation::valid_versioned_legacy_grind_program(
        "0.30.0", &route,
    ));

    // When/Then: a source version without a rescue remains valid.
    let mut missing = route.clone();
    for transition in &mut missing
        .program
        .as_mut()
        .expect("compiled route")
        .source_order
    {
        if let CompiledLegacyGrindTransition::Cluster {
            liquidation_rescue, ..
        } = transition
        {
            *liquidation_rescue = None;
        }
    }
    assert!(crate::validation::valid_versioned_legacy_grind_program(
        "0.30.0", &missing,
    ));

    // And: changing its declared cluster or moving it off the fifth ordinary cluster fails.
    let mut wrong_level = route.clone();
    let wrong_level_policy = wrong_level
        .program
        .as_mut()
        .expect("compiled route")
        .source_order
        .iter_mut()
        .find_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                liquidation_rescue: Some(policy),
                ..
            } => Some(policy),
            _ => None,
        })
        .expect("short rescue");
    wrong_level_policy.cluster_level = 4;
    assert!(!crate::validation::valid_versioned_legacy_grind_program(
        "0.30.0",
        &wrong_level,
    ));

    let mut moved = route.clone();
    let source_order = &mut moved.program.as_mut().expect("compiled route").source_order;
    let policy = source_order
        .iter_mut()
        .find_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                liquidation_rescue,
                ..
            } if entry_tag == "gd5" => liquidation_rescue.take(),
            _ => None,
        })
        .expect("short rescue");
    let target = source_order
        .iter_mut()
        .find_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                liquidation_rescue,
                ..
            } if entry_tag == "gd4" => Some(liquidation_rescue),
            _ => None,
        })
        .expect("GD4 transition");
    *target = Some(policy);
    assert!(!crate::validation::valid_versioned_legacy_grind_program(
        "0.30.0", &moved,
    ));

    let moved_policy = moved
        .program
        .as_mut()
        .expect("compiled route")
        .source_order
        .iter_mut()
        .find_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                liquidation_rescue: Some(policy),
                ..
            } if entry_tag == "gd4" => Some(policy),
            _ => None,
        })
        .expect("moved short rescue");
    moved_policy.cluster_level = 4;
    assert!(crate::validation::valid_versioned_legacy_grind_program(
        "0.30.0", &moved,
    ));

    // The sealed schema-0.29 long route remains accepted.
    let (_, long_route) = current_rescue_contract(-0.12);
    assert!(crate::validation::valid_versioned_legacy_grind_program(
        "0.29.0",
        &long_route,
    ));
}
