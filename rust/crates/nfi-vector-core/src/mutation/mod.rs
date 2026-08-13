//! Exact source-ordered execution for `signal-program-v1` and `tag-program-v1`.

mod engine;
mod equivalence;
mod model;
mod validation;

#[cfg(test)]
mod tests;

pub use engine::{materialize_execution_signals, ExecutionSignals, MutationEngine, MutationFrame};
pub use equivalence::prove_signal_tag_decision_equivalence;
pub use model::{MutationEntrypoint, MutationProgram};
