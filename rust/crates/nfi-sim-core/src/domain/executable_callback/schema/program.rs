use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{CallbackExpression, CallbackStatement};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Program {
    pub schema_version: String,
    pub identity: ExecutableCallbackIdentity,
    pub entrypoints: BTreeMap<String, ExecutableCallbackEntrypoint>,
    pub registers: Vec<CallbackRegister>,
    pub required_custom_state: Vec<CallbackCustomStateRequirement>,
    pub required_inputs: Vec<CallbackInputRequirement>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutableCallbackIdentity {
    pub callback_contract_file_sha256: String,
    pub callback_contract_fingerprint: String,
    pub callback_execution_ir_fingerprint: String,
    pub program_fingerprint: String,
    pub run_mode: CallbackRunMode,
    pub selected_class_ast_sha256: String,
    pub source_closure: Vec<CallbackSourceRef>,
    pub source_predicates: Vec<CallbackSourcePredicate>,
    pub trading_mode: CallbackTradingMode,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackRunMode {
    Backtest,
    Hyperopt,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackTradingMode {
    Spot,
    Futures,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackSourceRef {
    pub ast_sha256: String,
    pub logical_method_id: String,
    pub logical_owner_id: String,
    pub source_body_sha256: String,
    pub source_id: String,
    pub diagnostic_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackSourcePredicate {
    pub ast_sha256: String,
    pub expression: String,
    pub id: String,
    pub producer_method_id: String,
    pub source_order: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutableCallbackEntrypoint {
    pub accepted_returns: Vec<CallbackAcceptedReturn>,
    pub active: bool,
    pub cadence: CallbackCadence,
    pub exception_fallback: CallbackFallback,
    pub instructions: Vec<CallbackStatement>,
    pub max_steps: usize,
    pub name: String,
    pub order: CallbackOrder,
    pub predicate_ids: Vec<String>,
    pub transaction_policy: CallbackTransactionPolicy,
    pub visibility: CallbackProgramVisibility,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum CallbackCadence {
    OncePerMainCandle,
    PerInitialEntry,
    PerFill,
    PerOpenTradeCandle,
    PerExitCandidate,
    SyntheticLifecycle,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackOrder {
    pub after: Vec<String>,
    pub before: Vec<String>,
    pub phase: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackTransactionPolicy {
    pub ordinary_trade: OrdinaryTradePolicy,
    pub scheduler_prior: PreservePolicy,
    pub shared_custom_state: ExecutedWritePolicy,
    pub strategy_registers: ExecutedWritePolicy,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OrdinaryTradePolicy {
    CommitOnSuccessRollbackOnException,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PreservePolicy {
    Preserve,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ExecutedWritePolicy {
    CommitExecutedWrites,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackProgramVisibility {
    pub callback_dataframe_completed_candle_lag: usize,
    pub signal_row_offset: i64,
    pub successful_state_visible: SuccessfulStateVisibility,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SuccessfulStateVisibility {
    NextCallbackInSchedulerOrder,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackRegister {
    pub id: String,
    pub initial: CallbackExpression,
    pub logical_name_hash: String,
    pub scope: CallbackRegisterScope,
    #[serde(rename = "type")]
    pub value_type: CallbackType,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CallbackRegisterScope {
    StrategyRun,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum CallbackType {
    Bool,
    I64,
    F64,
    String,
    TimestampMs,
    Null,
    List {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        item: Option<Box<Self>>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        max_length: Option<usize>,
    },
    Record {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        fields: Option<Vec<CallbackTypeField>>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CallbackTypeField {
    pub name: String,
    #[serde(rename = "type")]
    pub value_type: CallbackType,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackCustomStateRequirement {
    pub key: String,
    #[serde(rename = "type")]
    pub value_type: Option<CallbackType>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackInputRequirement {
    pub entrypoint: String,
    pub name: String,
    #[serde(rename = "type")]
    pub value_type: CallbackType,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "kebab-case")]
pub enum CallbackAcceptedReturn {
    None,
    FiniteNumber,
    FinitePositiveNumber,
    Zero,
    TruthyAccept,
    FalsyReject,
    PositiveNumber,
    NegativeNumber,
    NumberAndTag,
    False,
    True,
    NonEmptyString,
    LifecycleTransition,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum CallbackReturnClass {
    None,
    Boolean,
    Number,
    String,
    Stake,
    Leverage,
    Stoploss,
    Adjustment,
    ExitReason,
    LifecycleTransition,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackReturn {
    pub class: CallbackReturnClass,
    pub value: Option<CallbackExpression>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackFallback {
    pub class: CallbackReturnClass,
    pub value: Value,
}
