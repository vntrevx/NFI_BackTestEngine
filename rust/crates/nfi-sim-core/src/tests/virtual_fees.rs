//! Exact official-source profit vectors and exchange-fee separation.

use super::*;
use crate::nfi::{nfi_fee_close, nfi_fee_open};

#[test]
fn source_virtual_fees_match_official_profit_vectors() {
    let vectors: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../benchmarks/evidence/x8-virtual-fees-2026-09-07/official-profit-cases.json"
    ))
    .unwrap();
    let cases = vectors["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 84);
    for (index, case) in cases.iter().enumerate() {
        let mut config = config(1);
        config.fee_open_rate = case["actual_open_rate"].as_f64();
        config.fee_close_rate = case["actual_close_rate"].as_f64();
        let mut manager = nfi_top_coins_manager(nfi_false_program());
        manager.virtual_fees = Some(
            serde_json::from_value(serde_json::json!({
                "open_rate": case["open_rate"], "close_rate": case["close_rate"]
            }))
            .unwrap(),
        );
        // Fee selection is tested separately from manager admission, which
        // requires a complete source-compiled exit contract.
        config.nfi_x7_trade_manager = Some(manager);
        let actual_open = fee_open(&config);
        let actual_close = fee_close(&config);
        assert_eq!(actual_open, 0.0004);
        assert_eq!(actual_close, 0.0005);
        assert_eq!(
            nfi_fee_open(&config),
            case["open_rate"].as_f64().unwrap_or(actual_open)
        );
        assert_eq!(
            nfi_fee_close(&config),
            case["close_rate"].as_f64().unwrap_or(actual_close)
        );
        let mut trade = initial_trade();
        let short = case["short"].as_bool().unwrap();
        trade.side = if short {
            TradeSide::Short
        } else {
            TradeSide::Long
        };
        trade.funding_fees_total = case["funding_fees"].as_f64().unwrap_or(0.0);
        trade.orders = case["orders"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .map(|(i, order)| {
                let is_entry = order[0].as_bool().unwrap();
                let amount = order[1].as_f64().unwrap();
                let price = order[2].as_f64().unwrap();
                FilledOrder {
                    id: u64::try_from(i + 1).unwrap(),
                    sequence: i,
                    funding_fee: 0.0,
                    is_entry,
                    side: if is_entry == short {
                        OrderSide::Sell
                    } else {
                        OrderSide::Buy
                    },
                    filled_timestamp_ms: i64::try_from(i + 1).unwrap(),
                    amount,
                    price,
                    cost: amount * price,
                    tag: None,
                }
            })
            .collect();
        config.is_futures = case["futures"].as_bool().unwrap();
        let result = crate::nfi::decision_profit_snapshot(
            &trade,
            case["exit_rate"].as_f64().unwrap(),
            &config,
        )
        .unwrap();
        let expected: [f64; 4] = serde_json::from_value(case["expected"].clone()).unwrap();
        assert_eq!(
            [
                result.stake,
                result.ratio,
                result.current_stake_ratio,
                result.initial_stake_ratio
            ],
            expected,
            "official profit vector {index}"
        );
    }
}

pub(super) fn initial_trade() -> crate::portfolio::OpenTrade {
    let pair = nfi_pair(vec![candle(1, 100.0, 100.0)], BTreeMap::new());
    let entry = pair.candles.get(0).unwrap();
    let signal = EntrySignal {
        tag: None,
        leverage: None,
        liquidation_price: None,
    };
    let entry_config = config(1);
    enter_trade(
        EntryRequest {
            pair_index: 0,
            pair: &pair,
            candle: &entry,
            side: TradeSide::Long,
            signal: &signal,
            stake: EntryStake {
                proposed: 100.0,
                maximum: 1000.0,
            },
            open_trades: &[],
            id: 1,
            order_id: 1,
        },
        &entry_config,
    )
    .unwrap()
    .unwrap()
}
