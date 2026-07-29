//! Feather decoding and disk-backed row-store construction.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use arrow2::io::ipc::read::{read_file_metadata, FileReader};
use nfi_sim_core::{CandleSeries, FeatureColumn, FileBackedRows};

use crate::loader::VectorPair;
use crate::row::append_batch_to_spool;
use crate::schema::{column_positions, feature_layout, projected_source_indices};
use crate::VectorInputError;

const SPOOL_DIRECTORY_ENVIRONMENT: &str = "NFI_BTE_SPOOL_DIRECTORY";
// One bounded buffer converts batches into fixed-width rows without issuing a
// kernel write for every candle. The spool itself remains disk-backed.
const SPOOL_WRITE_BUFFER_BYTES: usize = 256 * 1024;

pub(crate) fn read_feather(
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
    let source_indices = projected_source_indices(&metadata.schema, pair)?;
    let reader = FileReader::new(file, metadata, Some(source_indices), None);
    let projected_positions = column_positions(reader.schema());
    let (feature_layouts, row_stride) =
        feature_layout(reader.schema(), &projected_positions, pair)?;
    let spool_file = pair_spool(&pair.pair)?;
    let mut spool = BufWriter::with_capacity(SPOOL_WRITE_BUFFER_BYTES, spool_file);
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
            &feature_layouts,
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
    spool
        .flush()
        .map_err(|source| VectorInputError::FileBacking {
            pair: pair.pair.clone(),
            source,
        })?;
    let spool = spool
        .into_inner()
        .map_err(|error| VectorInputError::FileBacking {
            pair: pair.pair.clone(),
            source: error.into_error(),
        })?;
    let rows =
        FileBackedRows::new(spool, row_offset, feature_layouts.len(), tags).map_err(|source| {
            VectorInputError::FileBacking {
                pair: pair.pair.clone(),
                source,
            }
        })?;
    let features = feature_layouts
        .into_iter()
        .enumerate()
        .map(|(feature_index, layout)| {
            (
                layout.name,
                FeatureColumn::file_backed(rows.clone(), feature_index, layout.kind),
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
