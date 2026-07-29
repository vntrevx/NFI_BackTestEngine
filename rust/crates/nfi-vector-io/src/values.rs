//! Typed Arrow scalar extraction with Python-compatible null semantics.

use std::collections::BTreeMap;

use arrow2::array::{Array, PrimitiveArray, Utf8Array};
use arrow2::chunk::Chunk;
use arrow2::datatypes::{DataType, TimeUnit};

use crate::VectorInputError;

const EMPTY_TAG_TRANSPORT_SENTINEL: &str = "__nfi_bte_empty_tag_column__";

pub(crate) fn column<'a>(
    batch: &'a Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    name: &str,
) -> &'a dyn Array {
    batch.arrays()[positions[name]].as_ref()
}

pub(crate) fn optional_column<'a>(
    batch: &'a Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    name: &str,
) -> Option<&'a dyn Array> {
    positions
        .get(name)
        .map(|index| batch.arrays()[*index].as_ref())
}

pub(crate) fn required_timestamp_ms(
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
pub(crate) fn required_number(
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

pub(crate) fn primitive_value<T: arrow2::types::NativeType>(array: &dyn Array, row: usize) -> T {
    array
        .as_any()
        .downcast_ref::<PrimitiveArray<T>>()
        .expect("numeric physical type matches Arrow data type")
        .value(row)
}

pub(crate) fn is_numeric_type(data_type: &DataType) -> bool {
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

pub(crate) fn enabled(
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

pub(crate) fn optional_number(
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

pub(crate) fn optional_text(
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
    Ok((!value.is_empty() && value != EMPTY_TAG_TRANSPORT_SENTINEL).then(|| value.to_owned()))
}
