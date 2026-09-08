//! Auxiliary group resets, source order, and exit-anchor precedence.

use super::*;
use crate::nfi::source_auxiliary_adjustment::{counted_entries, group_state};

#[test]
fn auxiliary_prices_and_ids_follow_source_group_boundaries() {
    let (_, trade) = super::callbacks::order_filled_fixture();
    let seed = trade.orders.first().unwrap();
    let make = |sequence: usize, entry: bool, tag: &str, amount: f64, price: f64| {
        let mut order = seed.clone();
        order.sequence = sequence;
        order.id = u64::try_from(sequence + 1).unwrap();
        order.is_entry = entry;
        order.side = if entry {
            OrderSide::Buy
        } else {
            OrderSide::Sell
        };
        order.tag = Some(tag.to_owned());
        order.amount = amount;
        order.price = price;
        order
    };
    let group: CompiledCountedEntryGroup = serde_json::from_value(serde_json::json!({
        "count_variable":"group_count", "entry_tag":"aux_entry",
        "exit_tags":["aux_exit","aux_derisk","global_exit"],
        "fallback_exit_tag":"level_four", "exit_action_tag":"aux_derisk",
        "entry_program":"source_helper"
    }))
    .unwrap();
    let mut orders = vec![
        make(0, true, "aux_entry", 100.0, 100.0),
        make(1, true, "aux_entry", 1.0, 90.0),
        make(2, true, "aux_entry", 2.0, 80.0),
        make(3, false, "level_four", 0.5, 95.0),
        make(4, true, "aux_entry extra", 100.0, 120.0),
    ];
    let before = group_state(&orders, &group).unwrap();
    assert_eq!(before.total_amount, 3.0);
    assert_eq!(before.total_cost, 250.0);
    assert_eq!(before.open_rate.to_bits(), (250.0_f64 / 3.0).to_bits());
    assert_eq!(before.entry_ids, [3, 2]);
    assert_eq!(before.exit_price, Some(95.0));
    assert_eq!(counted_entries(&orders, std::slice::from_ref(&group)), [2]);
    orders.push(make(5, false, "aux_derisk 3 2", 3.0, 75.0));
    let closed = group_state(&orders, &group).unwrap();
    assert_eq!(closed.open_rate, 0.0);
    assert!(closed.entry_ids.is_empty());
    orders.push(make(6, true, "aux_entry", 3.0, 70.0));
    orders.push(make(7, false, "level_four", 1.0, 50.0));
    let reopened = group_state(&orders, &group).unwrap();
    assert_eq!(reopened.open_rate, 70.0);
    assert_eq!(reopened.exit_price, Some(75.0));
    assert_eq!(reopened.entry_ids, [7]);
    orders.push(make(8, false, "global_exit", 1.0, 60.0));
    let reset = group_state(&orders, &group).unwrap();
    assert_eq!(reset.total_amount, 0.0);
    assert_eq!(reset.exit_price, Some(60.0));
    assert_eq!(counted_entries(&orders, &[group]), [0]);
}
