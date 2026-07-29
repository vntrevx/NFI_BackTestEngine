//! Manifest parsing, artifact verification, and simulator-input assembly.

use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use nfi_sim_core::{
    PairSeries, PortfolioConfig, PriceStepChange, SimulationInput, SIMULATOR_SCHEMA_VERSION,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::decode::read_feather;
use crate::{VectorInputError, VECTOR_MANIFEST_SCHEMA_VERSION};

const LEGACY_VECTOR_MANIFEST_SCHEMA_VERSIONS: [&str; 2] = ["1.0.0", "1.1.0"];

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct VectorLoadProfile {
    pub schema_version: &'static str,
    pub manifest_ns: u64,
    pub vector_hash_ns: u64,
    pub feather_decode_ns: u64,
    pub pair_count: usize,
    pub row_count: usize,
    pub feature_column_count: usize,
    pub file_backed_bytes: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct VectorManifest {
    schema_version: String,
    config: PortfolioConfig,
    pairs: Vec<VectorPair>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct VectorPair {
    pub(crate) pair: String,
    /// Rows before this index are callback context, not trading-loop input.
    #[serde(default)]
    pub(crate) execution_start_index: usize,
    pub(crate) amount_step: Option<f64>,
    pub(crate) price_step: Option<f64>,
    #[serde(default)]
    pub(crate) price_steps: Vec<PriceStepChange>,
    #[serde(default)]
    pub(crate) minimum_stake: Option<f64>,
    #[serde(default)]
    pub(crate) minimum_amount: Option<f64>,
    #[serde(default)]
    pub(crate) minimum_cost: Option<f64>,
    pub(crate) vector: VectorArtifact,
    #[serde(default)]
    pub(crate) feature_columns: Vec<String>,
    #[serde(default)]
    pub(crate) can_short: ManifestFlag,
    /// The Feather vector carries sparse Freqtrade funding events.
    #[serde(default)]
    pub(crate) include_funding: ManifestFlag,
    #[serde(default = "default_enabled")]
    pub(crate) use_exit_signal: ManifestFlag,
    #[serde(default)]
    pub(crate) include_previous_close: ManifestFlag,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct VectorArtifact {
    path: PathBuf,
    sha256: String,
    rows: usize,
    format: String,
}

#[derive(Debug, Clone, Copy, Default, Deserialize)]
#[serde(transparent)]
pub(crate) struct ManifestFlag(bool);

impl ManifestFlag {
    pub(crate) const fn enabled(self) -> bool {
        self.0
    }
}

const fn default_enabled() -> ManifestFlag {
    ManifestFlag(true)
}

/// Load a compact manifest and reconstruct the existing simulator input.
///
/// The vector path must stay below the manifest directory and its SHA-256 must
/// match before Arrow metadata is trusted. This ordering makes a resumed run
/// fail closed if a cache link, symlink, or analyzed dataframe changed after
/// the manifest was written.
///
/// # Errors
///
/// Returns a precise manifest, filesystem, hash, Arrow schema, or scalar error.
pub fn load_vector_manifest(path: &Path) -> Result<SimulationInput, VectorInputError> {
    load_vector_manifest_profiled(path).map(|(input, _)| input)
}

/// Load the vector manifest and return aggregate input-boundary timings.
///
/// # Errors
///
/// Returns the same manifest, filesystem, hash, and Arrow errors as
/// [`load_vector_manifest`].
pub fn load_vector_manifest_profiled(
    path: &Path,
) -> Result<(SimulationInput, VectorLoadProfile), VectorInputError> {
    let manifest_started = Instant::now();
    let encoded = fs::read(path).map_err(|source| VectorInputError::ReadManifest {
        path: path.to_path_buf(),
        source,
    })?;
    let manifest: VectorManifest =
        serde_json::from_slice(&encoded).map_err(|source| VectorInputError::ParseManifest {
            path: path.to_path_buf(),
            source,
        })?;
    if manifest.schema_version != VECTOR_MANIFEST_SCHEMA_VERSION
        && !LEGACY_VECTOR_MANIFEST_SCHEMA_VERSIONS.contains(&manifest.schema_version.as_str())
    {
        return Err(VectorInputError::ManifestSchema(manifest.schema_version));
    }
    if manifest.pairs.is_empty() {
        return Err(VectorInputError::EmptyPairs);
    }

    let manifest_directory = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .canonicalize()
        .map_err(|source| VectorInputError::ReadManifest {
            path: path.to_path_buf(),
            source,
        })?;
    let mut profile = VectorLoadProfile {
        schema_version: "1.0.0",
        manifest_ns: duration_ns(manifest_started.elapsed()),
        vector_hash_ns: 0,
        feather_decode_ns: 0,
        pair_count: manifest.pairs.len(),
        row_count: 0,
        feature_column_count: 0,
        file_backed_bytes: 0,
    };
    let mut pair_names = BTreeSet::new();
    let mut pairs = Vec::with_capacity(manifest.pairs.len());
    for pair in manifest.pairs {
        if pair.pair.is_empty() || !pair_names.insert(pair.pair.clone()) {
            return Err(VectorInputError::InvalidPair(pair.pair));
        }
        pairs.push(load_pair(&manifest_directory, pair, &mut profile)?);
    }
    Ok((
        SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config: manifest.config,
            pairs,
        },
        profile,
    ))
}

fn load_pair(
    manifest_directory: &Path,
    pair: VectorPair,
    profile: &mut VectorLoadProfile,
) -> Result<PairSeries, VectorInputError> {
    validate_feature_names(&pair)?;
    if pair.vector.format != "feather-ipc" {
        return Err(VectorInputError::VectorFormat {
            pair: pair.pair,
            format: pair.vector.format,
        });
    }
    if pair.vector.path.is_absolute() {
        return Err(VectorInputError::AbsoluteVectorPath {
            pair: pair.pair,
            path: pair.vector.path,
        });
    }
    validate_sha256(&pair.pair, &pair.vector.sha256)?;
    let joined = manifest_directory.join(&pair.vector.path);
    let vector_path = joined
        .canonicalize()
        .map_err(|source| VectorInputError::ResolveVector {
            pair: pair.pair.clone(),
            path: joined.clone(),
            source,
        })?;
    if !vector_path.starts_with(manifest_directory) {
        return Err(VectorInputError::EscapedVectorPath {
            pair: pair.pair,
            path: vector_path,
        });
    }
    let hash_started = Instant::now();
    let actual_sha256 =
        sha256_file(&vector_path).map_err(|source| VectorInputError::HashVector {
            pair: pair.pair.clone(),
            path: vector_path.clone(),
            source,
        })?;
    profile.vector_hash_ns = profile
        .vector_hash_ns
        .saturating_add(duration_ns(hash_started.elapsed()));
    if actual_sha256 != pair.vector.sha256 {
        return Err(VectorInputError::VectorHash {
            pair: pair.pair,
            expected: pair.vector.sha256,
            actual: actual_sha256,
        });
    }

    let decode_started = Instant::now();
    let (candles, feature_columns, file_backed_bytes) = read_feather(&vector_path, &pair)?;
    profile.feather_decode_ns = profile
        .feather_decode_ns
        .saturating_add(duration_ns(decode_started.elapsed()));
    profile.row_count = profile.row_count.saturating_add(candles.len());
    profile.feature_column_count = profile
        .feature_column_count
        .saturating_add(feature_columns.len());
    profile.file_backed_bytes = profile.file_backed_bytes.saturating_add(file_backed_bytes);
    if candles.len() != pair.vector.rows {
        return Err(VectorInputError::RowCount {
            pair: pair.pair,
            expected: pair.vector.rows,
            actual: candles.len(),
        });
    }
    if pair.execution_start_index >= candles.len() {
        return Err(VectorInputError::ExecutionStart {
            pair: pair.pair,
            index: pair.execution_start_index,
            rows: candles.len(),
        });
    }
    Ok(PairSeries {
        pair: pair.pair,
        execution_start_index: pair.execution_start_index,
        amount_step: pair.amount_step,
        price_step: pair.price_step,
        price_steps: pair.price_steps,
        minimum_stake: pair.minimum_stake,
        minimum_amount: pair.minimum_amount,
        minimum_cost: pair.minimum_cost,
        feature_columns,
        candles,
    })
}

fn duration_ns(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

fn validate_feature_names(pair: &VectorPair) -> Result<(), VectorInputError> {
    let mut names = BTreeSet::new();
    for column in &pair.feature_columns {
        if column.is_empty() || !names.insert(column) {
            return Err(VectorInputError::InvalidFeatureName {
                pair: pair.pair.clone(),
                column: column.clone(),
            });
        }
    }
    Ok(())
}

fn validate_sha256(pair: &str, value: &str) -> Result<(), VectorInputError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(VectorInputError::InvalidSha256 {
            pair: pair.to_owned(),
            sha256: value.to_owned(),
        });
    }
    Ok(())
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
mod tests {
    use super::*;

    #[test]
    fn sha_validation_accepts_only_lowercase_canonical_tokens() {
        assert!(validate_sha256("AAA/USDT", &"a".repeat(64)).is_ok());
        assert!(validate_sha256("AAA/USDT", &"A".repeat(64)).is_err());
        assert!(validate_sha256("AAA/USDT", &"a".repeat(63)).is_err());
    }

    #[test]
    fn feature_names_must_be_unique_and_non_empty() {
        let pair = VectorPair {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 0,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            vector: VectorArtifact {
                path: PathBuf::from("vectors/a.feather"),
                sha256: "a".repeat(64),
                rows: 1,
                format: "feather-ipc".to_owned(),
            },
            feature_columns: vec!["RSI_14".to_owned(), "RSI_14".to_owned()],
            can_short: ManifestFlag(false),
            include_funding: ManifestFlag(false),
            use_exit_signal: ManifestFlag(true),
            include_previous_close: ManifestFlag(true),
        };

        assert!(matches!(
            validate_feature_names(&pair),
            Err(VectorInputError::InvalidFeatureName { .. })
        ));
    }
}
