//! Serialized simulation results and state projections.

use serde::Serialize;

use crate::protections::PairLockState;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationResult {
    pub schema_version: &'static str,
    pub starting_balance: f64,
    pub final_balance: f64,
    pub profit_total_abs: f64,
    pub total_volume: f64,
    pub rejected_signals: u64,
    pub maximum_concurrent_trades: usize,
    pub locks: Vec<PairLockState>,
    pub trades: Vec<ClosedTrade>,
}

/// Aggregate hot-loop measurements emitted separately from financial results.
///
/// Keeping this record outside [`SimulationResult`] preserves the exact public
/// trade-surface bytes while allowing representative runs to locate real
/// bottlenecks without per-candle logging.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SimulationProfile {
    pub schema_version: &'static str,
    pub validation_ns: u64,
    pub event_loop_ns: u64,
    pub finalization_ns: u64,
    pub timestamp_batches: u64,
    pub pair_events: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationEvent {
    pub timestamp_ms: i64,
    pub pair: String,
    pub state: SimulationState,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationState {
    pub quote_free: f64,
    pub base_balances: Vec<AssetBalance>,
    pub open_trade_count: usize,
    pub realized_profit: f64,
    pub closed_trade_count: usize,
    pub rejected_signals: u64,
    pub trade_id_counter: u64,
    pub order_id_counter: usize,
    pub locks: Vec<PairLockState>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AssetBalance {
    pub currency: String,
    pub free: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ClosedTrade {
    pub sequence: usize,
    pub id: u64,
    pub pair: String,
    pub is_short: bool,
    pub leverage: f64,
    pub open_timestamp_ms: i64,
    pub close_timestamp_ms: i64,
    pub open_rate: f64,
    pub close_rate: f64,
    pub amount: f64,
    pub stake_amount: f64,
    pub max_stake_amount: f64,
    pub entry_tag: Option<String>,
    pub exit_reason: String,
    pub fee_open: f64,
    pub fee_close: f64,
    pub funding_fees: f64,
    pub liquidation_price: Option<f64>,
    pub profit_abs: f64,
    pub profit_ratio: f64,
    pub initial_stop_loss: f64,
    pub stop_loss: f64,
    pub minimum_rate: f64,
    pub maximum_rate: f64,
    pub orders: Vec<FilledOrder>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FilledOrder {
    /// Freqtrade's process-global order identifier.
    ///
    /// It participates in NFI grind tags but is not part of the normalized
    /// public trade surface, so serialization deliberately omits it.
    #[serde(skip)]
    pub id: u64,
    /// Funding accumulated since the previous filled order.
    ///
    /// Freqtrade moves the complete running funding value onto every filled
    /// order and resets the running accumulator. Replaying this hidden field
    /// is required for exact partial-exit profit accounting, but it is not
    /// part of the engine result or normalized public trade surface.
    #[serde(skip)]
    pub funding_fee: f64,
    pub sequence: usize,
    pub side: OrderSide,
    pub is_entry: bool,
    pub filled_timestamp_ms: i64,
    pub amount: f64,
    pub price: f64,
    pub cost: f64,
    pub tag: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OrderSide {
    Buy,
    Sell,
}
