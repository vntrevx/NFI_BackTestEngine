//! Source slot limits, exact official wallet divisors, and scalp confirmations.

use super::*;
use crate::calculations::{source_trade_slot_limit, unlimited_entry_stake};

fn official() -> Value {
    serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-slot-settings-2026-09-08/official-results.json"
    ))
    .unwrap()
}

#[test]
fn unlimited_stake_divisors_match_official_boundaries() {
    for case in official()["wallet"].as_array().unwrap() {
        let raw = case["raw_slots"].as_f64().unwrap();
        let slots = if raw.to_bits() == (-1.0_f64).to_bits() {
            f64::INFINITY
        } else {
            raw
        };
        let result = unlimited_entry_stake(
            case["available"].as_f64().unwrap(),
            case["tied"].as_f64().unwrap(),
            slots,
        );
        assert_eq!(
            result.to_bits(),
            case["proposed"].as_f64().unwrap().to_bits(),
            "{case}"
        );
    }
}

#[test]
fn source_scalp_programs_match_official_slot_guards() {
    let compiled: Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-slot-settings-2026-09-08/scalp-programs.json"
    ))
    .unwrap();
    let (_, seed) = super::callbacks::order_filled_fixture();
    let mut checked = 0;
    for case in official()["scalp"].as_array().unwrap() {
        let Some(raw) = case["raw_slots"].as_i64() else {
            continue;
        };
        let count = usize::try_from(case["open_trades"].as_u64().unwrap()).unwrap();
        let opened = vec![seed.clone(); count];
        let minimum = case["minimum_free"].as_u64().unwrap().to_string();
        let program: ConfirmProgram =
            serde_json::from_value(compiled["programs"][minimum].clone()).unwrap();
        let result = evaluate_confirm_program(
            &program,
            ConfirmInputs {
                pair: "BTC/USDT",
                timestamp_ms: 1,
                amount: 1.0,
                rate: 100.0,
                entry_tag: Some("scalp"),
                side: TradeSide::Long,
                previous_close: Some(100.0),
                open_trades: &opened,
                max_open_trades: usize::try_from(raw).unwrap_or(2).max(1),
                source_max_open_trades: (raw <= 0).then_some(raw),
                is_futures: false,
                order_type: OrderType::Limit,
            },
        );
        assert_eq!(result, case["allowed"].as_bool(), "{case}");
        checked += 1;
    }
    assert_eq!(checked, 180);
}

#[test]
fn zero_slot_wallet_proposal_still_runs_stake_callback() {
    let mut settings = config(1);
    settings.unlimited_stake = true;
    settings.strategy_wallet_policy = Some(StrategyWalletPolicy {
        entry_minimum_stoploss_ratio: None,
        source_max_open_trades: Some(0),
        amend_last_stake_amount: false,
        last_stake_amount_min_ratio: 0.5,
        available_capital: None,
        initial_asset_balances: BTreeMap::new(),
    });
    settings.stake_program = Some(serde_json::from_value(serde_json::json!({
        "statements": [{"op":"if", "condition":{"op":"equal", "left":{"op":"variable","name":"proposed_stake"}, "right":{"op":"literal","value":0.0}},
            "then":[{"op":"return","value":{"op":"literal","value":10.0}}],"otherwise":[]},
            {"op":"return","value":{"op":"literal","value":0.0}}]
    })).unwrap());
    let mut opening = candle(1, 100.0, 100.0);
    opening.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: settings,
        pairs: vec![nfi_pair(
            vec![opening, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    };
    let result = simulate(&input).unwrap();
    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].stake_amount, 10.0);
    // Worker calibration can sample fewer pairs while retaining portfolio capacity.
    input.config.max_open_trades = 2;
    assert_eq!(simulate(&input).unwrap().trades.len(), 1);
    input
        .config
        .strategy_wallet_policy
        .as_mut()
        .unwrap()
        .source_max_open_trades = Some(-1);
    assert!(source_trade_slot_limit(&input.config).is_infinite());
    assert!(simulate(&input).is_err());
}

#[test]
fn initial_minimum_reserve_matches_official_zero_slot_entry_and_preserves_archives() {
    let mut settings = config(1);
    settings.stoploss_ratio = -0.99;
    settings.stake_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{"op":"return", "value":{"op":"variable","name":"min_stake"}}]
        }))
        .unwrap(),
    );
    settings.strategy_wallet_policy = Some(StrategyWalletPolicy {
        entry_minimum_stoploss_ratio: Some(-0.05),
        source_max_open_trades: Some(0),
        amend_last_stake_amount: false,
        last_stake_amount_min_ratio: 0.5,
        available_capital: None,
        initial_asset_balances: BTreeMap::new(),
    });
    settings.unlimited_stake = true;
    let mut opening = candle(1, 0.5566, 0.5566);
    opening.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut pair = nfi_pair(vec![opening, candle(2, 0.5566, 0.5566)], BTreeMap::new());
    pair.minimum_cost = Some(5.0);
    pair.amount_step = Some(0.1);
    pair.price_step = Some(0.0001);
    let mut input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: settings,
        pairs: vec![pair],
    };
    // Captured official first fill: 9.9 at 0.5566, stake 5.51034. Using the
    // strategy stoploss incorrectly raises the reserve and fills 13.4 instead.
    let official = simulate(&input).unwrap();
    assert_eq!(official.trades.len(), 1);
    assert_eq!(official.trades[0].orders[0].amount, 9.9);
    input
        .config
        .strategy_wallet_policy
        .as_mut()
        .unwrap()
        .entry_minimum_stoploss_ratio = None;
    let archived = simulate(&input).unwrap();
    assert_eq!(archived.trades[0].orders[0].amount, 13.4);
    for reserve in [f64::NAN, f64::INFINITY, -1.01, 0.01] {
        input
            .config
            .strategy_wallet_policy
            .as_mut()
            .unwrap()
            .entry_minimum_stoploss_ratio = Some(reserve);
        assert!(simulate(&input).is_err());
    }
}
