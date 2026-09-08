//! Official wallet vectors and entry callback ordering under insufficient funds.

use super::*;
use crate::calculations::{configured_available_stake_amount, configured_entry_stake};

fn input() -> SimulationInput {
    let mut opening = candle(1, 100.0, 100.0);
    opening.enter_long = Some(EntrySignal {
        tag: Some("entry".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config: config(2),
        pairs: vec![nfi_pair(
            vec![opening, candle(2, 100.0, 100.0)],
            BTreeMap::new(),
        )],
    }
}

#[test]
fn allocation_matches_official_wallet_vectors() {
    let document: Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-settings-2026-09-08/official-wallet-settings-results.json"
    )).unwrap();
    let mut closed = simulate(&input()).unwrap().trades;
    assert_eq!(closed.len(), 1);
    for case in document["cases"].as_array().unwrap() {
        let mut settings = config(2);
        settings.tradable_balance_ratio = 0.75;
        settings.strategy_wallet_policy = Some(StrategyWalletPolicy {
            entry_minimum_stoploss_ratio: None,
            source_max_open_trades: None,
            amend_last_stake_amount: case["amend"].as_bool().unwrap(),
            last_stake_amount_min_ratio: case["ratio"].as_f64().unwrap(),
            available_capital: case["capital"].as_f64(),
            initial_asset_balances: BTreeMap::new(),
        });
        closed[0].profit_abs = case["closed_profit"].as_f64().unwrap();
        let tied = case["tied"].as_f64().unwrap();
        let available = configured_available_stake_amount(
            case["free"].as_f64().unwrap(),
            tied,
            &closed,
            &settings,
        )
        .unwrap();
        assert_eq!(
            available.to_bits(),
            case["available"].as_f64().unwrap().to_bits(),
            "{case}"
        );
        let proposed = if case["unlimited"].as_bool().unwrap() {
            ((available + tied) / 2.0).min(available)
        } else {
            100.0
        };
        let stake = configured_entry_stake(proposed, available, &settings);
        assert_eq!(
            stake.map(f64::to_bits),
            case["stake"].as_f64().map(f64::to_bits),
            "{case}"
        );
    }
}

#[test]
fn insufficient_funds_skip_callback_but_zero_amendment_still_calls_it() {
    let mut scenario = input();
    scenario.config.starting_balance = 50.0;
    scenario.config.strategy_wallet_policy = Some(StrategyWalletPolicy {
        entry_minimum_stoploss_ratio: None,
        source_max_open_trades: None,
        amend_last_stake_amount: false,
        last_stake_amount_min_ratio: 0.5,
        available_capital: None,
        initial_asset_balances: BTreeMap::new(),
    });
    scenario.config.stake_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{"op": "return", "value": {"op": "literal", "value": 10.0}}]
        }))
        .unwrap(),
    );
    assert!(simulate(&scenario).unwrap().trades.is_empty());
    scenario
        .config
        .strategy_wallet_policy
        .as_mut()
        .unwrap()
        .amend_last_stake_amount = true;
    assert_eq!(simulate(&scenario).unwrap().trades.len(), 1);
}

#[test]
fn initial_assets_survive_entry_and_close_without_funding_new_stake() {
    let mut scenario = input();
    scenario.config.strategy_wallet_policy = Some(StrategyWalletPolicy {
        entry_minimum_stoploss_ratio: None,
        source_max_open_trades: None,
        amend_last_stake_amount: false,
        last_stake_amount_min_ratio: 0.5,
        available_capital: None,
        initial_asset_balances: BTreeMap::from([("AAA".to_owned(), 0.25), ("BTC".to_owned(), 0.2)]),
    });
    let mut opening = scenario.pairs[0].candles.get(0).unwrap().into_owned();
    opening.timestamp_ms = 2;
    let mut closing = candle(3, 100.0, 100.0);
    closing.exit_long = Some(ExitSignal {
        reason: "exit_signal".to_owned(),
    });
    scenario.pairs[0].candles = vec![candle(1, 100.0, 100.0), opening, closing].into();
    let mut events = Vec::new();
    let result = simulate_with_observer(&scenario, |event| events.push(event.clone())).unwrap();
    assert_eq!(result.trades.len(), 1);
    assert_eq!(events.len(), 2);
    for (index, expected) in [1.25_f64, 0.25].into_iter().enumerate() {
        let balances = &events[index].state.base_balances;
        assert_eq!(balances.len(), 2);
        assert_eq!(balances[0].currency, "AAA");
        assert_eq!(balances[0].free.to_bits(), expected.to_bits());
        assert_eq!(balances[1].currency, "BTC");
        assert_eq!(balances[1].free.to_bits(), 0.2_f64.to_bits());
    }
    assert_eq!(result.trades[0].stake_amount.to_bits(), 100.0_f64.to_bits());
}
