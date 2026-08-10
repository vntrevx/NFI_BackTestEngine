//! Typed, Arrow-backed column views and owned kernel results.

use arrow2::array::{Array, BooleanArray, PrimitiveArray};
use arrow2::datatypes::{DataType, TimeUnit};

use crate::error::VectorCoreError;
use crate::float::canonicalize;

/// Physical value types accepted by the safe vector core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum ValueType {
    F64,
    Bool,
    TimestampMs,
}

impl ValueType {
    #[must_use]
    pub const fn label(self) -> &'static str {
        match self {
            Self::F64 => "Float64",
            Self::Bool => "Boolean",
            Self::TimestampMs => "Timestamp(Millisecond)",
        }
    }
}

/// A zero-copy typed view into one Arrow record batch.
#[derive(Debug, Clone, Copy)]
pub enum ColumnView<'a> {
    F64(&'a PrimitiveArray<f64>),
    Bool(&'a BooleanArray),
    TimestampMs(&'a PrimitiveArray<i64>),
}

impl<'a> ColumnView<'a> {
    /// Validate and downcast an Arrow array without copying its buffers.
    ///
    /// # Errors
    ///
    /// Returns [`VectorCoreError::ColumnType`] when the physical Arrow type
    /// differs from the compiled value type.
    pub fn from_array(
        column: &str,
        array: &'a dyn Array,
        expected: ValueType,
    ) -> Result<Self, VectorCoreError> {
        match expected {
            ValueType::F64 if array.data_type() == &DataType::Float64 => array
                .as_any()
                .downcast_ref::<PrimitiveArray<f64>>()
                .map(Self::F64)
                .ok_or_else(|| type_error(column, array, expected)),
            ValueType::Bool if array.data_type() == &DataType::Boolean => array
                .as_any()
                .downcast_ref::<BooleanArray>()
                .map(Self::Bool)
                .ok_or_else(|| type_error(column, array, expected)),
            ValueType::TimestampMs
                if matches!(
                    array.data_type(),
                    DataType::Timestamp(TimeUnit::Millisecond, _)
                ) =>
            {
                array
                    .as_any()
                    .downcast_ref::<PrimitiveArray<i64>>()
                    .map(Self::TimestampMs)
                    .ok_or_else(|| type_error(column, array, expected))
            }
            _ => Err(type_error(column, array, expected)),
        }
    }

    #[must_use]
    pub fn len(self) -> usize {
        match self {
            Self::F64(values) => values.len(),
            Self::Bool(values) => values.len(),
            Self::TimestampMs(values) => values.len(),
        }
    }

    #[must_use]
    pub fn is_empty(self) -> bool {
        self.len() == 0
    }

    #[must_use]
    pub fn value_type(self) -> ValueType {
        match self {
            Self::F64(_) => ValueType::F64,
            Self::Bool(_) => ValueType::Bool,
            Self::TimestampMs(_) => ValueType::TimestampMs,
        }
    }

    #[must_use]
    pub fn f64_at(self, row: usize) -> Option<f64> {
        match self {
            Self::F64(values) if row < values.len() && values.is_valid(row) => {
                Some(canonicalize(values.value(row)))
            }
            _ => None,
        }
    }

    #[must_use]
    pub fn bool_at(self, row: usize) -> Option<bool> {
        match self {
            Self::Bool(values) if row < values.len() && values.is_valid(row) => {
                Some(values.value(row))
            }
            _ => None,
        }
    }

    #[must_use]
    pub fn timestamp_ms_at(self, row: usize) -> Option<i64> {
        match self {
            Self::TimestampMs(values) if row < values.len() && values.is_valid(row) => {
                Some(values.value(row))
            }
            _ => None,
        }
    }
}

/// Arrow-owned output produced for the current batch only.
#[derive(Debug, Clone)]
pub enum OwnedColumn {
    F64(PrimitiveArray<f64>),
    Bool(BooleanArray),
    TimestampMs(PrimitiveArray<i64>),
}

impl OwnedColumn {
    #[must_use]
    pub fn f64(values: Vec<Option<f64>>) -> Self {
        Self::F64(PrimitiveArray::from(
            values
                .into_iter()
                .map(|value| value.map(canonicalize))
                .collect::<Vec<_>>(),
        ))
    }

    #[must_use]
    pub fn boolean(values: Vec<Option<bool>>) -> Self {
        Self::Bool(BooleanArray::from(values))
    }

    #[must_use]
    pub fn timestamp_ms(values: Vec<Option<i64>>) -> Self {
        Self::TimestampMs(
            PrimitiveArray::from(values).to(DataType::Timestamp(TimeUnit::Millisecond, None)),
        )
    }

    #[must_use]
    pub fn as_view(&self) -> ColumnView<'_> {
        match self {
            Self::F64(values) => ColumnView::F64(values),
            Self::Bool(values) => ColumnView::Bool(values),
            Self::TimestampMs(values) => ColumnView::TimestampMs(values),
        }
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.as_view().len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Conservative live-buffer accounting used by memory-bound tests.
    #[must_use]
    pub fn estimated_bytes(&self) -> usize {
        let validity_bytes = self.len().div_ceil(8);
        match self {
            Self::F64(_) | Self::TimestampMs(_) => self
                .len()
                .saturating_mul(std::mem::size_of::<u64>())
                .saturating_add(validity_bytes),
            Self::Bool(_) => self.len().div_ceil(8).saturating_add(validity_bytes),
        }
    }
}

fn type_error(column: &str, array: &dyn Array, expected: ValueType) -> VectorCoreError {
    VectorCoreError::ColumnType {
        column: column.to_owned(),
        actual: format!("{:?}", array.data_type()),
        expected: expected.label(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn f64_outputs_canonicalize_nan_without_losing_null_or_signed_zero() {
        let column = OwnedColumn::f64(vec![
            None,
            Some(-0.0),
            Some(f64::from_bits(0x7ff0_0000_0000_0001)),
        ]);
        let view = column.as_view();
        assert_eq!(view.f64_at(0), None);
        assert_eq!(
            view.f64_at(1).expect("signed zero").to_bits(),
            (-0.0_f64).to_bits()
        );
        assert_eq!(
            view.f64_at(2).expect("canonical NaN").to_bits(),
            crate::float::CANONICAL_NAN_BITS
        );
    }

    #[test]
    fn timestamp_requires_millisecond_arrow_type() {
        let raw = PrimitiveArray::<i64>::from_vec(vec![1]);
        let error = ColumnView::from_array("date", &raw, ValueType::TimestampMs)
            .expect_err("plain Int64 is not a timestamp");
        assert!(matches!(error, VectorCoreError::ColumnType { .. }));

        let timestamp = raw.to(DataType::Timestamp(TimeUnit::Millisecond, None));
        assert_eq!(
            ColumnView::from_array("date", &timestamp, ValueType::TimestampMs)
                .expect("millisecond timestamp")
                .timestamp_ms_at(0),
            Some(1)
        );
    }
}
