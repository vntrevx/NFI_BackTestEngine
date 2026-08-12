use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

use nfi_vector_core::alignment::{FrameCatalog, NumericFrame, SourceLocation};
use nfi_vector_core::mutation::MutationProgram;
use nfi_vector_core::program::IndicatorProgram;
use sha2::{Digest, Sha256};

use super::model::{
    ArtifactDocument, FuturesFrameSet, ManifestDocument, NativeContractError, NativeVectorBundle,
    NativeVectorPlan, ValidatedDocument, ValidatedFrame, ValidatedFutures, VerifiedFrameSource,
    VerifiedFuturesSources,
};
use super::validation::{invalid, validate_document, validate_program_identity};
use crate::{load_raw_ohlcv_frame, FeatherFrameSource};

/// Parse, bind, and decode one strict complete-native manifest.
///
/// Artifact paths are resolved below the canonical manifest directory and all
/// digests are checked before any program parsing or Feather decoding. Program
/// fingerprints and compile context are then matched before raw frames are
/// opened by Arrow.
///
/// # Errors
///
/// Returns [`NativeContractError`] for unknown fields, invalid/duplicate
/// identities, path escape, digest drift, program/context drift, or raw-frame
/// schema/decode failure.
pub(super) fn load_bundle(path: &Path) -> Result<NativeVectorBundle, NativeContractError> {
    let plan = load_plan(path)?;
    let frames = decode_catalog(&plan.frames)?;
    let futures = decode_futures(&plan.futures)?;
    Ok(NativeVectorBundle {
        source: plan.source,
        source_execution: plan.source_execution,
        config: plan.config,
        compile_context: plan.compile_context,
        run: plan.run,
        retained_features: plan.retained_features,
        pairs: plan.pairs,
        indicator_program: plan.indicator_program,
        signal_program: plan.signal_program,
        tag_program: plan.tag_program,
        frames,
        futures,
    })
}

pub(crate) fn load_plan(path: &Path) -> Result<NativeVectorPlan, NativeContractError> {
    let validated = validate_document(read_document(path)?)?;
    let verified = verify_all_artifacts(path, &validated)?;
    let indicator_program = parse_indicator(&verified.indicator.path)?;
    let signal_program = parse_mutation("signal program", &verified.signal.path)?;
    let tag_program = parse_mutation("tag program", &verified.tag.path)?;
    validate_program_identity(
        &validated,
        &indicator_program,
        &signal_program,
        &tag_program,
    )?;
    let frames = bind_frame_sources(path, &validated.frames, verified.frames);
    let futures = bind_futures_sources(path, &validated.futures, verified.futures);
    Ok(NativeVectorPlan {
        source: validated.source,
        source_execution: validated.source_execution,
        config: validated.config,
        compile_context: validated.compile_context,
        run: validated.run,
        retained_features: validated.retained_features,
        pairs: validated.pairs,
        indicator_program,
        signal_program,
        tag_program,
        frames,
        futures,
    })
}

struct VerifiedArtifact {
    path: PathBuf,
}

struct VerifiedArtifacts {
    indicator: VerifiedArtifact,
    signal: VerifiedArtifact,
    tag: VerifiedArtifact,
    frames: Vec<VerifiedArtifact>,
    futures: Vec<(VerifiedArtifact, VerifiedArtifact)>,
}

fn read_document(path: &Path) -> Result<ManifestDocument, NativeContractError> {
    let encoded = fs::read(path).map_err(|source| NativeContractError::ReadManifest {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_slice(&encoded).map_err(|source| NativeContractError::ParseManifest {
        path: path.to_path_buf(),
        source,
    })
}

fn verify_all_artifacts(
    manifest_path: &Path,
    document: &ValidatedDocument,
) -> Result<VerifiedArtifacts, NativeContractError> {
    let directory = manifest_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .canonicalize()
        .map_err(|source| NativeContractError::ReadManifest {
            path: manifest_path.to_path_buf(),
            source,
        })?;
    let mut seen_paths = BTreeSet::new();
    let indicator = verify_artifact(
        &directory,
        "indicator program",
        &document.programs.indicator.artifact,
        &mut seen_paths,
    )?;
    let signal = verify_artifact(
        &directory,
        "signal program",
        &document.programs.signal.artifact,
        &mut seen_paths,
    )?;
    let tag = verify_artifact(
        &directory,
        "tag program",
        &document.programs.tag.artifact,
        &mut seen_paths,
    )?;
    let frames = document
        .frames
        .iter()
        .enumerate()
        .map(|(index, frame)| {
            verify_artifact(
                &directory,
                &format!("raw frame {index}"),
                &frame.artifact,
                &mut seen_paths,
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    let futures = document
        .futures
        .iter()
        .enumerate()
        .map(|(index, futures)| {
            Ok((
                verify_artifact(
                    &directory,
                    &format!("futures funding frame {index}"),
                    &futures.funding_rate.artifact,
                    &mut seen_paths,
                )?,
                verify_artifact(
                    &directory,
                    &format!("futures mark frame {index}"),
                    &futures.mark.artifact,
                    &mut seen_paths,
                )?,
            ))
        })
        .collect::<Result<Vec<_>, NativeContractError>>()?;
    Ok(VerifiedArtifacts {
        indicator,
        signal,
        tag,
        frames,
        futures,
    })
}

fn verify_artifact(
    directory: &Path,
    role: &str,
    artifact: &ArtifactDocument,
    seen_paths: &mut BTreeSet<PathBuf>,
) -> Result<VerifiedArtifact, NativeContractError> {
    let joined = directory.join(&artifact.path);
    let resolved =
        joined
            .canonicalize()
            .map_err(|source| NativeContractError::ResolveArtifact {
                role: role.to_owned(),
                path: joined,
                source,
            })?;
    if !resolved.starts_with(directory) || !resolved.is_file() {
        return Err(NativeContractError::EscapedArtifact {
            role: role.to_owned(),
            path: resolved,
        });
    }
    if !seen_paths.insert(resolved.clone()) {
        return Err(invalid(format!(
            "duplicate artifact path declared for {role}: {}",
            artifact.path.display()
        )));
    }
    let actual = sha256_file(&resolved).map_err(|source| NativeContractError::HashArtifact {
        role: role.to_owned(),
        path: resolved.clone(),
        source,
    })?;
    if actual != artifact.sha256 {
        return Err(NativeContractError::ArtifactDigest {
            role: role.to_owned(),
            expected: artifact.sha256.clone(),
            actual,
        });
    }
    Ok(VerifiedArtifact { path: resolved })
}

fn parse_indicator(path: &Path) -> Result<IndicatorProgram, NativeContractError> {
    let encoded = fs::read_to_string(path).map_err(|source| NativeContractError::ReadManifest {
        path: path.to_path_buf(),
        source,
    })?;
    IndicatorProgram::from_json(&encoded).map_err(|source| NativeContractError::Program {
        role: "indicator program",
        source,
    })
}

fn parse_mutation(role: &'static str, path: &Path) -> Result<MutationProgram, NativeContractError> {
    let encoded = fs::read_to_string(path).map_err(|source| NativeContractError::ReadManifest {
        path: path.to_path_buf(),
        source,
    })?;
    MutationProgram::from_json(&encoded)
        .map_err(|source| NativeContractError::Program { role, source })
}

fn bind_frame_sources(
    manifest_path: &Path,
    contracts: &[ValidatedFrame],
    artifacts: Vec<VerifiedArtifact>,
) -> Vec<VerifiedFrameSource> {
    let mut sources = Vec::with_capacity(contracts.len());
    for (index, (contract, artifact)) in contracts.iter().zip(artifacts).enumerate() {
        sources.push(bind_frame_source(
            manifest_path,
            format!("manifest-frame-{index}"),
            contract,
            artifact,
        ));
    }
    sources
}

fn bind_futures_sources(
    manifest_path: &Path,
    contracts: &[ValidatedFutures],
    artifacts: Vec<(VerifiedArtifact, VerifiedArtifact)>,
) -> Vec<VerifiedFuturesSources> {
    contracts
        .iter()
        .zip(artifacts)
        .enumerate()
        .map(
            |(index, (contract, (funding, mark)))| VerifiedFuturesSources {
                pair: contract.pair.clone(),
                funding_rate: bind_frame_source(
                    manifest_path,
                    format!("manifest-funding-{index}"),
                    &contract.funding_rate,
                    funding,
                ),
                mark: bind_frame_source(
                    manifest_path,
                    format!("manifest-mark-{index}"),
                    &contract.mark,
                    mark,
                ),
            },
        )
        .collect()
}

fn bind_frame_source(
    manifest_path: &Path,
    node: String,
    contract: &ValidatedFrame,
    artifact: VerifiedArtifact,
) -> VerifiedFrameSource {
    let source = FeatherFrameSource::new(
        contract.identity.clone(),
        artifact.path,
        SourceLocation::new(node, manifest_path.display().to_string(), 0, 0),
    );
    VerifiedFrameSource {
        identity: contract.identity.clone(),
        rows: contract.rows,
        source,
    }
}

pub(crate) fn decode_verified_frame(
    source: &VerifiedFrameSource,
    role: &str,
) -> Result<NumericFrame, NativeContractError> {
    let frame =
        load_raw_ohlcv_frame(&source.source).map_err(|source| NativeContractError::RawFrame {
            role: role.to_owned(),
            source,
        })?;
    let actual = frame.timestamps_ms.len();
    if actual != source.rows {
        return Err(invalid(format!(
            "{role} row count differs: expected {}, got {actual}",
            source.rows
        )));
    }
    Ok(frame)
}

fn decode_catalog(sources: &[VerifiedFrameSource]) -> Result<FrameCatalog, NativeContractError> {
    let entries = sources
        .iter()
        .enumerate()
        .map(|(index, source)| {
            Ok((
                source.identity.clone(),
                decode_verified_frame(source, &format!("raw frame {index}"))?,
            ))
        })
        .collect::<Result<Vec<_>, NativeContractError>>()?;
    FrameCatalog::new(entries).map_err(|error| invalid(error.to_string()))
}

fn decode_futures(
    sources: &[VerifiedFuturesSources],
) -> Result<Vec<FuturesFrameSet>, NativeContractError> {
    sources
        .iter()
        .enumerate()
        .map(|(index, source)| {
            Ok(FuturesFrameSet {
                pair: source.pair.clone(),
                funding_rate: decode_verified_frame(
                    &source.funding_rate,
                    &format!("futures funding frame {index}"),
                )?,
                mark: decode_verified_frame(&source.mark, &format!("futures mark frame {index}"))?,
            })
        })
        .collect()
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
