//! Errors emitted by the verified vector-input boundary.

use std::path::PathBuf;

use arrow2::datatypes::DataType;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum VectorInputError {
    #[error("cannot read vector manifest {path}: {source}")]
    ReadManifest {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid vector manifest {path}: {source}")]
    ParseManifest {
        path: PathBuf,
        source: serde_json::Error,
    },
    #[error("unsupported vector manifest schema {0:?}")]
    ManifestSchema(String),
    #[error("vector manifest must contain at least one pair")]
    EmptyPairs,
    #[error("duplicate or empty pair in vector manifest: {0:?}")]
    InvalidPair(String),
    #[error("pair {pair:?} has duplicate or empty feature column {column:?}")]
    InvalidFeatureName { pair: String, column: String },
    #[error("pair {pair:?} vector path must be relative to the manifest: {path}")]
    AbsoluteVectorPath { pair: String, path: PathBuf },
    #[error("pair {pair:?} vector path escapes the manifest directory: {path}")]
    EscapedVectorPath { pair: String, path: PathBuf },
    #[error("cannot resolve pair {pair:?} vector {path}: {source}")]
    ResolveVector {
        pair: String,
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("pair {pair:?} vector format must be \"feather-ipc\", got {format:?}")]
    VectorFormat { pair: String, format: String },
    #[error("pair {pair:?} vector SHA-256 is invalid: {sha256:?}")]
    InvalidSha256 { pair: String, sha256: String },
    #[error("cannot hash pair {pair:?} vector {path}: {source}")]
    HashVector {
        pair: String,
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("pair {pair:?} vector SHA-256 mismatch: expected {expected}, got {actual}")]
    VectorHash {
        pair: String,
        expected: String,
        actual: String,
    },
    #[error("cannot open pair {pair:?} Feather file {path}: {source}")]
    OpenFeather {
        pair: String,
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("cannot create file-backed rows for pair {pair:?}: {source}")]
    FileBacking {
        pair: String,
        source: std::io::Error,
    },
    #[error("invalid pair {pair:?} Feather file {path}: {message}")]
    Feather {
        pair: String,
        path: PathBuf,
        message: String,
    },
    #[error("pair {pair:?} Feather file is missing column {column:?}")]
    MissingColumn { pair: String, column: String },
    #[error("pair {pair:?} Feather column {column:?} has type {actual:?}; expected {expected}")]
    ColumnType {
        pair: String,
        column: String,
        actual: Box<DataType>,
        expected: &'static str,
    },
    #[error("pair {pair:?} Feather column {column:?} contains null at row {row}")]
    NullValue {
        pair: String,
        column: String,
        row: usize,
    },
    #[error(
        "pair {pair:?} Feather row count differs from manifest: expected {expected}, got {actual}"
    )]
    RowCount {
        pair: String,
        expected: usize,
        actual: usize,
    },
    #[error("pair {pair:?} execution_start_index {index} is outside its {rows} vector rows")]
    ExecutionStart {
        pair: String,
        index: usize,
        rows: usize,
    },
}
