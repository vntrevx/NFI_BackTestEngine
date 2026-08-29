//! Strict executable-callback-program-v1 wire contract.

#[path = "executable_callback/fingerprint.rs"]
mod fingerprint;
#[path = "executable_callback/schema.rs"]
mod schema;
#[path = "executable_callback/validation.rs"]
mod validation;
#[path = "executable_callback/validation_tree.rs"]
mod validation_tree;

pub use crate::execution::executable_callback::{
    CallbackDeltaOperation, CallbackInvocation, CallbackObservation, CallbackProgramException,
    CallbackProgramResult, CallbackProgramRuntime, CallbackProgramTransaction, CallbackTypedDelta,
    EXECUTABLE_CALLBACK_TRACE_SCHEMA_VERSION,
};
pub use fingerprint::*;
pub use schema::*;
pub use validation::*;
pub(crate) use validation_tree::matches_type as callback_value_matches_type;
