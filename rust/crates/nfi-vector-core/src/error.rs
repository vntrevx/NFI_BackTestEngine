//! Precise fail-closed errors for vector program validation and execution.

use thiserror::Error;

/// Errors produced before or during deterministic vector execution.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum VectorCoreError {
    /// The serialized causal program violates its structural contract.
    #[error("invalid indicator program: {0}")]
    InvalidProgram(String),
    /// A requested final column is not produced by the program.
    #[error("indicator program does not produce requested output: {0}")]
    MissingOutput(String),
    /// Stateful execution was configured with an invalid bound.
    #[error("invalid vector state: {0}")]
    InvalidState(String),
    /// A projected input column is absent.
    #[error("required vector column is missing: {0}")]
    MissingColumn(String),
    /// A column has a different Arrow type from its declared program type.
    #[error("vector column {column} has type {actual}; expected {expected}")]
    ColumnType {
        column: String,
        actual: String,
        expected: &'static str,
    },
    /// One Arrow array differs from the record batch row count.
    #[error("vector column {column} has {actual} rows; expected {expected}")]
    ColumnLength {
        column: String,
        actual: usize,
        expected: usize,
    },
    /// An opcode has no exact kernel in the current engine version.
    #[error("unsupported vector opcode {opcode} at {location}")]
    UnsupportedOpcode { opcode: String, location: String },
    /// An otherwise valid node cannot be evaluated with the supplied values.
    #[error("vector node {node} failed: {message}")]
    Execution { node: String, message: String },
    /// A sink rejected an inconsistent final batch.
    #[error("invalid vector output batch: {0}")]
    InvalidOutput(String),
}
