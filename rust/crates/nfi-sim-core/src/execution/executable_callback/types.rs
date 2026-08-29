use std::collections::BTreeMap;

use serde::Serialize;
use serde_json::Value;

use crate::domain::CallbackReturnClass;

pub const EXECUTABLE_CALLBACK_TRACE_SCHEMA_VERSION: &str = "callback-semantic-trace-v2";

#[derive(Debug, Clone, PartialEq)]
pub struct CallbackInvocation {
    pub callback: String,
    pub timestamp_ms: i64,
    pub inputs: BTreeMap<String, Value>,
    pub trade: BTreeMap<String, Value>,
    pub order: BTreeMap<String, Value>,
    pub candle: BTreeMap<String, Value>,
    pub wallet: BTreeMap<String, Value>,
}

impl CallbackInvocation {
    #[must_use]
    pub fn new(
        callback: impl Into<String>,
        timestamp_ms: i64,
        inputs: BTreeMap<String, Value>,
    ) -> Self {
        Self {
            callback: callback.into(),
            timestamp_ms,
            inputs,
            trade: BTreeMap::new(),
            order: BTreeMap::new(),
            candle: BTreeMap::new(),
            wallet: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackProgramTransaction {
    Committed,
    RolledBack,
    Fallback,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CallbackTypedDelta {
    pub operation: CallbackDeltaOperation,
    pub key: String,
    pub before: Option<Value>,
    pub after: Option<Value>,
    pub producer_instruction_id: String,
    pub predicate_ids: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackDeltaOperation {
    Set,
    Delete,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CallbackObservation {
    pub channel: String,
    pub payload: Value,
    pub canonical_sha256: String,
    pub producer_instruction_id: String,
    pub predicate_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CallbackProgramException {
    pub class: String,
    pub diagnostic: String,
    pub instruction_id: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CallbackProgramResult {
    pub schema_version: &'static str,
    pub callback_contract_fingerprint: String,
    pub callback_execution_ir_fingerprint: String,
    pub callback_name: String,
    pub source_id: String,
    pub program_fingerprint: String,
    pub return_class: CallbackReturnClass,
    pub return_value: Option<Value>,
    pub transaction: CallbackProgramTransaction,
    pub exception: Option<CallbackProgramException>,
    pub predicate_ids: Vec<String>,
    pub register_deltas: Vec<CallbackTypedDelta>,
    pub custom_state_deltas: Vec<CallbackTypedDelta>,
    pub observations: Vec<CallbackObservation>,
    pub register_before_fingerprint: String,
    pub register_after_fingerprint: String,
    pub custom_state_fingerprint: String,
}
