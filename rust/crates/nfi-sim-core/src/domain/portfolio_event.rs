//! Versioned chronological portfolio mutation observer contract.

use serde::Serialize;

pub const PORTFOLIO_EVENT_SCHEMA_VERSION: &str = "portfolio-mutation-event-v1";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct PortfolioBoundaryEvent {
    pub schema_version: &'static str,
    pub sequence: u64,
    pub timestamp_ms: i64,
    pub boundary: PortfolioBoundary,
    pub pair: String,
    pub configured_pair_index: usize,
    pub processing_order_index: usize,
    pub state_before: PortfolioBoundaryState,
    pub state_after: PortfolioBoundaryState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rejection_reason: Option<EntryRejectionReason>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allocated_trade_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allocated_order_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proposed_stake: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub compounding_base: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub partial_exit_slot_retained: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub force_exit_index: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub force_exit_trade_id: Option<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub force_exit_order_ids: Vec<u64>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PortfolioBoundary {
    PairVisit,
    EntryAccepted,
    EntryRejected,
    PositionAdjustment,
    PartialExit,
    TradeClose,
    ForceExit,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EntryRejectionReason {
    PairLocked,
    SlotLimit,
    MinimumStake,
    StakePrecision,
    EntryConfirmation,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct PortfolioBoundaryState {
    pub wallet_free: f64,
    pub wallet_tied: f64,
    pub realized_closed: f64,
    pub realized_partial: f64,
    pub occupied_slots: usize,
    pub slot_limit: usize,
    pub open_trade_ids: Vec<u64>,
    pub open_trade_pairs: Vec<String>,
    pub open_order_ids: Vec<u64>,
    pub next_trade_id: u64,
    pub next_order_id: u64,
    pub rejected_signals: u64,
}
