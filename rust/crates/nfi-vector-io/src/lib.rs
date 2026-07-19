//! Verified columnar input boundary for analyzed strategy vectors.
//!
//! Python remains responsible for running the real strategy's vector methods.
//! This crate reads their immutable Feather output directly, so neither Python
//! nor JSON duplicates every candle and callback feature before simulation.
//! The simulator core intentionally does not depend on Arrow or the filesystem.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use arrow2::array::{Array, BooleanArray, PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, Schema, TimeUnit};
use arrow2::io::ipc::read::{read_file_metadata, FileReader};
use nfi_sim_core::{
    CandleSeries, FeatureColumn, FileBackedFeatureKind, FileBackedRows, PairSeries,
    PortfolioConfig, PriceStepChange, SimulationInput, FILE_BACKED_FEATURE_BYTES,
    FILE_BACKED_ROW_HEADER_BYTES, SIMULATOR_SCHEMA_VERSION,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

/// Version of the compact manifest consumed by this crate.
pub const VECTOR_MANIFEST_SCHEMA_VERSION: &str = "1.2.0";
const LEGACY_VECTOR_MANIFEST_SCHEMA_VERSIONS: [&str; 2] = ["1.0.0", "1.1.0"];
const SPOOL_DIRECTORY_ENVIRONMENT: &str = "NFI_BTE_SPOOL_DIRECTORY";

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
struct VectorPair {
    pair: String,
    /// Rows before this index are callback context, not trading-loop input.
    #[serde(default)]
    execution_start_index: usize,
    amount_step: Option<f64>,
    price_step: Option<f64>,
    #[serde(default)]
    price_steps: Vec<PriceStepChange>,
    #[serde(default)]
    minimum_stake: Option<f64>,
    #[serde(default)]
    minimum_amount: Option<f64>,
    #[serde(default)]
    minimum_cost: Option<f64>,
    vector: VectorArtifact,
    #[serde(default)]
    feature_columns: Vec<String>,
    #[serde(default)]
    can_short: ManifestFlag,
    /// The Feather vector carries sparse Freqtrade funding events.
    #[serde(default)]
    include_funding: ManifestFlag,
    #[serde(default = "default_enabled")]
    use_exit_signal: ManifestFlag,
    #[serde(default)]
    include_previous_close: ManifestFlag,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct VectorArtifact {
    path: PathBuf,
    sha256: String,
    rows: usize,
    format: String,
}

#[derive(Debug, Clone, Copy, Default, Deserialize)]
#[serde(transparent)]
struct ManifestFlag(bool);

impl ManifestFlag {
    const fn enabled(self) -> bool {
        self.0
    }
}

const fn default_enabled() -> ManifestFlag {
    ManifestFlag(true)
}

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

fn read_feather(
    path: &Path,
    pair: &VectorPair,
) -> Result<(CandleSeries, BTreeMap<String, FeatureColumn>, u64), VectorInputError> {
    let mut file = File::open(path).map_err(|source| VectorInputError::OpenFeather {
        pair: pair.pair.clone(),
        path: path.to_path_buf(),
        source,
    })?;
    let metadata = read_file_metadata(&mut file).map_err(|error| VectorInputError::Feather {
        pair: pair.pair.clone(),
        path: path.to_path_buf(),
        message: error.to_string(),
    })?;
    let mut required = required_columns(pair);
    // Tags are optional in Freqtrade vector output when the strategy never
    // populated them. Their absence means `None`, not a malformed signal.
    for optional in ["nfi_exec_enter_tag", "nfi_exec_exit_tag"] {
        if metadata
            .schema
            .fields
            .iter()
            .any(|field| field.name == optional)
        {
            required.insert(optional.to_owned());
        }
    }
    let mut source_indices = Vec::with_capacity(required.len());
    for column in &required {
        let index = metadata
            .schema
            .fields
            .iter()
            .position(|field| field.name == *column)
            .ok_or_else(|| VectorInputError::MissingColumn {
                pair: pair.pair.clone(),
                column: column.clone(),
            })?;
        source_indices.push(index);
    }
    source_indices.sort_unstable();
    source_indices.dedup();
    let reader = FileReader::new(file, metadata, Some(source_indices), None);
    let projected_positions = column_positions(reader.schema());
    let (feature_kinds, row_stride) = feature_layout(reader.schema(), &projected_positions, pair)?;
    let mut spool = pair_spool(&pair.pair)?;
    let mut row_buffer = vec![0_u8; row_stride];
    let mut tag_ids = BTreeMap::new();
    let mut tags = Vec::new();

    let mut previous_close = None;
    let mut row_offset = 0_usize;
    for batch in reader {
        let batch = batch.map_err(|error| VectorInputError::Feather {
            pair: pair.pair.clone(),
            path: path.to_path_buf(),
            message: error.to_string(),
        })?;
        append_batch_to_spool(
            &batch,
            &projected_positions,
            pair,
            row_offset,
            &mut previous_close,
            &feature_kinds,
            &mut spool,
            &mut row_buffer,
            &mut tag_ids,
            &mut tags,
        )?;
        row_offset += batch.len();
    }
    let file_backed_bytes = row_offset
        .checked_mul(row_stride)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| VectorInputError::FileBacking {
            pair: pair.pair.clone(),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "pair spool byte count is too large",
            ),
        })?;
    let rows =
        FileBackedRows::new(spool, row_offset, feature_kinds.len(), tags).map_err(|source| {
            VectorInputError::FileBacking {
                pair: pair.pair.clone(),
                source,
            }
        })?;
    let features = feature_kinds
        .into_iter()
        .enumerate()
        .map(|(feature_index, (name, kind))| {
            (
                name,
                FeatureColumn::file_backed(rows.clone(), feature_index, kind),
            )
        })
        .collect();
    Ok((CandleSeries::file_backed(rows), features, file_backed_bytes))
}

fn pair_spool(pair: &str) -> Result<File, VectorInputError> {
    // OS-local temp avoids the severe random-read penalty of a WSL-mounted
    // Windows vector directory. Hosts whose temp directory is RAM-backed can
    // select a disk-backed mount explicitly; the path is configuration, never
    // a compiled machine assumption.
    let result = std::env::var_os(SPOOL_DIRECTORY_ENVIRONMENT)
        .map_or_else(tempfile::tempfile, tempfile::tempfile_in);
    result.map_err(|source| VectorInputError::FileBacking {
        pair: pair.to_owned(),
        source,
    })
}

fn column_positions(schema: &Schema) -> BTreeMap<String, usize> {
    schema
        .fields
        .iter()
        .enumerate()
        .map(|(index, field)| (field.name.clone(), index))
        .collect()
}

fn feature_layout(
    schema: &Schema,
    projected_positions: &BTreeMap<String, usize>,
    pair: &VectorPair,
) -> Result<(Vec<(String, FileBackedFeatureKind)>, usize), VectorInputError> {
    let feature_kinds = pair
        .feature_columns
        .iter()
        .map(|name| {
            let data_type = &schema.fields[projected_positions[name]].data_type;
            let kind = match data_type {
                DataType::Boolean => FileBackedFeatureKind::Boolean,
                data_type if is_numeric_type(data_type) => FileBackedFeatureKind::Number,
                actual => {
                    return Err(VectorInputError::ColumnType {
                        pair: pair.pair.clone(),
                        column: name.clone(),
                        actual: Box::new(actual.clone()),
                        expected: "numeric or boolean",
                    });
                }
            };
            Ok((name.clone(), kind))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let feature_bytes = feature_kinds
        .len()
        .checked_mul(FILE_BACKED_FEATURE_BYTES)
        .ok_or_else(|| file_backing_error(&pair.pair, "feature row is too wide"))?;
    let row_stride = FILE_BACKED_ROW_HEADER_BYTES
        .checked_add(feature_bytes)
        .ok_or_else(|| file_backing_error(&pair.pair, "pair row is too wide"))?;
    Ok((feature_kinds, row_stride))
}

fn file_backing_error(pair: &str, message: &'static str) -> VectorInputError {
    VectorInputError::FileBacking {
        pair: pair.to_owned(),
        source: std::io::Error::new(std::io::ErrorKind::InvalidData, message),
    }
}

fn required_columns(pair: &VectorPair) -> BTreeSet<String> {
    let mut columns = BTreeSet::from([
        "date".to_owned(),
        "open".to_owned(),
        "high".to_owned(),
        "low".to_owned(),
        "close".to_owned(),
        "volume".to_owned(),
        "nfi_exec_enter_long".to_owned(),
        "nfi_exec_exit_long".to_owned(),
    ]);
    if pair.can_short.enabled() {
        columns.insert("nfi_exec_enter_short".to_owned());
        columns.insert("nfi_exec_exit_short".to_owned());
    }
    if pair.include_funding.enabled() {
        columns.insert("nfi_exec_funding_rate".to_owned());
        columns.insert("nfi_exec_funding_mark_price".to_owned());
    }
    columns.extend(pair.feature_columns.iter().cloned());
    columns
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
// Keeping one row constructor makes the signal timing and shared tag/reason
// order directly reviewable against the two legacy Python adapter loops.
fn append_batch_to_spool(
    batch: &Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    pair: &VectorPair,
    row_offset: usize,
    previous_close: &mut Option<f64>,
    feature_kinds: &[(String, FileBackedFeatureKind)],
    spool: &mut File,
    row_buffer: &mut [u8],
    tag_ids: &mut BTreeMap<String, u32>,
    tags: &mut Vec<String>,
) -> Result<(), VectorInputError> {
    for row in 0..batch.len() {
        row_buffer.fill(0);
        let absolute_row = row_offset + row;
        let timestamp_ms = required_timestamp_ms(
            column(batch, positions, "date"),
            row,
            &pair.pair,
            "date",
            absolute_row,
        )?;
        let open = required_number(
            column(batch, positions, "open"),
            row,
            &pair.pair,
            "open",
            absolute_row,
        )?;
        let high = required_number(
            column(batch, positions, "high"),
            row,
            &pair.pair,
            "high",
            absolute_row,
        )?;
        let low = required_number(
            column(batch, positions, "low"),
            row,
            &pair.pair,
            "low",
            absolute_row,
        )?;
        let close = required_number(
            column(batch, positions, "close"),
            row,
            &pair.pair,
            "close",
            absolute_row,
        )?;
        let volume = required_number(
            column(batch, positions, "volume"),
            row,
            &pair.pair,
            "volume",
            absolute_row,
        )?;
        let entry_tag = optional_column(batch, positions, "nfi_exec_enter_tag")
            .map(|array| optional_text(array, row, &pair.pair, "nfi_exec_enter_tag"))
            .transpose()?
            .flatten();
        let exit_tag = optional_column(batch, positions, "nfi_exec_exit_tag")
            .map(|array| optional_text(array, row, &pair.pair, "nfi_exec_exit_tag"))
            .transpose()?
            .flatten();
        let enter_long = enabled(
            column(batch, positions, "nfi_exec_enter_long"),
            row,
            &pair.pair,
            "nfi_exec_enter_long",
            absolute_row,
        )?;
        let exit_long = pair.use_exit_signal.enabled()
            && enabled(
                column(batch, positions, "nfi_exec_exit_long"),
                row,
                &pair.pair,
                "nfi_exec_exit_long",
                absolute_row,
            )?;
        let enter_short = pair.can_short.enabled()
            && enabled(
                column(batch, positions, "nfi_exec_enter_short"),
                row,
                &pair.pair,
                "nfi_exec_enter_short",
                absolute_row,
            )?;
        let exit_short = pair.can_short.enabled()
            && pair.use_exit_signal.enabled()
            && enabled(
                column(batch, positions, "nfi_exec_exit_short"),
                row,
                &pair.pair,
                "nfi_exec_exit_short",
                absolute_row,
            )?;
        let funding_rate = pair
            .include_funding
            .enabled()
            .then(|| {
                optional_number(
                    column(batch, positions, "nfi_exec_funding_rate"),
                    row,
                    &pair.pair,
                    "nfi_exec_funding_rate",
                    absolute_row,
                )
            })
            .transpose()?
            .flatten();
        let funding_mark_price = pair
            .include_funding
            .enabled()
            .then(|| {
                optional_number(
                    column(batch, positions, "nfi_exec_funding_mark_price"),
                    row,
                    &pair.pair,
                    "nfi_exec_funding_mark_price",
                    absolute_row,
                )
            })
            .transpose()?
            .flatten();
        let prior_close = pair
            .include_previous_close
            .enabled()
            .then_some(*previous_close)
            .flatten();
        let mut flags = 0_u8;
        set_flag(&mut flags, 0, prior_close.is_some());
        set_flag(&mut flags, 1, funding_rate.is_some());
        set_flag(&mut flags, 2, funding_mark_price.is_some());
        set_flag(&mut flags, 3, enter_long);
        set_flag(&mut flags, 4, enter_short);
        set_flag(&mut flags, 5, exit_long);
        set_flag(&mut flags, 6, exit_short);
        put_i64(row_buffer, 0, timestamp_ms);
        put_f64(row_buffer, 8, open);
        put_f64(row_buffer, 16, high);
        put_f64(row_buffer, 24, low);
        put_f64(row_buffer, 32, close);
        put_f64(row_buffer, 40, volume);
        put_f64(row_buffer, 48, prior_close.unwrap_or_default());
        put_f64(row_buffer, 56, funding_rate.unwrap_or_default());
        put_f64(row_buffer, 64, funding_mark_price.unwrap_or_default());
        row_buffer[72] = flags;
        put_u32(
            row_buffer,
            73,
            dictionary_id(entry_tag.as_deref(), tag_ids, tags, &pair.pair)?,
        );
        put_u32(
            row_buffer,
            77,
            dictionary_id(exit_tag.as_deref(), tag_ids, tags, &pair.pair)?,
        );
        *previous_close = Some(close);

        for (feature_index, (name, kind)) in feature_kinds.iter().enumerate() {
            let array = column(batch, positions, name);
            let value = match kind {
                FileBackedFeatureKind::Number => {
                    // Pandas materializes nullable numeric Arrow values as
                    // NaN. Preserve that exact callback-visible warm-up value.
                    if array.is_null(row) {
                        f64::NAN
                    } else {
                        required_number(array, row, &pair.pair, name, absolute_row)?
                    }
                }
                FileBackedFeatureKind::Boolean => {
                    if array.is_null(row) {
                        return Err(VectorInputError::NullValue {
                            pair: pair.pair.clone(),
                            column: name.clone(),
                            row: absolute_row,
                        });
                    }
                    let boolean = array
                        .as_any()
                        .downcast_ref::<BooleanArray>()
                        .expect("feature type was checked against the Arrow schema");
                    f64::from(u8::from(boolean.value(row)))
                }
            };
            put_f64(
                row_buffer,
                FILE_BACKED_ROW_HEADER_BYTES + feature_index * FILE_BACKED_FEATURE_BYTES,
                value,
            );
        }
        spool
            .write_all(row_buffer)
            .map_err(|source| VectorInputError::FileBacking {
                pair: pair.pair.clone(),
                source,
            })?;
    }
    Ok(())
}

fn dictionary_id(
    value: Option<&str>,
    ids: &mut BTreeMap<String, u32>,
    values: &mut Vec<String>,
    pair: &str,
) -> Result<u32, VectorInputError> {
    let Some(value) = value else {
        return Ok(0);
    };
    if let Some(existing) = ids.get(value) {
        return Ok(*existing);
    }
    let identifier =
        u32::try_from(values.len() + 1).map_err(|_| VectorInputError::FileBacking {
            pair: pair.to_owned(),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "pair tag dictionary exceeds its row schema",
            ),
        })?;
    ids.insert(value.to_owned(), identifier);
    values.push(value.to_owned());
    Ok(identifier)
}

const fn set_flag(flags: &mut u8, bit: u8, enabled: bool) {
    if enabled {
        *flags |= 1 << bit;
    }
}

fn put_i64(row: &mut [u8], offset: usize, value: i64) {
    row[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn put_u32(row: &mut [u8], offset: usize, value: u32) {
    row[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put_f64(row: &mut [u8], offset: usize, value: f64) {
    row[offset..offset + 8].copy_from_slice(&value.to_bits().to_le_bytes());
}

fn column<'a>(
    batch: &'a Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    name: &str,
) -> &'a dyn Array {
    batch.arrays()[positions[name]].as_ref()
}

fn optional_column<'a>(
    batch: &'a Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    name: &str,
) -> Option<&'a dyn Array> {
    positions
        .get(name)
        .map(|index| batch.arrays()[*index].as_ref())
}

fn required_timestamp_ms(
    array: &dyn Array,
    row: usize,
    pair: &str,
    column: &str,
    absolute_row: usize,
) -> Result<i64, VectorInputError> {
    if array.is_null(row) {
        return Err(VectorInputError::NullValue {
            pair: pair.to_owned(),
            column: column.to_owned(),
            row: absolute_row,
        });
    }
    let timestamp_units = array
        .as_any()
        .downcast_ref::<PrimitiveArray<i64>>()
        .ok_or_else(|| VectorInputError::ColumnType {
            pair: pair.to_owned(),
            column: column.to_owned(),
            actual: Box::new(array.data_type().clone()),
            expected: "Arrow timestamp",
        })?
        .value(row);
    // Vector-worker files are timestamp[ms], while small pandas-authored
    // fixtures are commonly timestamp[ns]. The legacy adapter used
    // `Timestamp.value // 1_000_000`; these positive UTC timestamps therefore
    // use the same integer conversion instead of a floating-point cast.
    match array.data_type() {
        DataType::Timestamp(TimeUnit::Second, _) => {
            timestamp_units
                .checked_mul(1_000)
                .ok_or_else(|| VectorInputError::ColumnType {
                    pair: pair.to_owned(),
                    column: column.to_owned(),
                    actual: Box::new(array.data_type().clone()),
                    expected: "timestamp representable in milliseconds",
                })
        }
        DataType::Timestamp(TimeUnit::Millisecond, _) => Ok(timestamp_units),
        DataType::Timestamp(TimeUnit::Microsecond, _) => Ok(timestamp_units / 1_000),
        DataType::Timestamp(TimeUnit::Nanosecond, _) => Ok(timestamp_units / 1_000_000),
        actual => Err(VectorInputError::ColumnType {
            pair: pair.to_owned(),
            column: column.to_owned(),
            actual: Box::new(actual.clone()),
            expected: "Arrow timestamp",
        }),
    }
}

#[allow(clippy::cast_precision_loss)]
// The Python contract calls `float()` on integer signal/features. Accepting
// Arrow integer columns must perform that same conversion before comparison.
fn required_number(
    array: &dyn Array,
    row: usize,
    pair: &str,
    column: &str,
    absolute_row: usize,
) -> Result<f64, VectorInputError> {
    if array.is_null(row) {
        return Err(VectorInputError::NullValue {
            pair: pair.to_owned(),
            column: column.to_owned(),
            row: absolute_row,
        });
    }
    let value = match array.data_type() {
        DataType::Float64 => primitive_value::<f64>(array, row),
        DataType::Float32 => f64::from(primitive_value::<f32>(array, row)),
        DataType::Int64 => primitive_value::<i64>(array, row) as f64,
        DataType::Int32 => f64::from(primitive_value::<i32>(array, row)),
        DataType::Int16 => f64::from(primitive_value::<i16>(array, row)),
        DataType::Int8 => f64::from(primitive_value::<i8>(array, row)),
        DataType::UInt64 => primitive_value::<u64>(array, row) as f64,
        DataType::UInt32 => f64::from(primitive_value::<u32>(array, row)),
        DataType::UInt16 => f64::from(primitive_value::<u16>(array, row)),
        DataType::UInt8 => f64::from(primitive_value::<u8>(array, row)),
        actual => {
            return Err(VectorInputError::ColumnType {
                pair: pair.to_owned(),
                column: column.to_owned(),
                actual: Box::new(actual.clone()),
                expected: "numeric",
            });
        }
    };
    Ok(value)
}

fn primitive_value<T: arrow2::types::NativeType>(array: &dyn Array, row: usize) -> T {
    array
        .as_any()
        .downcast_ref::<PrimitiveArray<T>>()
        .expect("numeric physical type matches Arrow data type")
        .value(row)
}

fn is_numeric_type(data_type: &DataType) -> bool {
    matches!(
        data_type,
        DataType::Float64
            | DataType::Float32
            | DataType::Int64
            | DataType::Int32
            | DataType::Int16
            | DataType::Int8
            | DataType::UInt64
            | DataType::UInt32
            | DataType::UInt16
            | DataType::UInt8
    )
}

fn enabled(
    array: &dyn Array,
    row: usize,
    pair: &str,
    column: &str,
    absolute_row: usize,
) -> Result<bool, VectorInputError> {
    if array.is_null(row) {
        return Ok(false);
    }
    let value = required_number(array, row, pair, column, absolute_row)?;
    Ok(!value.is_nan() && value != 0.0)
}

fn optional_number(
    array: &dyn Array,
    row: usize,
    pair: &str,
    column: &str,
    absolute_row: usize,
) -> Result<Option<f64>, VectorInputError> {
    if array.is_null(row) {
        return Ok(None);
    }
    let value = required_number(array, row, pair, column, absolute_row)?;
    Ok((!value.is_nan()).then_some(value))
}

fn optional_text(
    array: &dyn Array,
    row: usize,
    pair: &str,
    column: &str,
) -> Result<Option<String>, VectorInputError> {
    if array.is_null(row) {
        return Ok(None);
    }
    let value = match array.data_type() {
        DataType::Utf8 => array
            .as_any()
            .downcast_ref::<Utf8Array<i32>>()
            .expect("UTF-8 physical type uses i32 offsets")
            .value(row),
        DataType::LargeUtf8 => array
            .as_any()
            .downcast_ref::<Utf8Array<i64>>()
            .expect("large UTF-8 physical type uses i64 offsets")
            .value(row),
        actual => {
            return Err(VectorInputError::ColumnType {
                pair: pair.to_owned(),
                column: column.to_owned(),
                actual: Box::new(actual.clone()),
                expected: "UTF-8 string",
            });
        }
    };
    Ok((!value.is_empty()).then(|| value.to_owned()))
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
