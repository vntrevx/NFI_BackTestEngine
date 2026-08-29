//! Simulator domain contracts and serialized result types.

mod callback;
mod callback_runtime;
mod executable_callback;
mod execution_event;
mod failures;
mod market;
mod outcome;
mod portfolio_event;
mod programs;
mod settings;
mod state_machine;
mod x7;

pub use callback::{
    Outcome as CallbackOutcome, Phase as CallbackPhase, SemanticEvent as CallbackSemanticEvent,
    Transaction as CallbackTransaction, Visibility as CallbackVisibility,
    CALLBACK_TRACE_SCHEMA_VERSION,
};
pub use callback_runtime::{
    same_candle_exit_winner, Error as CallbackRuntimeError, Runtime as CallbackRuntime,
};
pub use executable_callback::*;
pub use execution_event::*;
pub use failures::*;
pub use market::*;
pub use outcome::*;
pub use portfolio_event::*;
pub use programs::*;
pub use settings::*;
pub use state_machine::*;
pub(crate) use x7::FeatureProjection;
pub use x7::*;
