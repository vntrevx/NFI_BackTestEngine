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
use nfi_vector_core::alignment::{FrameCatalog, FrameIdentity, SourceLocation, Timeframe};
use nfi_vector_core::mutation::prove_signal_tag_decision_equivalence;
use nfi_vector_core::program::IndicatorProgram;
use nfi_vector_core::VectorCoreError;
use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::full_manifest::{
    decode_verified_frame, load_plan, FuturesFrameSet, NativeContractError, NativeVectorBundle,
    NativeVectorPlan, VerifiedFrameSource, VerifiedFuturesSources,
};
use crate::{
    execute_in_memory_pair_dag_profiled, execute_in_memory_pair_dag_profiled_with_worker_limit,
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
    pub frame_loading_mode: String,
    pub raw_frame_resident_limit: usize,
    pub futures_frame_set_count: usize,
    pub spool_required_upper_bound_bytes: u64,
    pub spool_available_bytes_at_admission: u64,
    pub spool_target_source: &'static str,
    pub spool_cleanup_mode: &'static str,
    pub manifest_declared_raw_rows: u64,
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
    load_full_native_vector_manifest_profiled_inner(path, None)
}

/// Load and execute a strict manifest with an explicit bounded pair worker pool.
///
/// Numeric libraries inside each independent pair remain single-threaded. The
/// worker limit applies only to the outer pair DAG and never to the global
/// chronological wallet loop.
///
/// # Errors
///
/// Returns the same errors as [`load_full_native_vector_manifest_profiled`],
/// plus a fail-closed error for a zero or unbuildable worker limit.
pub fn load_full_native_vector_manifest_profiled_with_worker_limit(
    path: &Path,
    pair_worker_limit: usize,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    load_full_native_vector_manifest_profiled_inner(path, Some(pair_worker_limit))
}

fn load_full_native_vector_manifest_profiled_inner(
    path: &Path,
    pair_worker_limit: Option<usize>,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    let manifest_sha256 =
        sha256_file(path).map_err(|source| FullNativePipelineError::ManifestHash {
            path: path.to_path_buf(),
            source,
        })?;
    let plan = load_plan(path)?;
    execute_plan(plan, Some(manifest_sha256), pair_worker_limit)
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
    execute_bundle(bundle, None, None)
}

fn execute_bundle(
    bundle: NativeVectorBundle,
    manifest_sha256: Option<String>,
    pair_worker_limit: Option<usize>,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    validate_funding_contract(&bundle)?;
    prove_signal_tag_decision_equivalence(&bundle.signal_program, &bundle.tag_program)
        .map_err(VectorInputError::from)?;
    let admission = crate::spool_admission::admit(
        &bundle.run,
        bundle.pairs.len(),
        bundle.retained_features.columns.len(),
    )?;
    let declared_raw_rows = bundle_declared_raw_rows(&bundle)?;
    let identity = ProfileIdentity::from_bundle(&bundle, manifest_sha256, declared_raw_rows);
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
    let execute = |pair| {
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
    };
    let (input, transport) = if let Some(limit) = pair_worker_limit {
        execute_in_memory_pair_dag_profiled_with_worker_limit(config, pairs, limit, execute)?
    } else {
        execute_in_memory_pair_dag_profiled(config, pairs, execute)?
    };
    Ok((input, identity.finish(transport, admission)?))
}

fn execute_plan(
    plan: NativeVectorPlan,
    manifest_sha256: Option<String>,
    pair_worker_limit: Option<usize>,
) -> Result<(SimulationInput, FullNativeVectorProfile), FullNativePipelineError> {
    validate_plan_funding_contract(&plan)?;
    prove_signal_tag_decision_equivalence(&plan.signal_program, &plan.tag_program)
        .map_err(VectorInputError::from)?;
    let admission = crate::spool_admission::admit(
        &plan.run,
        plan.pairs.len(),
        plan.retained_features.columns.len(),
    )?;
    let declared_raw_rows = plan_declared_raw_rows(&plan)?;
    let literal_frames = literal_frame_identities(&plan.indicator_program)?;
    let identity =
        ProfileIdentity::from_plan(&plan, manifest_sha256, &literal_frames, declared_raw_rows);
    let NativeVectorPlan {
        config,
        run,
        retained_features,
        pairs,
        indicator_program,
        signal_program,
        tag_program,
        frames,
        futures,
        ..
    } = plan;
    let execute = |pair: crate::PairContract| {
        let catalog = prepare_pair_catalog(&frames, &literal_frames, &pair.identity.pair, &run)?;
        let pair_futures = decode_pair_futures(&futures, &pair.identity.pair)?;
        stage::execute_pair(
            &indicator_program,
            &signal_program,
            &tag_program,
            &catalog,
            &run,
            &retained_features.columns,
            &pair_futures,
            pair,
        )
    };
    let (input, transport) = if let Some(limit) = pair_worker_limit {
        execute_in_memory_pair_dag_profiled_with_worker_limit(config, pairs, limit, execute)?
    } else {
        execute_in_memory_pair_dag_profiled(config, pairs, execute)?
    };
    Ok((input, identity.finish(transport, admission)?))
}

fn bundle_declared_raw_rows(bundle: &NativeVectorBundle) -> Result<u64, VectorInputError> {
    let source = SourceLocation::new("spool-admission", "native/full_pipeline", 0, 0);
    let base_rows = bundle.frames.identities().map(|identity| {
        bundle
            .frames
            .lookup(identity, &source)
            .map(|frame| frame.timestamps_ms.len())
    });
    let futures_rows = bundle.futures.iter().flat_map(|frames| {
        [
            frames.funding_rate.timestamps_ms.len(),
            frames.mark.timestamps_ms.len(),
        ]
        .into_iter()
        .map(Ok)
    });
    checked_declared_rows(base_rows.chain(futures_rows))
}

fn plan_declared_raw_rows(plan: &NativeVectorPlan) -> Result<u64, VectorInputError> {
    let base_rows = plan.frames.iter().map(|frame| Ok(frame.rows));
    let futures_rows = plan.futures.iter().flat_map(|frames| {
        [frames.funding_rate.rows, frames.mark.rows]
            .into_iter()
            .map(Ok)
    });
    checked_declared_rows(base_rows.chain(futures_rows))
}

fn checked_declared_rows(
    rows: impl IntoIterator<Item = Result<usize, VectorCoreError>>,
) -> Result<u64, VectorInputError> {
    rows.into_iter().try_fold(0_u64, |total, rows| {
        let rows = u64::try_from(rows?)
            .map_err(|_| VectorInputError::SpoolBound("raw row count exceeds u64".to_owned()))?;
        total.checked_add(rows).ok_or_else(|| {
            VectorInputError::SpoolBound("aggregate raw row count exceeds u64".to_owned())
        })
    })
}

fn prepare_pair_catalog(
    sources: &[VerifiedFrameSource],
    literal_frames: &std::collections::BTreeSet<FrameIdentity>,
    pair: &str,
    run: &crate::RunContract,
) -> Result<FrameCatalog, VectorInputError> {
    let entries = sources
        .iter()
        .filter(|source| source.identity.pair == pair || literal_frames.contains(&source.identity))
        .enumerate()
        .map(|(index, source)| {
            let frame = decode_verified_frame(source, &format!("pair-local raw frame {index}"))
                .map_err(|error| pipeline_invalid(error.to_string()))?;
            Ok((source.identity.clone(), frame))
        })
        .collect::<Result<Vec<_>, VectorInputError>>()?;
    let raw = FrameCatalog::new(entries)?;
    let timerange = format!("{}-{}", run.timerange_start_ms, run.timerange_stop_ms);
    prepare_freqtrade_ohlcv_catalog(&raw, &timerange, run.startup_candles)
}

fn decode_pair_futures(
    sources: &[VerifiedFuturesSources],
    pair: &str,
) -> Result<Vec<FuturesFrameSet>, VectorInputError> {
    sources
        .iter()
        .filter(|source| source.pair == pair)
        .enumerate()
        .map(|(index, source)| {
            Ok(FuturesFrameSet {
                pair: source.pair.clone(),
                funding_rate: decode_verified_frame(
                    &source.funding_rate,
                    &format!("pair-local funding frame {index}"),
                )
                .map_err(|error| pipeline_invalid(error.to_string()))?,
                mark: decode_verified_frame(
                    &source.mark,
                    &format!("pair-local mark frame {index}"),
                )
                .map_err(|error| pipeline_invalid(error.to_string()))?,
            })
        })
        .collect()
}

fn literal_frame_identities(
    program: &IndicatorProgram,
) -> Result<std::collections::BTreeSet<FrameIdentity>, VectorInputError> {
    let mut identities = std::collections::BTreeSet::new();
    for node in program
        .nodes
        .iter()
        .filter(|node| node.op == "frame-source")
    {
        let Some(pair) = node
            .parameters
            .get("pair")
            .and_then(serde_json::Value::as_object)
        else {
            return Err(pipeline_invalid(format!(
                "frame-source {} has no pair binding",
                node.id
            )));
        };
        match pair.get("kind").and_then(serde_json::Value::as_str) {
            Some("metadata") => continue,
            Some("literal") => {}
            _ => {
                return Err(pipeline_invalid(format!(
                    "frame-source {} has an unsupported pair binding",
                    node.id
                )))
            }
        }
        let value = pair
            .get("value")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| {
                pipeline_invalid(format!("frame-source {} has no literal pair", node.id))
            })?;
        let timeframe = node
            .parameters
            .get("timeframe")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| {
                pipeline_invalid(format!("frame-source {} has no timeframe", node.id))
            })?;
        identities.insert(FrameIdentity::new(value, Timeframe::parse(timeframe)?)?);
    }
    Ok(identities)
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

fn validate_plan_funding_contract(plan: &NativeVectorPlan) -> Result<(), VectorInputError> {
    let futures_mode = plan.run.trading_mode == crate::TradingMode::Futures;
    if plan.config.is_futures != futures_mode {
        return Err(pipeline_invalid(
            "embedded config is_futures differs from the full native run mode",
        ));
    }
    if !futures_mode {
        if !plan.futures.is_empty() || plan.config.funding_fee_interval_ms.is_some() {
            return Err(pipeline_invalid(
                "Spot execution cannot declare funding frames or an interval",
            ));
        }
        return Ok(());
    }

    let mut interval_ms = None;
    let mut seen_pairs = std::collections::BTreeSet::new();
    for pair in &plan.pairs {
        if !pair.options.include_funding {
            return Err(pipeline_invalid(format!(
                "Futures pair {} does not enable funding",
                pair.identity.pair
            )));
        }
        let matching = plan
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
    if plan
        .futures
        .iter()
        .any(|frames| !seen_pairs.contains(frames.pair.as_str()))
    {
        return Err(pipeline_invalid(
            "funding descriptor has no matching execution pair",
        ));
    }
    if plan.config.funding_fee_interval_ms != interval_ms {
        return Err(pipeline_invalid(format!(
            "embedded funding_fee_interval_ms {:?} differs from manifest-derived {:?}",
            plan.config.funding_fee_interval_ms, interval_ms
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
    frame_loading_mode: String,
    raw_frame_resident_limit: usize,
    futures_frame_set_count: usize,
    manifest_declared_raw_rows: u64,
}

impl ProfileIdentity {
    fn from_bundle(
        bundle: &NativeVectorBundle,
        manifest_sha256: Option<String>,
        manifest_declared_raw_rows: u64,
    ) -> Self {
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
            frame_loading_mode: "preloaded-catalog".to_owned(),
            raw_frame_resident_limit: bundle.frames.len(),
            futures_frame_set_count: bundle.futures.len(),
            manifest_declared_raw_rows,
        }
    }

    fn from_plan(
        plan: &NativeVectorPlan,
        manifest_sha256: Option<String>,
        literal_frames: &std::collections::BTreeSet<FrameIdentity>,
        manifest_declared_raw_rows: u64,
    ) -> Self {
        Self {
            manifest_sha256,
            strategy_sha256: plan.source.strategy_sha256.clone(),
            config_sha256: plan.source.config_sha256.clone(),
            compiler_source_fingerprint: plan.source.compiler_source_fingerprint.clone(),
            selected_class: plan.source.selected_class.clone(),
            strategy_source_mode: plan.source_execution.strategy_source_mode.clone(),
            populate_methods_executed: plan.source_execution.populate_methods_executed,
            runtime_mode: plan.source_execution.runtime_mode.clone(),
            program_fingerprints: BTreeMap::from([
                (
                    "indicator".to_owned(),
                    plan.indicator_program.fingerprint.clone(),
                ),
                ("signal".to_owned(), plan.signal_program.fingerprint.clone()),
                ("tag".to_owned(), plan.tag_program.fingerprint.clone()),
            ]),
            retained_feature_fingerprint: plan.retained_features.fingerprint.clone(),
            trading_mode: plan.run.trading_mode.as_str().to_owned(),
            base_timeframe: plan.run.base_timeframe.as_str().to_owned(),
            source_row_shift: plan.run.source_row_shift,
            pair_identities: plan
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
            raw_frame_count: plan.frames.len(),
            frame_loading_mode: "pair-local-streaming".to_owned(),
            raw_frame_resident_limit: plan
                .pairs
                .iter()
                .map(|pair| {
                    plan.frames
                        .iter()
                        .filter(|source| {
                            source.identity.pair == pair.identity.pair
                                || literal_frames.contains(&source.identity)
                        })
                        .count()
                })
                .max()
                .unwrap_or(0),
            futures_frame_set_count: plan.futures.len(),
            manifest_declared_raw_rows,
        }
    }

    fn finish(
        self,
        transport: InMemoryVectorProfile,
        admission: crate::spool_admission::SpoolAdmission,
    ) -> Result<FullNativeVectorProfile, VectorInputError> {
        if transport.file_backed_bytes > admission.required_upper_bound_bytes {
            return Err(VectorInputError::SpoolBound(format!(
                "actual file-backed bytes {} exceed admitted upper bound {}",
                transport.file_backed_bytes, admission.required_upper_bound_bytes
            )));
        }
        Ok(FullNativeVectorProfile {
            schema_version: "1.2.0",
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
            frame_loading_mode: self.frame_loading_mode,
            raw_frame_resident_limit: self.raw_frame_resident_limit,
            futures_frame_set_count: self.futures_frame_set_count,
            spool_required_upper_bound_bytes: admission.required_upper_bound_bytes,
            spool_available_bytes_at_admission: admission.available_bytes,
            spool_target_source: admission.target_source,
            spool_cleanup_mode: admission.cleanup_mode,
            manifest_declared_raw_rows: self.manifest_declared_raw_rows,
            transport,
        })
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
