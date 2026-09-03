//! Shared open-trade and wallet state for the chronological portfolio.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};

use serde_json::Value;

use super::nfi::AdjustmentState;
use super::order_aggregates::FilledOrderAggregates;
use crate::calculations::{checked_float_sum, precise_sum};
use crate::domain::SimError;

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
    pub(crate) custom_stop_loss_ratio: Option<f64>,
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
    pub(crate) nfi_adjustment_state: Option<Arc<AdjustmentState>>,
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

    pub(crate) fn push_filled_order(&mut self, order: FilledOrder) -> Result<(), SimError> {
        if let Some(aggregates) = self.filled_order_aggregates.get_mut() {
            aggregates.push(&order)?;
        }
        self.orders.push(order);
        self.nfi_adjustment_state = None;
        Ok(())
    }

    pub(crate) fn filled_order_aggregates(&self) -> Result<&FilledOrderAggregates, SimError> {
        if self.filled_order_aggregates.get().is_none() {
            let aggregates = FilledOrderAggregates::from_orders(&self.orders)?;
            let _ = self.filled_order_aggregates.set(aggregates);
        }
        self.filled_order_aggregates
            .get()
            .ok_or(SimError::ExactArithmetic {
                operation: "order-aggregate-storage",
            })
    }
}

pub(super) fn wallet_free(
    starting_balance: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    is_futures: bool,
) -> Result<f64, SimError> {
    let realized_profit = checked_float_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
        "wallet-realized-profit",
    )?;
    let tied_up_stake = checked_float_sum(
        &open_trades
            .iter()
            .map(|trade| trade.stake_amount)
            .collect::<Vec<_>>(),
        "wallet-tied-up-stake",
    )?;
    let open_realized_profit = checked_float_sum(
        &open_trades
            .iter()
            .map(|trade| trade.realized_partial_profit)
            .collect::<Vec<_>>(),
        "wallet-open-realized-profit",
    )?;
    // Freqtrade first merges closed and open realized profit, then adds the
    // result to starting capital, and only then subtracts collateral.
    // Reassociating these operations changes exact quote-free tokens.
    wallet_free_from_totals(
        starting_balance,
        realized_profit,
        open_realized_profit,
        tied_up_stake,
        is_futures,
    )
}

fn wallet_free_from_totals(
    starting_balance: f64,
    realized_profit: f64,
    open_realized_profit: f64,
    tied_up_stake: f64,
    is_futures: bool,
) -> Result<f64, SimError> {
    let total_profit = checked_float_sum(
        &[realized_profit, open_realized_profit],
        "wallet-total-profit",
    )?;
    if is_futures && (total_profit == 0.0 || tied_up_stake == 0.0) {
        // Futures collateral has already crossed Freqtrade's decimal precision
        // boundary. Preserve that representation for ungrouped wallet paths.
        // Spot keeps Python's ordinary left-associated float subtraction.
        return precise_sum(&[starting_balance, total_profit, -tied_up_stake]).map_err(|_| {
            SimError::ExactArithmetic {
                operation: "wallet-final-balance",
            }
        });
    }
    checked_float_sum(
        &[starting_balance, total_profit, -tied_up_stake],
        "wallet-final-balance",
    )
}

#[cfg(test)]
mod tests {
    use super::wallet_free_from_totals;

    #[test]
    fn wallet_free_preserves_freqtrade_profit_grouping_boundary() {
        let quote_free = wallet_free_from_totals(
            10_000.0,
            1_707.555_080_630_000_2,
            -509.448_001_5,
            9_273.294_6,
            false,
        )
        .expect("finite wallet");

        assert_eq!(quote_free.to_bits(), 1_924.812_479_130_001_5_f64.to_bits());
    }

    #[test]
    fn wallet_free_preserves_no_profit_decimal_boundary() {
        let quote_free =
            wallet_free_from_totals(110.0, 0.0, 0.0, 95.9792, true).expect("finite wallet");

        assert_eq!(quote_free.to_bits(), 14.0208_f64.to_bits());
    }

    #[test]
    fn wallet_free_preserves_fully_closed_decimal_boundary() {
        let quote_free =
            wallet_free_from_totals(110.0, -67.585_389_07, 0.0, 0.0, true).expect("finite wallet");

        assert_eq!(quote_free.to_bits(), 42.414_610_93_f64.to_bits());
    }

    #[test]
    fn spot_wallet_free_preserves_python_subtraction_boundary() {
        let quote_free = wallet_free_from_totals(10_000.0, 0.0, 0.0, 9_899.999_187_6, false)
            .expect("finite wallet");

        assert_eq!(quote_free.to_bits(), 100.000_812_399_999_63_f64.to_bits());
    }
}
