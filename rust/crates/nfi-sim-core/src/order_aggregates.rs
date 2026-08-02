//! Strategy-neutral projections over immutable filled-order history.

use std::collections::BTreeMap;

use crate::domain::FilledOrder;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FilledOrderSelector {
    All,
    Entries,
    Exits,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct LatestFilledOrder {
    pub(crate) id: u64,
    pub(crate) sequence: usize,
    pub(crate) price: f64,
    pub(crate) timestamp_ms: i64,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct FilledOrderSummary {
    pub(crate) count: usize,
    pub(crate) total_amount: f64,
    pub(crate) total_cost: f64,
    pub(crate) order_ids: Vec<u64>,
    pub(crate) latest: Option<LatestFilledOrder>,
}

impl FilledOrderSummary {
    fn observe(&mut self, order: &FilledOrder) {
        self.count += 1;
        self.total_amount += order.amount;
        self.total_cost += order.cost;
        self.order_ids.push(order.id);
        self.latest = Some(LatestFilledOrder {
            id: order.id,
            sequence: order.sequence,
            price: order.price,
            timestamp_ms: order.filled_timestamp_ms,
        });
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
struct FilledOrderGroup {
    all: FilledOrderSummary,
    entries: FilledOrderSummary,
    exits: FilledOrderSummary,
}

impl FilledOrderGroup {
    fn observe(&mut self, order: &FilledOrder) {
        self.all.observe(order);
        if order.is_entry {
            self.entries.observe(order);
        } else {
            self.exits.observe(order);
        }
    }

    fn select(&self, selector: FilledOrderSelector) -> &FilledOrderSummary {
        match selector {
            FilledOrderSelector::All => &self.all,
            FilledOrderSelector::Entries => &self.entries,
            FilledOrderSelector::Exits => &self.exits,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct FilledOrderAggregates {
    order_count: usize,
    totals: FilledOrderGroup,
    by_tag: BTreeMap<Option<String>, FilledOrderGroup>,
}

impl FilledOrderAggregates {
    pub(crate) fn from_orders(orders: &[FilledOrder]) -> Self {
        let mut aggregates = Self::default();
        for order in orders {
            aggregates.push(order);
        }
        aggregates
    }

    /// Extend an initialized projection after the immutable order log appends.
    pub(crate) fn push(&mut self, order: &FilledOrder) {
        self.totals.observe(order);
        self.by_tag
            .entry(order.tag.clone())
            .or_default()
            .observe(order);
        self.order_count += 1;
    }

    pub(crate) const fn order_count(&self) -> usize {
        self.order_count
    }

    pub(crate) fn select(&self, selector: FilledOrderSelector) -> &FilledOrderSummary {
        self.totals.select(selector)
    }

    #[allow(dead_code, reason = "generic cluster IR consumes this in M16-02")]
    pub(crate) fn select_tag(
        &self,
        selector: FilledOrderSelector,
        tag: Option<&str>,
    ) -> Option<&FilledOrderSummary> {
        self.by_tag
            .get(&tag.map(ToOwned::to_owned))
            .map(|group| group.select(selector))
    }
}

#[cfg(test)]
mod tests {
    use crate::domain::{FilledOrder, OrderSide};

    use super::*;

    fn order(
        id: u64,
        sequence: usize,
        is_entry: bool,
        amount: f64,
        price: f64,
        tag: Option<&str>,
    ) -> FilledOrder {
        FilledOrder {
            id,
            funding_fee: 0.0,
            sequence,
            side: if is_entry {
                OrderSide::Buy
            } else {
                OrderSide::Sell
            },
            is_entry,
            filled_timestamp_ms: i64::try_from(sequence + 1).expect("small sequence"),
            amount,
            price,
            cost: amount * price,
            tag: tag.map(ToOwned::to_owned),
        }
    }

    #[test]
    fn generic_aggregates_match_a_source_order_walk() {
        let orders = vec![
            order(1, 0, true, 2.0, 100.0, Some("entry-a")),
            order(2, 1, true, 1.0, 90.0, Some("entry-a")),
            order(3, 2, false, 0.5, 110.0, Some("exit-a")),
            order(4, 3, true, 0.25, 80.0, None),
        ];
        let aggregates = FilledOrderAggregates::from_orders(&orders);

        assert_eq!(aggregates.order_count(), orders.len());
        assert_eq!(
            aggregates.select(FilledOrderSelector::All),
            &FilledOrderSummary {
                count: 4,
                total_amount: 3.75,
                total_cost: 365.0,
                order_ids: vec![1, 2, 3, 4],
                latest: Some(LatestFilledOrder {
                    id: 4,
                    sequence: 3,
                    price: 80.0,
                    timestamp_ms: 4,
                }),
            }
        );
        let entry_cluster = aggregates
            .select_tag(FilledOrderSelector::Entries, Some("entry-a"))
            .expect("entry tag cluster");
        assert_eq!(entry_cluster.count, 2);
        assert!((entry_cluster.total_amount - 3.0).abs() < f64::EPSILON);
        assert!((entry_cluster.total_cost - 290.0).abs() < f64::EPSILON);
        assert_eq!(entry_cluster.order_ids, [1, 2]);
        assert_eq!(
            aggregates
                .select_tag(FilledOrderSelector::Entries, None)
                .expect("untagged entry")
                .latest
                .as_ref()
                .map(|order| order.id),
            Some(4)
        );
        assert_eq!(
            aggregates
                .select(FilledOrderSelector::Exits)
                .latest
                .as_ref()
                .map(|order| order.id),
            Some(3)
        );
    }

    #[test]
    fn incremental_append_matches_a_complete_rebuild() {
        let mut orders = vec![
            order(1, 0, true, 2.0, 100.0, Some("entry-a")),
            order(2, 1, true, 1.0, 90.0, Some("entry-a")),
        ];
        let mut incremental = FilledOrderAggregates::from_orders(&orders);
        let appended = order(3, 2, false, 0.5, 110.0, Some("exit-a"));

        incremental.push(&appended);
        orders.push(appended);

        assert_eq!(incremental, FilledOrderAggregates::from_orders(&orders));
    }
}
