//! Verified columnar input boundary for analyzed strategy vectors.
//!
//! Python remains responsible for running the real strategy's vector methods.
//! This crate reads their immutable Feather output directly, so neither Python
//! nor JSON duplicates every candle and callback feature before simulation.
//! The simulator core intentionally does not depend on Arrow or the filesystem.

mod decode;
mod failures;
mod freqtrade_ohlcv;
mod full_manifest;
#[allow(clippy::module_name_repetitions)] // Public API distinguishes direct and Feather profiles.
mod in_memory;
mod loader;
mod raw_ohlcv;
mod row;
mod schema;
mod values;

pub use failures::VectorInputError;
pub use freqtrade_ohlcv::clean_frame as clean_freqtrade_ohlcv;
pub use freqtrade_ohlcv::{
    execution_positions, prepare_execution_ohlcv, prepare_freqtrade_ohlcv_catalog, ClosedTimerange,
    InclusiveExecutionPositions, PreparedExecutionOhlcv,
};
pub use full_manifest::{
    load_full_native_vector_manifest, retained_feature_fingerprint, CompileContext,
    FeatureRetention, FuturesFrameSet, HistoricPriceStep, NativeContractError, NativeVectorBundle,
    PairContract, PairLimits, PairPrecision, RunContract, SourceSeal, TradingMode,
    FULL_NATIVE_VECTOR_MANIFEST_VERSION,
};
pub use in_memory::{
    assemble_in_memory_vectors, assemble_in_memory_vectors_profiled,
    execute_in_memory_pair_dag_profiled, InMemoryVectorPair, InMemoryVectorProfile,
    VectorPairOptions,
};
pub use loader::{load_vector_manifest, load_vector_manifest_profiled, VectorLoadProfile};
pub use raw_ohlcv::{load_raw_ohlcv_catalog, load_raw_ohlcv_frame, FeatherFrameSource};

/// Version of the compact manifest consumed by this crate.
pub const VECTOR_MANIFEST_SCHEMA_VERSION: &str = "1.2.0";
