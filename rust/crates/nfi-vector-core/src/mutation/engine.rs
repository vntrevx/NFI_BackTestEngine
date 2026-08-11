mod evaluator;
mod execution;
mod frame;
mod value;

use super::MutationProgram;

pub use execution::{materialize_execution_signals, ExecutionSignals};
pub use frame::MutationFrame;

/// Source-ordered executor for one validated Signal or Tag program.
#[derive(Debug)]
pub struct MutationEngine<'program> {
    pub(super) program: &'program MutationProgram,
}
