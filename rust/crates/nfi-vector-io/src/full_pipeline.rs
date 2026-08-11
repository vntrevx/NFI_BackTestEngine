//! Complete manifest-to-simulator native vector pipeline.
//!
//! This is the production handoff between the SHA-bound manifest, exact
//! Freqtrade OHLCV preparation, the three independent vector programs, and the
//! existing in-memory simulator transport. It never executes strategy Python.

mod stage;

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

use nfi_sim_core::SimulationInput;
use nfi_vector_core::VectorCoreError;
use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::full_manifest::{NativeContractError, NativeVectorBundle};
use crate::{
    execute_in_memory_pair_dag_profiled, load_full_native_vector_manifest,
    prepare_freqtrade_ohlcv_catalog, InMemoryVectorProfile, VectorInputError,
};

/// Evidence-friendly identity and timings for one complete native vector run.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct FullNativeVectorProfile {
    pub schema_version: &'static str,
    pub manifest_sha256: Option<String>,
    pub strategy_sha256: String,
    pub config_sha256: String,
    pub compiler_source_fingerprint: String,
    pub selected_class: String,
    pub strategy_source_mode: String,
    pub populate_methods_executed: bool,
    pub runtime_mode: String,
    pub program_fingerprints: BTreeMap<String, String>,
    pub retained_feature_fingerprint: String,
    pub trading_mode: String,
    pub base_timeframe: String,
    pub source_row_shift: usize,
    pub pair_identities: Vec<String>,
    pub raw_frame_count: usize,
    pub futures_frame_set_count: usize,
    pub transport: InMemoryVectorProfile,
}

/// Fail-closed error from manifest loading or complete native execution.
#[derive(Debug, Error)]
pub enum FullNativePipelineError {
    #[error(transparent)]
    Manifest(#[from] NativeContractError),
    #[error(transparent)]
    VectorInput(#[from] VectorInputError),
    #[error("cannot hash full native manifest {path}: {source}")]
    ManifestHash {
        path: PathBuf,
        source: std::io::Error,
    },
}

/// Load a strict manifest and execute its complete vector pipeline in Rust.
///
/// # Errors
///
/// Returns a manifest, program, frame-preparation, funding, mutation, slicing,
/// or in-memory transport error without returning a partial simulator input.
pub fn load_full_native_vector_manifest_profiled(
    path: &Path,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    let manifest_sha256 =
        sha256_file(path).map_err(|source| FullNativePipelineError::ManifestHash {
            path: path.to_path_buf(),
            source,
        })?;
    let bundle = load_full_native_vector_manifest(path)?;
    execute_bundle(bundle, Some(manifest_sha256))
}

/// Execute an already verified bundle without strategy Python.
///
/// This entrypoint is useful for focused tests and callers that retain a
/// verified bundle in memory. Its profile intentionally has no manifest hash.
///
/// # Errors
///
/// Returns the same vector and transport failures as
/// [`load_full_native_vector_manifest_profiled`].
pub fn execute_full_native_vector_bundle_profiled(
    bundle: NativeVectorBundle,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    execute_bundle(bundle, None)
}

fn execute_bundle(
    bundle: NativeVectorBundle,
    manifest_sha256: Option<String>,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    validate_funding_contract(&bundle)?;
    let identity = ProfileIdentity::from_bundle(&bundle, manifest_sha256);
    let timerange = format!(
        "{}-{}",
        bundle.run.timerange_start_ms, bundle.run.timerange_stop_ms
    );
    let prepared =
        prepare_freqtrade_ohlcv_catalog(&bundle.frames, &timerange, bundle.run.startup_candles)?;
    let NativeVectorBundle {
        config,
        run,
        retained_features,
        pairs,
        indicator_program,
        signal_program,
        tag_program,
        futures,
        ..
    } = bundle;
    let (input, transport) = execute_in_memory_pair_dag_profiled(config, pairs, |pair| {
        stage::execute_pair(
            &indicator_program,
            &signal_program,
            &tag_program,
            &prepared,
            &run,
            &retained_features.columns,
            &futures,
            pair,
        )
    })?;
    Ok((input, identity.finish(transport)))
}

fn validate_funding_contract(bundle: &NativeVectorBundle) -> Result<(), VectorInputError> {
    let futures_mode = bundle.run.trading_mode == crate::TradingMode::Futures;
    if bundle.config.is_futures != futures_mode {
        return Err(pipeline_invalid(
            "embedded config is_futures differs from the full native run mode",
        ));
    }
    if !futures_mode {
        if !bundle.futures.is_empty() || bundle.config.funding_fee_interval_ms.is_some() {
            return Err(pipeline_invalid(
                "Spot execution cannot declare funding frames or an interval",
            ));
        }
        return Ok(());
    }

    let mut interval_ms = None;
    let mut seen_pairs = std::collections::BTreeSet::new();
    for pair in &bundle.pairs {
        if !pair.options.include_funding {
            return Err(pipeline_invalid(format!(
                "Futures pair {} does not enable funding",
                pair.identity.pair
            )));
        }
        let matching = bundle
            .futures
            .iter()
            .filter(|frames| frames.pair == pair.identity.pair)
            .collect::<Vec<_>>();
        let [frames] = matching.as_slice() else {
            return Err(pipeline_invalid(format!(
                "Futures execution has no unique funding descriptor for {}",
                pair.identity.pair
            )));
        };
        let actual = frames
            .funding_rate
            .identity
            .timeframe
            .resample_duration_ms();
        if interval_ms.is_some_and(|expected| expected != actual) {
            return Err(pipeline_invalid(
                "Futures pairs declare different funding intervals",
            ));
        }
        interval_ms = Some(actual);
        seen_pairs.insert(pair.identity.pair.as_str());
    }
    if bundle
        .futures
        .iter()
        .any(|frames| !seen_pairs.contains(frames.pair.as_str()))
    {
        return Err(pipeline_invalid(
            "funding descriptor has no matching execution pair",
        ));
    }
    if bundle.config.funding_fee_interval_ms != interval_ms {
        return Err(pipeline_invalid(format!(
            "embedded funding_fee_interval_ms {:?} differs from manifest-derived {:?}",
            bundle.config.funding_fee_interval_ms, interval_ms
        )));
    }
    Ok(())
}

fn pipeline_invalid(message: impl Into<String>) -> VectorInputError {
    VectorCoreError::InvalidProgram(message.into()).into()
}

struct ProfileIdentity {
    manifest_sha256: Option<String>,
    strategy_sha256: String,
    config_sha256: String,
    compiler_source_fingerprint: String,
    selected_class: String,
    strategy_source_mode: String,
    populate_methods_executed: bool,
    runtime_mode: String,
    program_fingerprints: BTreeMap<String, String>,
    retained_feature_fingerprint: String,
    trading_mode: String,
    base_timeframe: String,
    source_row_shift: usize,
    pair_identities: Vec<String>,
    raw_frame_count: usize,
    futures_frame_set_count: usize,
}

impl ProfileIdentity {
    fn from_bundle(bundle: &NativeVectorBundle, manifest_sha256: Option<String>) -> Self {
        Self {
            manifest_sha256,
            strategy_sha256: bundle.source.strategy_sha256.clone(),
            config_sha256: bundle.source.config_sha256.clone(),
            compiler_source_fingerprint: bundle.source.compiler_source_fingerprint.clone(),
            selected_class: bundle.source.selected_class.clone(),
            strategy_source_mode: bundle.source_execution.strategy_source_mode.clone(),
            populate_methods_executed: bundle.source_execution.populate_methods_executed,
            runtime_mode: bundle.source_execution.runtime_mode.clone(),
            program_fingerprints: BTreeMap::from([
                (
                    "indicator".to_owned(),
                    bundle.indicator_program.fingerprint.clone(),
                ),
                (
                    "signal".to_owned(),
                    bundle.signal_program.fingerprint.clone(),
                ),
                ("tag".to_owned(), bundle.tag_program.fingerprint.clone()),
            ]),
            retained_feature_fingerprint: bundle.retained_features.fingerprint.clone(),
            trading_mode: bundle.run.trading_mode.as_str().to_owned(),
            base_timeframe: bundle.run.base_timeframe.as_str().to_owned(),
            source_row_shift: bundle.run.source_row_shift,
            pair_identities: bundle
                .pairs
                .iter()
                .map(|pair| {
                    format!(
                        "{}|{}",
                        pair.identity.pair,
                        pair.identity.timeframe.as_str()
                    )
                })
                .collect(),
            raw_frame_count: bundle.frames.len(),
            futures_frame_set_count: bundle.futures.len(),
        }
    }

    fn finish(self, transport: InMemoryVectorProfile) -> FullNativeVectorProfile {
        FullNativeVectorProfile {
            schema_version: "1.0.0",
            manifest_sha256: self.manifest_sha256,
            strategy_sha256: self.strategy_sha256,
            config_sha256: self.config_sha256,
            compiler_source_fingerprint: self.compiler_source_fingerprint,
            selected_class: self.selected_class,
            strategy_source_mode: self.strategy_source_mode,
            populate_methods_executed: self.populate_methods_executed,
            runtime_mode: self.runtime_mode,
            program_fingerprints: self.program_fingerprints,
            retained_feature_fingerprint: self.retained_feature_fingerprint,
            trading_mode: self.trading_mode,
            base_timeframe: self.base_timeframe,
            source_row_shift: self.source_row_shift,
            pair_identities: self.pair_identities,
            raw_frame_count: self.raw_frame_count,
            futures_frame_set_count: self.futures_frame_set_count,
            transport,
        }
    }
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024].into_boxed_slice();
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests;
