//! Exact source-ordered execution for `signal-program-v1` and `tag-program-v1`.

mod engine;
mod model;
mod validation;

#[cfg(test)]
mod tests;

pub use engine::{materialize_execution_signals, ExecutionSignals, MutationEngine, MutationFrame};
pub use model::{MutationEntrypoint, MutationProgram};
