//! Safe parsing, validation, and planning for `indicator-program-v1`.
//!
//! Opcode parameters remain generic JSON. This module establishes the
//! structural and causal contract; selecting exact execution kernels remains
//! the engine's responsibility.

mod model;
mod plan;
pub(crate) mod validation;

#[cfg(test)]
mod tests;

use crate::VectorCoreError;
pub use model::{
    ExecutionPlan, FunctionParameter, IndicatorProgram, Lookback, ProgramFunction, ProgramNode,
    ProgramSource, SourceLocation, INDICATOR_PROGRAM_VERSION,
};

pub(super) fn invalid_program(message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::InvalidProgram(message.into())
}
