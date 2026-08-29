//! Serialized simulation results and state projections.

use std::collections::BTreeMap;

use serde::ser::SerializeStruct;
use serde::{Serialize, Serializer};
use serde_json::Value;

use crate::protections::PairLockState;

use super::{
    CallbackProgramResult, CallbackSemanticEvent, ExecutionBoundaryEvent, PortfolioBoundaryEvent,
};

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

impl SimulationResult {
    pub(crate) fn numbers_are_finite(&self) -> bool {
        [
            self.starting_balance,
            self.final_balance,
            self.profit_total_abs,
            self.total_volume,
        ]
        .into_iter()
        .all(f64::is_finite)
            && self.trades.iter().all(ClosedTrade::numbers_are_finite)
    }
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

pub const SIMULATION_EVENT_SCHEMA_VERSION: &str = "portfolio-scheduler-event-v1";

#[derive(Debug, Clone, PartialEq)]
pub struct SimulationEvent {
    pub schema_version: &'static str,
    pub timestamp_ms: i64,
    pub pair: String,
    pub state: SimulationState,
    pub callback_events: Vec<CallbackSemanticEvent>,
    pub executable_callback_events: Vec<CallbackProgramResult>,
    pub portfolio_events: Vec<PortfolioBoundaryEvent>,
    pub execution_events: Vec<ExecutionBoundaryEvent>,
}

impl Serialize for SimulationEvent {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("SimulationEvent", 7)?;
        state.serialize_field("schema_version", self.schema_version)?;
        state.serialize_field("timestamp_ms", &self.timestamp_ms)?;
        state.serialize_field("pair", &self.pair)?;
        state.serialize_field("state", &self.state)?;
        if self.executable_callback_events.is_empty() {
            state.serialize_field("callback_events", &self.callback_events)?;
        } else {
            state.serialize_field("callback_events", &self.executable_callback_events)?;
        }
        state.serialize_field("portfolio_events", &self.portfolio_events)?;
        state.serialize_field("execution_events", &self.execution_events)?;
        state.end()
    }
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SimulationState {
    pub quote_total: f64,
    pub quote_free: f64,
    pub quote_used: f64,
    pub tied_up_stake: f64,
    pub realized_wallet_profit: f64,
    pub base_balances: Vec<AssetBalance>,
    pub configured_pair_index: usize,
    pub processing_order_index: usize,
    pub candle_index: usize,
    pub next_candle_index: usize,
    pub occupied_slots: usize,
    pub slot_limit: usize,
    pub open_trade_count: usize,
    pub open_trade_ids: Vec<u64>,
    pub open_trade_pairs: Vec<String>,
    pub open_order_ids: Vec<u64>,
    pub open_trades: Vec<SemanticOpenTradeState>,
    pub realized_profit: f64,
    pub closed_trade_count: usize,
    pub closed_trades: Vec<SemanticClosedTradeState>,
    pub rejected_signals: u64,
    pub trade_id_counter: u64,
    pub order_id_counter: u64,
    pub locks: Vec<PairLockState>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AssetBalance {
    pub currency: String,
    pub free: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SemanticOrderState {
    pub id: u64,
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

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SemanticOpenTradeState {
    pub id: u64,
    pub pair_index: usize,
    pub pair: String,
    pub is_short: bool,
    pub leverage: f64,
    pub amount_step: f64,
    pub price_step: f64,
    pub open_timestamp_ms: i64,
    pub open_rate: f64,
    pub amount: f64,
    pub stake_amount: f64,
    pub max_stake_amount: f64,
    pub entry_cost_with_fees: f64,
    pub first_entry_cost_with_fees: f64,
    pub adjustment_count: usize,
    pub entry_tag: Option<String>,
    pub funding_fees: f64,
    pub funding_fees_total: f64,
    pub funding_sum_high: f64,
    pub funding_sum_low: f64,
    pub funding_rebase_seed: Option<f64>,
    pub realized_partial_profit: f64,
    pub liquidation_price: Option<f64>,
    pub liquidation_price_is_explicit: bool,
    pub initial_stop_loss: f64,
    pub stop_loss: f64,
    pub custom_stop_loss_ratio: Option<f64>,
    pub minimum_rate: f64,
    pub maximum_rate: f64,
    pub orders: Vec<SemanticOrderState>,
    pub custom_data: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SemanticClosedTradeState {
    pub trade: ClosedTrade,
    pub orders: Vec<SemanticOrderState>,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub custom_stop_loss_ratio: Option<f64>,
    pub minimum_rate: f64,
    pub maximum_rate: f64,
    pub orders: Vec<FilledOrder>,
}

impl ClosedTrade {
    fn numbers_are_finite(&self) -> bool {
        [
            self.leverage,
            self.open_rate,
            self.close_rate,
            self.amount,
            self.stake_amount,
            self.max_stake_amount,
            self.fee_open,
            self.fee_close,
            self.funding_fees,
            self.profit_abs,
            self.profit_ratio,
            self.initial_stop_loss,
            self.stop_loss,
            self.custom_stop_loss_ratio.unwrap_or_default(),
            self.minimum_rate,
            self.maximum_rate,
        ]
        .into_iter()
        .all(f64::is_finite)
            && self.liquidation_price.is_none_or(f64::is_finite)
            && self.orders.iter().all(FilledOrder::numbers_are_finite)
    }
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

impl FilledOrder {
    fn numbers_are_finite(&self) -> bool {
        [self.funding_fee, self.amount, self.price, self.cost]
            .into_iter()
            .all(f64::is_finite)
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OrderSide {
    Buy,
    Sell,
}
