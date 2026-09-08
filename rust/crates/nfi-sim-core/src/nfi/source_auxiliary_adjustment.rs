//! Source-described entry counts outside the numbered grind ladder.

use crate::calculations::{checked_finite, checked_float_product, checked_float_sum};
use crate::domain::{CompiledCountedEntryGroup, FilledOrder};

pub(crate) fn counted_entries(
    orders: &[FilledOrder],
    groups: &[CompiledCountedEntryGroup],
) -> Vec<usize> {
    groups
        .iter()
        .map(|group| {
            orders
                .iter()
                .rev()
                .take_while(|order| {
                    order.is_entry
                        || !group.exit_tags.iter().any(|tag| {
                            order.tag.as_deref().unwrap_or("").split(' ').next()
                                == Some(tag.as_str())
                        })
                })
                .filter(|order| {
                    order.is_entry
                        && order.sequence != 0
                        && order.tag.as_deref() == Some(group.entry_tag.as_str())
                })
                .count()
        })
        .collect()
}

pub(crate) struct AuxiliaryGroupState {
    pub(crate) open_rate: f64,
    pub(crate) total_amount: f64,
    pub(crate) total_cost: f64,
    pub(crate) entry_ids: Vec<u64>,
    pub(crate) exit_price: Option<f64>,
}

pub(crate) fn group_state(
    orders: &[FilledOrder],
    group: &CompiledCountedEntryGroup,
) -> Option<AuxiliaryGroupState> {
    let mut entry_ids = Vec::new();
    let mut total_amount = 0.0;
    let mut total_cost = 0.0;
    let mut count = 0_usize;
    let mut exit_price = None;
    let mut fallback_price = None;
    for order in orders.iter().rev() {
        let tag = order.tag.as_deref().unwrap_or("");
        if order.is_entry {
            if exit_price.is_none() && order.sequence != 0 && tag == group.entry_tag {
                count = count.checked_add(1)?;
                entry_ids.push(order.id);
                total_amount = checked_float_sum(
                    &[total_amount, order.amount],
                    "auxiliary-group-total-amount",
                )
                .ok()?;
                let cost = checked_float_product(
                    &[order.amount, order.price],
                    "auxiliary-group-order-cost",
                )
                .ok()?;
                total_cost =
                    checked_float_sum(&[total_cost, cost], "auxiliary-group-total-cost").ok()?;
            }
        } else {
            let head = tag.split(' ').next().unwrap_or("");
            if exit_price.is_none() && group.exit_tags.iter().any(|closing| closing == head) {
                exit_price = Some(order.price);
            }
            if fallback_price.is_none() && group.fallback_exit_tag.as_deref() == Some(head) {
                fallback_price = Some(order.price);
            }
        }
    }
    let open_rate = if count == 0 {
        0.0
    } else {
        checked_finite(total_cost / total_amount, "auxiliary-group-open-rate").ok()?
    };
    Some(AuxiliaryGroupState {
        open_rate,
        total_amount,
        total_cost,
        entry_ids,
        exit_price: exit_price.or(fallback_price),
    })
}
