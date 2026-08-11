//! Strict, versioned input contract for the complete native vector stage.
//!
//! Unlike the legacy simulator-vector manifest, this document binds the three
//! compiled programs and every raw market-data source before Arrow decoding.
//! Raw frame cleanup remains outside this module: verified files are passed to
//! the non-transforming raw loader without aggregation, resampling, gap fill,
//! or timerange trimming.

mod load;
mod model;
mod validation;

pub use model::{
    CompileContext, FeatureRetention, FuturesFrameSet, HistoricPriceStep, NativeContractError,
    NativeVectorBundle, PairContract, PairLimits, PairOptions, PairPrecision, RunContract,
    SourceSeal, TradingMode,
};
pub use validation::retained_feature_fingerprint;

/// The only complete native-vector manifest accepted by this parser.
pub const FULL_NATIVE_VECTOR_MANIFEST_VERSION: &str = "full-native-vector-manifest-v1";

/// Parse, SHA-bind, validate, and decode a complete-native vector manifest.
///
/// # Errors
///
/// Returns [`NativeContractError`] for an invalid contract, contained-path or
/// digest failure, program identity drift, or raw-frame decode failure.
pub fn load_full_native_vector_manifest(
    path: &std::path::Path,
) -> Result<NativeVectorBundle, NativeContractError> {
    load::load_bundle(path)
}

#[cfg(test)]
mod tests;
