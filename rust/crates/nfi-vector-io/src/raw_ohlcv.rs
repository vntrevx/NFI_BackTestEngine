//! Exact, non-transforming Feather input for native indicator execution.
//!
//! This boundary validates and decodes raw OHLCV values. It deliberately does
//! not aggregate duplicate candles, resample timeframes, fill gaps, or apply a
//! timerange. Those are observable Freqtrade semantics and belong in a
//! separately verified cleaning stage. Rows are only stable-sorted by `date`,
//! matching the ordering performed before Python's cleaning stage.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::path::PathBuf;

use arrow2::array::{Array, PrimitiveArray};
use arrow2::datatypes::{DataType, Schema, TimeUnit};
use arrow2::io::ipc::read::{read_file_metadata, FileReader};
use nfi_vector_core::alignment::{FrameCatalog, FrameIdentity, NumericFrame, SourceLocation};

use crate::VectorInputError;

const DATE_COLUMN: &str = "date";
const VALUE_COLUMNS: [&str; 5] = ["open", "high", "low", "close", "volume"];

/// One explicit raw-candle source and the strategy location that requested it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FeatherFrameSource {
    pub identity: FrameIdentity,
    pub path: PathBuf,
    pub source: SourceLocation,
}

impl FeatherFrameSource {
    #[must_use]
    pub fn new(identity: FrameIdentity, path: impl Into<PathBuf>, source: SourceLocation) -> Self {
        Self {
            identity,
            path: path.into(),
            source,
        }
    }

    fn error(&self, message: impl Into<String>) -> VectorInputError {
        self.source.error(message).into()
    }

    fn label(&self) -> String {
        format!(
            "raw OHLCV frame {} {} at {}",
            self.identity.pair,
            self.identity.timeframe.as_str(),
            self.path.display()
        )
    }
}

/// Decode one raw Feather source into a numeric frame.
///
/// Numeric nulls remain `None`, present NaNs remain `Some(NaN)`, and duplicate
/// timestamps remain separate rows in their original relative order. No
/// Freqtrade cleaning or timerange bounding is performed here.
///
/// # Errors
///
/// Returns a source-located error when the file cannot be read, the required
/// schema is absent or ambiguous, `date` is not `timestamp[ms]`, an OHLCV
/// value is not `float64`, or a date value is null.
pub fn load_raw_ohlcv_frame(input: &FeatherFrameSource) -> Result<NumericFrame, VectorInputError> {
    let mut file = File::open(&input.path)
        .map_err(|error| input.error(format!("cannot open {}: {error}", input.label())))?;
    let metadata = read_file_metadata(&mut file)
        .map_err(|error| input.error(format!("cannot decode {}: {error}", input.label())))?;
    let source_indices = validate_schema(&metadata.schema, input)?;
    let reader = FileReader::new(file, metadata, Some(source_indices), None);
    let positions = column_positions(reader.schema(), input)?;
    let mut rows = Vec::new();
    let mut absolute_row = 0_usize;

    for batch in reader {
        let batch = batch
            .map_err(|error| input.error(format!("cannot decode {}: {error}", input.label())))?;
        let date = timestamp_array(batch.arrays()[positions[DATE_COLUMN]].as_ref());
        let values =
            VALUE_COLUMNS.map(|name| numeric_array(batch.arrays()[positions[name]].as_ref()));
        for row in 0..batch.len() {
            if date.is_null(row) {
                return Err(input.error(format!(
                    "{} column {DATE_COLUMN:?} contains null at row {absolute_row}",
                    input.label()
                )));
            }
            rows.push(RawRow {
                timestamp_ms: date.value(row),
                values: values.map(|array| (!array.is_null(row)).then(|| array.value(row))),
            });
            absolute_row = absolute_row
                .checked_add(1)
                .ok_or_else(|| input.error(format!("{} row count is too large", input.label())))?;
        }
    }

    // Rust's slice sort is stable, so equal timestamps retain their exact
    // source order for the later Freqtrade duplicate aggregation stage.
    rows.sort_by_key(|row| row.timestamp_ms);
    let timestamps_ms = rows.iter().map(|row| row.timestamp_ms).collect();
    let columns = VALUE_COLUMNS
        .into_iter()
        .enumerate()
        .map(|(index, name)| {
            (
                name.to_owned(),
                rows.iter().map(|row| row.values[index]).collect(),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let frame = NumericFrame {
        identity: input.identity.clone(),
        timestamps_ms,
        columns,
    };
    frame
        .validate()
        .map_err(|error| input.error(format!("{} is invalid: {error}", input.label())))?;
    Ok(frame)
}

/// Decode explicit pair/timeframe sources and build one exact frame catalog.
///
/// # Errors
///
/// Returns the first source-located decode error, or fails closed when two
/// inputs declare the same identity.
pub fn load_raw_ohlcv_catalog(
    inputs: impl IntoIterator<Item = FeatherFrameSource>,
) -> Result<FrameCatalog, VectorInputError> {
    let mut entries = Vec::new();
    let mut identities = BTreeSet::new();
    for input in inputs {
        if !identities.insert(input.identity.clone()) {
            return Err(input.error(format!(
                "raw OHLCV catalog contains duplicate identity {} {}",
                input.identity.pair,
                input.identity.timeframe.as_str()
            )));
        }
        let frame = load_raw_ohlcv_frame(&input)?;
        entries.push((input.identity, frame));
    }
    FrameCatalog::new(entries).map_err(Into::into)
}

#[derive(Clone, Copy, Debug)]
struct RawRow {
    timestamp_ms: i64,
    values: [Option<f64>; 5],
}

fn validate_schema(
    schema: &Schema,
    input: &FeatherFrameSource,
) -> Result<Vec<usize>, VectorInputError> {
    let required = std::iter::once(DATE_COLUMN).chain(VALUE_COLUMNS);
    let mut indices = Vec::with_capacity(6);
    for name in required {
        let matches = schema
            .fields
            .iter()
            .enumerate()
            .filter(|(_, field)| field.name == name)
            .collect::<Vec<_>>();
        let [(index, field)] = matches.as_slice() else {
            let message = if matches.is_empty() {
                format!("{} is missing required column {name:?}", input.label())
            } else {
                format!("{} contains duplicate column {name:?}", input.label())
            };
            return Err(input.error(message));
        };
        let expected = if name == DATE_COLUMN {
            "timestamp[ms]"
        } else {
            "float64"
        };
        let valid = if name == DATE_COLUMN {
            matches!(
                field.data_type,
                DataType::Timestamp(TimeUnit::Millisecond, _)
            )
        } else {
            field.data_type == DataType::Float64
        };
        if !valid {
            return Err(input.error(format!(
                "{} column {name:?} has type {:?}; expected {expected}",
                input.label(),
                field.data_type
            )));
        }
        indices.push(*index);
    }
    indices.sort_unstable();
    Ok(indices)
}

fn column_positions(
    schema: &Schema,
    input: &FeatherFrameSource,
) -> Result<BTreeMap<String, usize>, VectorInputError> {
    let positions = schema
        .fields
        .iter()
        .enumerate()
        .map(|(index, field)| (field.name.clone(), index))
        .collect::<BTreeMap<_, _>>();
    for name in std::iter::once(DATE_COLUMN).chain(VALUE_COLUMNS) {
        if !positions.contains_key(name) {
            return Err(input.error(format!(
                "{} projection omitted required column {name:?}",
                input.label()
            )));
        }
    }
    Ok(positions)
}

fn timestamp_array(array: &dyn Array) -> &PrimitiveArray<i64> {
    array
        .as_any()
        .downcast_ref::<PrimitiveArray<i64>>()
        .expect("validated timestamp[ms] has i64 Arrow storage")
}

fn numeric_array(array: &dyn Array) -> &PrimitiveArray<f64> {
    array
        .as_any()
        .downcast_ref::<PrimitiveArray<f64>>()
        .expect("validated float64 has f64 Arrow storage")
}

#[cfg(test)]
mod tests {
    use std::fs::File;
    use std::path::Path;

    use arrow2::array::PrimitiveArray;
    use arrow2::chunk::Chunk;
    use arrow2::datatypes::{Field, Schema};
    use arrow2::io::ipc::write::{FileWriter, WriteOptions};
    use nfi_vector_core::alignment::Timeframe;

    use super::*;

    fn identity(pair: &str, timeframe: &str) -> FrameIdentity {
        FrameIdentity::new(pair, Timeframe::parse(timeframe).expect("timeframe")).expect("identity")
    }

    fn input(path: &Path, pair: &str, timeframe: &str) -> FeatherFrameSource {
        FeatherFrameSource::new(
            identity(pair, timeframe),
            path,
            SourceLocation::new("n41", "NostalgiaForInfinityX7.py", 2200, 16),
        )
    }

    fn write_ohlcv(path: &Path, dates: Vec<Option<i64>>, values: [Vec<Option<f64>>; 5]) {
        let schema = Schema::from(
            [Field::new(
                DATE_COLUMN,
                DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".to_owned())),
                true,
            )]
            .into_iter()
            .chain(VALUE_COLUMNS.map(|name| Field::new(name, DataType::Float64, true)))
            .collect::<Vec<_>>(),
        );
        let mut arrays: Vec<Box<dyn Array>> = vec![Box::new(PrimitiveArray::from(dates).to(
            DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".to_owned())),
        ))];
        arrays.extend(
            values
                .into_iter()
                .map(|column| Box::new(PrimitiveArray::from(column)) as Box<dyn Array>),
        );
        write_chunk(path, schema, &Chunk::new(arrays));
    }

    fn write_chunk(path: &Path, schema: Schema, chunk: &Chunk<Box<dyn Array>>) {
        let mut writer = FileWriter::try_new(
            File::create(path).expect("create Feather"),
            schema,
            None,
            WriteOptions { compression: None },
        )
        .expect("writer");
        writer.write(chunk, None).expect("write");
        writer.finish().expect("finish");
    }

    #[test]
    fn stable_sorts_but_preserves_duplicates_nulls_and_nans() {
        let temporary = tempfile::tempdir().expect("temporary");
        let path = temporary.path().join("BTC_USDT-5m.feather");
        write_ohlcv(
            &path,
            vec![Some(300), Some(100), Some(100), Some(200)],
            [
                vec![Some(30.0), Some(10.0), Some(11.0), None],
                vec![Some(31.0), Some(12.0), Some(13.0), Some(f64::NAN)],
                vec![Some(29.0), Some(9.0), Some(8.0), Some(19.0)],
                vec![Some(30.5), Some(10.5), Some(11.5), Some(20.0)],
                vec![Some(3.0), Some(1.0), Some(2.0), Some(4.0)],
            ],
        );

        let frame = load_raw_ohlcv_frame(&input(&path, "BTC/USDT", "5m")).expect("frame");

        assert_eq!(frame.timestamps_ms, [100, 100, 200, 300]);
        assert_eq!(
            frame.columns["open"],
            [Some(10.0), Some(11.0), None, Some(30.0)]
        );
        assert!(frame.columns["high"][2].expect("present NaN").is_nan());
        assert_eq!(
            frame.columns["volume"],
            [Some(1.0), Some(2.0), Some(4.0), Some(3.0)]
        );
    }

    #[test]
    fn builds_the_explicit_five_timeframe_catalog_without_substitution() {
        let temporary = tempfile::tempdir().expect("temporary");
        let mut inputs = Vec::new();
        for (index, timeframe) in ["5m", "15m", "1h", "4h", "1d"].into_iter().enumerate() {
            let path = temporary
                .path()
                .join(format!("BTC_USDT-{timeframe}.feather"));
            let value = f64::from(u32::try_from(index).expect("small test index"));
            write_ohlcv(
                &path,
                vec![Some(1_700_000_000_000)],
                std::array::from_fn(|_| vec![Some(value)]),
            );
            inputs.push(input(&path, "BTC/USDT", timeframe));
        }

        let catalog = load_raw_ohlcv_catalog(inputs).expect("catalog");

        assert_eq!(catalog.len(), 5);
        for timeframe in ["5m", "15m", "1h", "4h", "1d"] {
            let requested = identity("BTC/USDT", timeframe);
            let frame = catalog
                .lookup(
                    &requested,
                    &SourceLocation::new("lookup", "strategy.py", 1, 0),
                )
                .expect("exact frame");
            assert_eq!(frame.identity, requested);
        }
    }

    #[test]
    fn catalog_rejects_identity_mismatch_and_duplicate_declarations() {
        let temporary = tempfile::tempdir().expect("temporary");
        let path = temporary.path().join("BTC_USDT-5m.feather");
        write_ohlcv(
            &path,
            vec![Some(0)],
            std::array::from_fn(|_| vec![Some(1.0)]),
        );
        let source = input(&path, "BTC/USDT", "5m");
        let frame = load_raw_ohlcv_frame(&source).expect("frame");
        let mismatch = FrameCatalog::new([(identity("ETH/USDT", "5m"), frame)])
            .expect_err("identity mismatch");
        assert!(mismatch
            .to_string()
            .contains("differs from stored frame identity"));

        let error =
            load_raw_ohlcv_catalog([source.clone(), source]).expect_err("duplicate declaration");
        assert!(error.to_string().contains("duplicate identity BTC/USDT 5m"));
    }

    #[test]
    fn null_date_and_schema_errors_are_source_located() {
        let temporary = tempfile::tempdir().expect("temporary");
        let null_path = temporary.path().join("null-date.feather");
        write_ohlcv(
            &null_path,
            vec![None],
            std::array::from_fn(|_| vec![Some(1.0)]),
        );
        let null_error =
            load_raw_ohlcv_frame(&input(&null_path, "BTC/USDT", "5m")).expect_err("null date");
        let message = null_error.to_string();
        assert!(message.contains("NostalgiaForInfinityX7.py:2200:16"));
        assert!(message.contains("column \"date\" contains null at row 0"));

        let wrong_type_path = temporary.path().join("wrong-type.feather");
        let schema = Schema::from(
            [Field::new(DATE_COLUMN, DataType::Int64, false)]
                .into_iter()
                .chain(VALUE_COLUMNS.map(|name| Field::new(name, DataType::Float64, true)))
                .collect::<Vec<_>>(),
        );
        let arrays =
            std::iter::once(Box::new(PrimitiveArray::from_vec(vec![0_i64])) as Box<dyn Array>)
                .chain(
                    (0..5).map(|_| {
                        Box::new(PrimitiveArray::from_vec(vec![1.0_f64])) as Box<dyn Array>
                    }),
                )
                .collect();
        write_chunk(&wrong_type_path, schema, &Chunk::new(arrays));
        let type_error = load_raw_ohlcv_frame(&input(&wrong_type_path, "BTC/USDT", "5m"))
            .expect_err("wrong date type");
        let message = type_error.to_string();
        assert!(message.contains("NostalgiaForInfinityX7.py:2200:16"));
        assert!(message.contains("expected timestamp[ms]"));
    }
}
