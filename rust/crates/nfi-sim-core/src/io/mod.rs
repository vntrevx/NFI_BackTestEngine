//! Vector-backed candle storage and simulator JSON input decoding.

mod backing;
mod json;
mod storage;

pub use backing::FileBackedRows;
pub use json::{parse_simulation_input, serialize_simulation_result};
pub use storage::{CandleSeries, CandleSeriesIter, FeatureColumn, FileBackedFeatureKind};

/// Normalized trade-surface contract understood by this workspace.
pub const TRADE_SURFACE_SCHEMA_VERSION: &str = "2.0.0";
/// Bytes before the first feature in one file-backed vector row.
///
/// This is a transport boundary shared with `nfi-vector-io`, not a tuning
/// value. Changing it requires a new row schema and an explicit decoder.
pub const FILE_BACKED_ROW_HEADER_BYTES: usize = 81;
/// Width of one normalized numeric or boolean feature in a file-backed row.
pub const FILE_BACKED_FEATURE_BYTES: usize = std::mem::size_of::<f64>();

// The chronological loop revisits many pair files in round-robin order. One
// buffer per pair retains its sequential read-ahead across those switches,
// avoiding a seek and kernel read for every candle without retaining the full
// multi-year vector in heap memory.
pub(crate) const FILE_BACKED_READ_BUFFER_BYTES: usize = 256 * 1024;
// NFI callback programs can address `previous_candle_1` through
// `previous_candle_5`. Keep that contract in one place so the scalar VM and
// file-backed read window cannot drift apart.
pub(crate) const CALLBACK_FEATURE_LOOKBACK_ROWS: usize = 5;
