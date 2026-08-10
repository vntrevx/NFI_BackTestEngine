//! Zero-copy projection of the Arrow columns required by one execution plan.

use std::collections::{BTreeMap, BTreeSet};

use arrow2::array::Array;
use arrow2::chunk::Chunk;
use arrow2::datatypes::Schema;

use crate::column::{ColumnView, ValueType};
use crate::error::VectorCoreError;

/// One typed input requested by a compiled execution plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnRequest {
    pub name: String,
    pub value_type: ValueType,
}

impl ColumnRequest {
    #[must_use]
    pub fn new(name: impl Into<String>, value_type: ValueType) -> Self {
        Self {
            name: name.into(),
            value_type,
        }
    }
}

/// Evidence that batch projection borrowed only the requested Arrow buffers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProjectionProfile {
    pub source_columns: usize,
    pub projected_columns: usize,
    pub materialized_input_columns: usize,
    pub rows: usize,
}

/// Borrowed typed columns for a single Arrow record batch.
#[derive(Debug)]
pub struct BatchView<'a> {
    rows: usize,
    columns: BTreeMap<String, ColumnView<'a>>,
    profile: ProjectionProfile,
}

impl<'a> BatchView<'a> {
    /// Project and type-check only the columns that the execution plan needs.
    ///
    /// # Errors
    ///
    /// Returns a structural, missing-column, type, or row-count error before
    /// any kernel receives the batch.
    pub fn project(
        schema: &Schema,
        batch: &'a Chunk<Box<dyn Array>>,
        requests: &[ColumnRequest],
    ) -> Result<Self, VectorCoreError> {
        if schema.fields.len() != batch.arrays().len() {
            return Err(VectorCoreError::InvalidProgram(format!(
                "Arrow schema has {} fields but batch has {} arrays",
                schema.fields.len(),
                batch.arrays().len()
            )));
        }

        let mut positions = BTreeMap::new();
        for (index, field) in schema.fields.iter().enumerate() {
            if positions.insert(field.name.as_str(), index).is_some() {
                return Err(VectorCoreError::InvalidProgram(format!(
                    "Arrow schema repeats column {}",
                    field.name
                )));
            }
        }

        let mut requested_names = BTreeSet::new();
        let mut columns = BTreeMap::new();
        for request in requests {
            if !requested_names.insert(request.name.as_str()) {
                return Err(VectorCoreError::InvalidProgram(format!(
                    "execution plan repeats input column {}",
                    request.name
                )));
            }
            let index = positions
                .get(request.name.as_str())
                .copied()
                .ok_or_else(|| VectorCoreError::MissingColumn(request.name.clone()))?;
            let array = batch.arrays()[index].as_ref();
            let view = ColumnView::from_array(&request.name, array, request.value_type)?;
            if view.len() != batch.len() {
                return Err(VectorCoreError::ColumnLength {
                    column: request.name.clone(),
                    actual: view.len(),
                    expected: batch.len(),
                });
            }
            columns.insert(request.name.clone(), view);
        }

        Ok(Self {
            rows: batch.len(),
            profile: ProjectionProfile {
                source_columns: schema.fields.len(),
                projected_columns: columns.len(),
                materialized_input_columns: 0,
                rows: batch.len(),
            },
            columns,
        })
    }

    #[must_use]
    pub const fn len(&self) -> usize {
        self.rows
    }

    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.rows == 0
    }

    #[must_use]
    pub fn column(&self, name: &str) -> Option<ColumnView<'a>> {
        self.columns.get(name).copied()
    }

    #[must_use]
    pub const fn profile(&self) -> ProjectionProfile {
        self.profile
    }
}

#[cfg(test)]
mod tests {
    use arrow2::array::{PrimitiveArray, Utf8Array};
    use arrow2::datatypes::{DataType, Field};

    use super::*;

    #[test]
    fn projection_borrows_only_requested_columns() {
        let schema = Schema::from(vec![
            Field::new("close", DataType::Float64, true),
            Field::new("unused_text", DataType::Utf8, true),
        ]);
        let batch = Chunk::new(vec![
            Box::new(PrimitiveArray::<f64>::from_vec(vec![1.0, 2.0])) as Box<dyn Array>,
            Box::new(Utf8Array::<i32>::from_slice(["ignored", "ignored"])) as Box<dyn Array>,
        ]);

        let view = BatchView::project(
            &schema,
            &batch,
            &[ColumnRequest::new("close", ValueType::F64)],
        )
        .expect("unused unsupported column is never inspected");

        assert_eq!(
            view.column("close").and_then(|item| item.f64_at(1)),
            Some(2.0)
        );
        assert!(view.column("unused_text").is_none());
        assert_eq!(
            view.profile(),
            ProjectionProfile {
                source_columns: 2,
                projected_columns: 1,
                materialized_input_columns: 0,
                rows: 2,
            }
        );
    }

    #[test]
    fn missing_duplicate_and_wrong_type_requests_fail_closed() {
        let schema = Schema::from(vec![Field::new("close", DataType::Float64, false)]);
        let batch = Chunk::new(vec![
            Box::new(PrimitiveArray::<f64>::from_vec(vec![1.0])) as Box<dyn Array>
        ]);

        assert!(matches!(
            BatchView::project(
                &schema,
                &batch,
                &[ColumnRequest::new("open", ValueType::F64)]
            ),
            Err(VectorCoreError::MissingColumn(column)) if column == "open"
        ));
        assert!(matches!(
            BatchView::project(
                &schema,
                &batch,
                &[
                    ColumnRequest::new("close", ValueType::F64),
                    ColumnRequest::new("close", ValueType::F64),
                ]
            ),
            Err(VectorCoreError::InvalidProgram(_))
        ));
        assert!(matches!(
            BatchView::project(
                &schema,
                &batch,
                &[ColumnRequest::new("close", ValueType::Bool)]
            ),
            Err(VectorCoreError::ColumnType { .. })
        ));
    }
}
