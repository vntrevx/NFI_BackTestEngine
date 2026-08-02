//! Shared open-trade and wallet state for the chronological portfolio.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};

use serde_json::Value;

use super::nfi::AdjustmentState;
use super::order_aggregates::FilledOrderAggregates;
use super::{ClosedTrade, FilledOrder};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TradeSide {
    Long,
    Short,
}

#[derive(Debug)]
pub(crate) struct EntryTagCache {
    pub(crate) words: Vec<String>,
    pub(crate) nfi_ids: OnceLock<Vec<Option<usize>>>,
}

#[derive(Debug, Clone)]
pub(crate) struct OpenTrade {
    pub(crate) id: u64,
    pub(crate) pair_index: usize,
    pub(crate) pair: String,
    pub(crate) side: TradeSide,
    pub(crate) leverage: f64,
    pub(crate) amount_step: f64,
    pub(crate) price_step: f64,
    pub(crate) open_timestamp_ms: i64,
    pub(crate) open_rate: f64,
    pub(crate) amount: f64,
    pub(crate) stake_amount: f64,
    pub(crate) max_stake_amount: f64,
    pub(crate) entry_cost_with_fees: f64,
    pub(crate) first_entry_cost_with_fees: f64,
    pub(crate) adjustment_count: usize,
    pub(crate) entry_tag: Option<String>,
    /// Words and manager-derived IDs cached for the immutable entry tag.
    pub(crate) entry_tag_cache: OnceLock<Arc<EntryTagCache>>,
    pub(crate) funding_fees: f64,
    pub(crate) funding_fees_total: f64,
    /// High and correction words for `CPython`'s compensated `sum(float)` path.
    ///
    /// Freqtrade recomputes the funding accrued since the most recent filled
    /// order with Python `sum()` on every funding tick. Keeping both words
    /// avoids losing the correction before that running value is attached to
    /// the next order.
    pub(crate) funding_sum_high: f64,
    pub(crate) funding_sum_low: f64,
    /// Post-partial-exit value of the funding row at the fill timestamp.
    ///
    /// Freqtrade temporarily retains the pre-exit forced refresh. At the next
    /// funding tick it recalculates the inclusive segment with the reduced
    /// amount, replacing that temporary value rather than adding to it.
    pub(crate) funding_rebase_seed: Option<f64>,
    pub(crate) realized_partial_profit: f64,
    pub(crate) liquidation_price: Option<f64>,
    pub(crate) liquidation_price_is_explicit: bool,
    pub(crate) initial_stop_loss: f64,
    pub(crate) stop_loss: f64,
    pub(crate) minimum_rate: f64,
    pub(crate) maximum_rate: f64,
    pub(crate) orders: Vec<FilledOrder>,
    pub(crate) filled_order_aggregates: OnceLock<FilledOrderAggregates>,
    pub(crate) custom_data: BTreeMap<String, Value>,
    /// Order-derived NFI grind state.
    ///
    /// NFI reconstructs this state from immutable filled orders on every
    /// callback. Caching the exact derived projection is behavior-preserving:
    /// adjustments only append orders, and the cache records the order count
    /// used to build it so the next callback invalidates it automatically.
    pub(crate) nfi_adjustment_state: Option<AdjustmentState>,
}

impl OpenTrade {
    pub(crate) fn entry_tag_cache(&self) -> &EntryTagCache {
        self.entry_tag_cache
            .get_or_init(|| {
                Arc::new(EntryTagCache {
                    words: self
                        .entry_tag
                        .as_deref()
                        .unwrap_or("")
                        .split_whitespace()
                        .map(str::to_owned)
                        .collect(),
                    nfi_ids: OnceLock::new(),
                })
            })
            .as_ref()
    }

    pub(crate) fn entry_tag_words(&self) -> &[String] {
        &self.entry_tag_cache().words
    }

    pub(crate) fn push_filled_order(&mut self, order: FilledOrder) {
        self.orders.push(order);
        self.filled_order_aggregates.take();
        self.nfi_adjustment_state = None;
    }

    pub(crate) fn filled_order_aggregates(&self) -> &FilledOrderAggregates {
        self.filled_order_aggregates
            .get_or_init(|| FilledOrderAggregates::from_orders(&self.orders))
    }
}

pub(super) fn wallet_free(
    starting_balance: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
) -> f64 {
    let realized_profit = closed_trades
        .iter()
        .map(|trade| trade.profit_abs)
        .sum::<f64>();
    let tied_up_stake = open_trades
        .iter()
        .map(|trade| trade.stake_amount)
        .sum::<f64>();
    let open_realized_profit = open_trades
        .iter()
        .map(|trade| trade.realized_partial_profit)
        .sum::<f64>();
    // Freqtrade does not settle a running funding value into its backtest
    // wallet. It becomes available only through a realized partial exit or a
    // closed trade, both of which are already included above.
    starting_balance + realized_profit + open_realized_profit - tied_up_stake
}
