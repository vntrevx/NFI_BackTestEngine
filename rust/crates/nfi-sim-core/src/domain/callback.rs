//! Versioned callback semantic-event contract.

use serde::Serialize;

use super::{CallbackOutcome, CallbackPhase, CallbackTransaction, CallbackVisibility};

pub const CALLBACK_TRACE_SCHEMA_VERSION: &str = "callback-semantic-trace-v1";

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    CandleStart,
    StakeSizing,
    Leverage,
    EntryConfirmation,
    OrderFilled,
    PositionAdjustment,
    CustomStoploss,
    CustomExit,
    ExitConfirmation,
    CandleAfter,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Accepted,
    Rejected,
    Value,
    None,
    Exception,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Transaction {
    Committed,
    RolledBack,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Visibility {
    /// Last closed analyzed row. It is one less than the execution candle.
    pub feature_index: Option<usize>,
    pub order_count: usize,
    pub wallet_available: f64,
    /// Monotonic generation committed by prior callback entrypoints.
    pub custom_state_generation: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SemanticEvent {
    pub schema_version: &'static str,
    pub candle_index: usize,
    pub sequence: usize,
    pub phase: CallbackPhase,
    pub outcome: CallbackOutcome,
    pub transaction: CallbackTransaction,
    pub visibility: CallbackVisibility,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostic: Option<String>,
}
