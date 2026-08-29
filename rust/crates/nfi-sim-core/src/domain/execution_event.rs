//! Versioned exact execution-boundary observer contract.

use std::collections::BTreeMap;

use serde::Serialize;

use super::PortfolioBoundaryState;

pub const EXECUTION_BOUNDARY_EVENT_SCHEMA_VERSION: &str = "execution-boundary-event-v1";

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionBoundary {
    EntryCandidate,
    EntryGate,
    EntryFill,
    ExitCandidate,
    ExitCompetition,
    ExitConfirmation,
    ExitFill,
    AdjustmentCandidate,
    AdjustmentFill,
    PartialExitFill,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ExecutionCandle {
    pub open: String,
    pub high: String,
    pub low: String,
    pub close: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ExecutionBoundaryEvent {
    pub schema_version: &'static str,
    pub sequence: u64,
    pub timestamp_ms: i64,
    pub pair: String,
    pub phase: ExecutionBoundary,
    pub order_type: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_status: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proposed_rate: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub clamped_rate: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub precision_rate: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub within_candle: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout_checked: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timed_out: Option<bool>,
    pub candle: ExecutionCandle,
    pub candidates: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub winner: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confirmation: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rejection_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trade_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount_input: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount_step: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount_output: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price_input: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub current_price_step: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price_step: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price_output: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub minimum_stake: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub minimum_stake_stage: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub minimum_stake_accepted: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fee_open: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fee_close: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fee_applied: Option<String>,
    pub intermediates: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state_before: Option<PortfolioBoundaryState>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state_after: Option<PortfolioBoundaryState>,
}
