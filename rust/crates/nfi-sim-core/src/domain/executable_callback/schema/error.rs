use thiserror::Error;

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum Error {
    #[error("InvalidExecutableCallbackProgram: {reason}")]
    InvalidExecutableCallbackProgram { reason: String },
    #[error("ExecutableCallbackIdentityMismatch: {field}")]
    ExecutableCallbackIdentityMismatch { field: String },
    #[error("ExecutableCallbackInvalidTransition source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackInvalidTransition {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackInvalidReturn source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackInvalidReturn {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackRegisterType source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackRegisterType {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackStepLimit source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackStepLimit {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackMissingInput source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackMissingInput {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackObservation source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackObservation {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
    #[error("ExecutableCallbackTransaction source_id={source_id} callback={callback} instruction={instruction_id:?} timestamp={timestamp_ms}")]
    ExecutableCallbackTransaction {
        source_id: String,
        callback: String,
        instruction_id: Option<String>,
        timestamp_ms: i64,
    },
}
