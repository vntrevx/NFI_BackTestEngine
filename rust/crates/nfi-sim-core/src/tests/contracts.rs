//! Focused execution-boundary and NFI cashflow contracts.

use super::*;
use crate::order_aggregates::FilledOrderSelector;

#[test]
fn nfi_profit_snapshot_uses_filled_order_cashflows_and_first_entry_basis() {
    let config = config(1);
    let pair = PairSeries {
        pair: "AAA/USDT".to_owned(),
        execution_start_index: 0,
        amount_step: None,
        price_step: None,
        price_steps: Vec::new(),
        minimum_stake: None,
        minimum_amount: None,
        minimum_cost: None,
        feature_columns: BTreeMap::new(),
        candles: vec![candle(1, 100.0, 100.0)].into(),
    };
    let signal = EntrySignal {
        tag: Some("141".to_owned()),
        leverage: None,
        liquidation_price: None,
    };
    let entry_candle = pair.candles.get(0).expect("fixture candle");
    let mut trade = enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry_candle,
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1_000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &config,
    )
    .expect("valid entry")
    .expect("sized entry");
    let initial_aggregates = trade.filled_order_aggregates().expect("finite aggregates");
    assert_eq!(initial_aggregates.order_count(), 1);
    assert_eq!(
        initial_aggregates
            .select_tag(FilledOrderSelector::Entries, Some("141"))
            .expect("initial tagged entry")
            .order_ids,
        [1]
    );
    let first = trade.orders[0].clone();
    let exit_amount = first.amount * 0.25;
    trade
        .push_filled_order(FilledOrder {
            id: 2,
            funding_fee: 0.0,
            sequence: 1,
            side: OrderSide::Sell,
            is_entry: false,
            filled_timestamp_ms: 2,
            amount: exit_amount,
            price: 110.0,
            cost: exit_amount * 110.0,
            tag: Some("d1".to_owned()),
        })
        .expect("finite aggregate append");
    let updated_aggregates = trade.filled_order_aggregates().expect("finite aggregates");
    assert_eq!(updated_aggregates.order_count(), 2);
    assert_eq!(
        updated_aggregates
            .select(FilledOrderSelector::Exits)
            .latest
            .as_ref()
            .map(|order| (order.id, order.price)),
        Some((2, 110.0))
    );

    let snapshot = nfi_profit_snapshot(&trade, 105.0, fee_open(&config), fee_close(&config), false)
        .expect("open amount remains");
    let entry_stake = first.amount * first.price * (1.0 + fee_open(&config));
    let exit_stake = exit_amount * 110.0 * (1.0 - fee_close(&config));
    let current_stake = (first.amount - exit_amount) * 105.0 * (1.0 - fee_close(&config));
    let expected = -entry_stake + exit_stake + current_stake;

    assert!((snapshot.stake - expected).abs() < 1e-12);
    assert!((snapshot.ratio - expected / entry_stake).abs() < 1e-12);
    assert!((snapshot.current_stake_ratio - expected / current_stake).abs() < 1e-12);
    assert!((snapshot.initial_stake_ratio - expected / (first.amount * first.price)).abs() < 1e-12);
}

#[test]
fn entry_confirmation_receives_post_precision_amount() {
    let mut config = config(1);
    config.entry_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {
                    "op": "greater",
                    "left": {"op": "variable", "name": "amount"},
                    "right": {"op": "literal", "value": 0.9}
                }
            }],
            "functions": {}
        }))
        .expect("valid confirmation program"),
    );
    let mut first = candle(1, 100.0, 100.0);
    first.enter_long = Some(EntrySignal {
        tag: Some("141".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut second = candle(2, 101.0, 101.0);
    second.exit_long = Some(ExitSignal {
        reason: "signal_exit".to_owned(),
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config,
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![first, second].into(),
        }],
    };

    let result = simulate(&input).expect("simulation succeeds");

    assert_eq!(result.trades.len(), 1);
}

#[test]
fn rejected_entry_confirmation_consumes_order_id_but_not_rejected_signal_count() {
    let mut config = config(1);
    config.entry_confirmation_program = Some(
        serde_json::from_value(serde_json::json!({
            "statements": [{
                "op": "return",
                "value": {
                    "op": "greater",
                    "left": {"op": "variable", "name": "amount"},
                    "right": {"op": "literal", "value": 0.9}
                }
            }],
            "functions": {}
        }))
        .expect("valid confirmation program"),
    );
    let mut rejected = candle(1, 200.0, 200.0);
    rejected.enter_long = Some(EntrySignal {
        tag: Some("rejected".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let mut accepted = candle(2, 100.0, 100.0);
    accepted.enter_long = Some(EntrySignal {
        tag: Some("accepted".to_owned()),
        leverage: None,
        liquidation_price: None,
    });
    let input = SimulationInput {
        schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
        config,
        pairs: vec![PairSeries {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: BTreeMap::new(),
            candles: vec![rejected, accepted, candle(3, 100.0, 100.0)].into(),
        }],
    };

    let result = simulate(&input).expect("simulation succeeds");

    assert_eq!(result.trades.len(), 1);
    assert_eq!(result.trades[0].orders[0].id, 2);
    assert_eq!(result.rejected_signals, 0);
}
