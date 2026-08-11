use std::collections::BTreeMap;
use std::path::PathBuf;

use nfi_vector_core::alignment::{FrameCatalog, FrameIdentity, NumericFrame, Timeframe};
use nfi_vector_core::mutation::MutationProgram;
use nfi_vector_core::program::IndicatorProgram;
use serde::Deserialize;
use thiserror::Error;

/// A fully verified and decoded complete-native input bundle.
#[derive(Clone, Debug)]
pub struct NativeVectorBundle {
    pub source: SourceSeal,
    pub compile_context: CompileContext,
    pub run: RunContract,
    pub retained_features: FeatureRetention,
    pub pairs: Vec<PairContract>,
    pub indicator_program: IndicatorProgram,
    pub signal_program: MutationProgram,
    pub tag_program: MutationProgram,
    pub frames: FrameCatalog,
    pub futures: Vec<FuturesFrameSet>,
}

/// Static inputs that produced the compiled programs.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceSeal {
    pub strategy_sha256: String,
    pub config_sha256: String,
    pub compiler_source_fingerprint: String,
    pub selected_class: String,
}

/// Runtime context frozen by both mutation programs and the manifest.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompileContext {
    pub run_mode: String,
    pub trading_mode: TradingMode,
}

/// Supported Freqtrade trading modes for the native lane.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum TradingMode {
    Spot,
    Futures,
}

impl TradingMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Spot => "spot",
            Self::Futures => "futures",
        }
    }
}

/// Chronological vector-run semantics supplied to the native executor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunContract {
    pub trading_mode: TradingMode,
    pub timerange_start_ms: i64,
    pub timerange_stop_ms: i64,
    pub startup_candles: usize,
    pub base_timeframe: Timeframe,
    pub source_row_shift: usize,
}

/// Dynamically selected analyzed columns retained for callbacks/simulation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FeatureRetention {
    pub columns: Vec<String>,
    pub fingerprint: String,
}

/// Pair-local metadata and exchange precision/limit contract.
#[derive(Clone, Debug, PartialEq)]
pub struct PairContract {
    pub identity: FrameIdentity,
    pub metadata: BTreeMap<String, String>,
    pub precision: PairPrecision,
    pub limits: PairLimits,
    pub price_steps: Vec<HistoricPriceStep>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PairPrecision {
    pub amount_step: Option<f64>,
    pub price_step: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PairLimits {
    pub minimum_stake: Option<f64>,
    pub minimum_amount: Option<f64>,
    pub minimum_cost: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct HistoricPriceStep {
    pub timestamp_ms: i64,
    pub step: f64,
}

/// Sparse Futures sources remain separate because their pair/timeframe
/// identities can be equal while their semantic roles differ.
#[derive(Clone, Debug, PartialEq)]
pub struct FuturesFrameSet {
    pub pair: String,
    pub funding_rate: NumericFrame,
    pub mark: NumericFrame,
}

/// Fail-closed errors from the complete native manifest boundary.
#[derive(Debug, Error)]
pub enum NativeContractError {
    #[error("cannot read full native vector manifest {path}: {source}")]
    ReadManifest {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid full native vector manifest {path}: {source}")]
    ParseManifest {
        path: PathBuf,
        source: serde_json::Error,
    },
    #[error("invalid full native vector contract: {0}")]
    Invalid(String),
    #[error("cannot resolve full native artifact {role} at {path}: {source}")]
    ResolveArtifact {
        role: String,
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("full native artifact {role} escapes the manifest directory: {path}")]
    EscapedArtifact { role: String, path: PathBuf },
    #[error("cannot hash full native artifact {role} at {path}: {source}")]
    HashArtifact {
        role: String,
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("full native artifact {role} SHA-256 mismatch: expected {expected}, got {actual}")]
    ArtifactDigest {
        role: String,
        expected: String,
        actual: String,
    },
    #[error("invalid full native {role}: {source}")]
    Program {
        role: &'static str,
        source: nfi_vector_core::VectorCoreError,
    },
    #[error("cannot decode full native {role}: {source}")]
    RawFrame {
        role: String,
        source: crate::VectorInputError,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ManifestDocument {
    pub(super) schema_version: String,
    pub(super) source: SourceDocument,
    pub(super) compile_context: CompileContextDocument,
    pub(super) programs: ProgramDocuments,
    pub(super) run: RunDocument,
    pub(super) retained_features: FeatureDocument,
    pub(super) pairs: Vec<PairDocument>,
    pub(super) frames: Vec<FrameDocument>,
    pub(super) futures: Option<Vec<FuturesDocument>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SourceDocument {
    pub(super) strategy_sha256: String,
    pub(super) config_sha256: String,
    pub(super) compiler_source_fingerprint: String,
    pub(super) selected_class: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct CompileContextDocument {
    pub(super) run_mode: String,
    pub(super) trading_mode: TradingMode,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ProgramDocuments {
    pub(super) indicator: ProgramDocument,
    pub(super) signal: ProgramDocument,
    pub(super) tag: ProgramDocument,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ProgramDocument {
    pub(super) artifact: ArtifactDocument,
    pub(super) fingerprint: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ArtifactDocument {
    pub(super) path: PathBuf,
    pub(super) sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RunDocument {
    pub(super) trading_mode: TradingMode,
    pub(super) timerange: TimerangeDocument,
    pub(super) startup_candles: usize,
    pub(super) base_timeframe: String,
    pub(super) source_row_shift: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct TimerangeDocument {
    pub(super) start_ms: i64,
    pub(super) stop_ms: i64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FeatureDocument {
    pub(super) columns: Vec<String>,
    pub(super) fingerprint: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PairDocument {
    pub(super) identity: IdentityDocument,
    pub(super) metadata: BTreeMap<String, String>,
    pub(super) precision: PrecisionDocument,
    pub(super) limits: LimitsDocument,
    pub(super) price_steps: Vec<PriceStepDocument>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct IdentityDocument {
    pub(super) pair: String,
    pub(super) timeframe: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PrecisionDocument {
    pub(super) amount_step: Option<f64>,
    pub(super) price_step: Option<f64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(clippy::struct_field_names)] // Exact public Freqtrade market-limit field names.
pub(super) struct LimitsDocument {
    pub(super) minimum_stake: Option<f64>,
    pub(super) minimum_amount: Option<f64>,
    pub(super) minimum_cost: Option<f64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PriceStepDocument {
    pub(super) timestamp_ms: i64,
    pub(super) step: f64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FrameDocument {
    pub(super) identity: IdentityDocument,
    pub(super) rows: usize,
    pub(super) artifact: ArtifactDocument,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FuturesDocument {
    pub(super) pair: String,
    pub(super) funding_rate: FrameDocument,
    pub(super) mark: FrameDocument,
}

pub(super) struct ValidatedDocument {
    pub(super) source: SourceSeal,
    pub(super) compile_context: CompileContext,
    pub(super) programs: ProgramDocuments,
    pub(super) run: RunContract,
    pub(super) retained_features: FeatureRetention,
    pub(super) pairs: Vec<PairContract>,
    pub(super) frames: Vec<ValidatedFrame>,
    pub(super) futures: Vec<ValidatedFutures>,
}

pub(super) struct ValidatedFrame {
    pub(super) identity: FrameIdentity,
    pub(super) rows: usize,
    pub(super) artifact: ArtifactDocument,
}

pub(super) struct ValidatedFutures {
    pub(super) pair: String,
    pub(super) funding_rate: ValidatedFrame,
    pub(super) mark: ValidatedFrame,
}
