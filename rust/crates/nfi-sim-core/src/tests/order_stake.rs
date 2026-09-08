//! Exchange bounds captured from the pinned official backtester.

use super::*;
use crate::execution::order_stake::{additional_entry_maximum_stake, maximum_pair_stake};
use crate::execution::validate_stake_amount;

fn policy(pair: &str, amount: Option<f64>, cost: Option<f64>) -> OrderStakePolicy {
    OrderStakePolicy {
        pair_limits: BTreeMap::from([(
            pair.to_owned(),
            PairStakeLimits {
                maximum_amount: amount,
                maximum_cost: cost,
            },
        )]),
    }
}

fn official() -> Value {
    serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-execution-settings-2026-09-08/official-bounds.json"
    ))
    .unwrap()
}

#[test]
fn exchange_maximum_matches_all_official_boundaries() {
    let pair = nfi_pair(vec![], BTreeMap::new());
    for case in official()["maxima"].as_array().unwrap() {
        let mut settings = config(1);
        settings.is_futures = case["mode"] == "futures";
        let size = if settings.is_futures {
            case["contract_size"].as_f64().unwrap()
        } else {
            1.0
        };
        settings.order_stake_policy = Some(policy(
            &pair.pair,
            case["amount_max"].as_f64().map(|v| v * size),
            case["cost_max"].as_f64().map(|v| v * size),
        ));
        if case["tiered"] == true {
            settings.liquidation_model = Some(isolated_model(
                &pair.pair,
                vec![
                    leverage_tier(0.0, Some(1000.0), 20.0, 0.01, 0.0),
                    leverage_tier(1000.0, Some(5000.0), 10.0, 0.02, 0.0),
                    leverage_tier(5000.0, Some(25000.0), 2.0, 0.03, 0.0),
                ],
            ));
        }
        let result = maximum_pair_stake(
            &pair,
            case["rate"].as_f64().unwrap(),
            case["leverage"].as_f64().unwrap(),
            &settings,
        )
        .unwrap();
        let expected = case["maximum"].as_f64().unwrap_or(f64::INFINITY);
        assert_eq!(result.to_bits(), expected.to_bits(), "{case}");
    }
}

#[test]
fn additional_entry_validation_matches_all_official_wallet_boundaries() {
    let pair = nfi_pair(vec![], BTreeMap::new());
    for case in official()["wallets"].as_array().unwrap() {
        let mut settings = config(1);
        settings.order_stake_policy = Some(policy(&pair.pair, None, case["maximum"].as_f64()));
        let maximum = additional_entry_maximum_stake(
            &pair,
            100.0,
            1.0,
            case["available"].as_f64().unwrap(),
            case["existing"].as_f64().unwrap_or(0.0),
            &settings,
        )
        .unwrap();
        let result = validate_stake_amount(
            case["requested"].as_f64().unwrap(),
            case["minimum"].as_f64().unwrap(),
            maximum,
        )
        .unwrap_or(0.0);
        assert_eq!(
            result.to_bits(),
            case["validated"].as_f64().unwrap().to_bits(),
            "{case}"
        );
    }
}

#[test]
fn selected_leverage_precedes_custom_stake_under_exchange_contract() {
    let mut settings = config(1);
    settings.is_futures = true;
    settings.starting_balance = 10000.0;
    settings.stake_amount = 1000.0;
    settings.leverage = Some(20.0);
    settings.stoploss_ratio = -0.99;
    // Returning selected leverage as stake makes the callback ordering observable.
    settings.stake_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{"op":"return", "value":{"op":"variable","name":"leverage"}}]
        }))
        .unwrap(),
    );
    let mut opening = candle(1, 100.0, 100.0);
    opening.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let pair = nfi_pair(vec![opening, candle(2, 100.0, 100.0)], BTreeMap::new());
    settings.liquidation_model = Some(isolated_model(
        &pair.pair,
        vec![
            leverage_tier(0.0, Some(1000.0), 20.0, 0.01, 0.0),
            leverage_tier(1000.0, Some(50000.0), 2.0, 0.02, 10.0),
        ],
    ));
    settings.order_stake_policy = Some(policy(&pair.pair, None, None));
    let mut input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: settings,
        pairs: vec![pair],
    };
    let result = simulate(&input).unwrap();
    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].leverage, 2.0);
    assert_eq!(result.trades[0].stake_amount, 2.0);
    input.config.order_stake_policy = None;
    let legacy = simulate(&input).unwrap();
    assert_eq!(legacy.trades[0].leverage, 20.0);
    assert_eq!(legacy.trades[0].stake_amount, 20.0);
}

#[test]
fn exchange_contract_rejects_missing_pairs_and_invalid_bounds() {
    let pair = nfi_pair(vec![candle(1, 100.0, 100.0)], BTreeMap::new());
    let mut input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(1),
        pairs: vec![pair],
    };
    for limit in [f64::NAN, f64::INFINITY, -1.0] {
        input.config.order_stake_policy = Some(policy(&input.pairs[0].pair, Some(limit), None));
        assert!(simulate(&input).is_err());
    }
    input.config.order_stake_policy = Some(policy("missing", None, None));
    assert!(simulate(&input).is_err());
    input.config.order_stake_policy = Some(policy(&input.pairs[0].pair, None, None));
    assert!(simulate(&input).is_ok());
    input.config.is_futures = true;
    assert!(simulate(&input).is_err());
}

#[test]
fn unbounded_limits_require_explicit_null_fields() {
    for value in [
        serde_json::json!({}),
        serde_json::json!({"maximum_amount": null}),
    ] {
        assert!(serde_json::from_value::<PairStakeLimits>(value).is_err());
    }
    assert!(
        serde_json::from_value::<PairStakeLimits>(serde_json::json!({
            "maximum_amount": null, "maximum_cost": null,
        }))
        .is_ok()
    );
}
