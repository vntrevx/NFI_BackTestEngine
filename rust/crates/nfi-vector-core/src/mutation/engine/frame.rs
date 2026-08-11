use std::collections::BTreeMap;

use crate::column::OwnedColumn;
use crate::VectorCoreError;

/// Complete typed dataframe state before the M21 in-memory simulator handoff.
#[derive(Clone, Debug)]
pub struct MutationFrame {
    pub(super) rows: usize,
    pub(super) columns: BTreeMap<String, OwnedColumn>,
}

impl MutationFrame {
    /// Validate a complete frame with one shared row count.
    ///
    /// # Errors
    ///
    /// Returns a column-length error before any mutation runs.
    pub fn new(columns: BTreeMap<String, OwnedColumn>) -> Result<Self, VectorCoreError> {
        let rows = columns.values().next().map_or(0, OwnedColumn::len);
        for (name, column) in &columns {
            if column.len() != rows {
                return Err(VectorCoreError::ColumnLength {
                    column: name.clone(),
                    actual: column.len(),
                    expected: rows,
                });
            }
        }
        Ok(Self { rows, columns })
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
    pub fn columns(&self) -> &BTreeMap<String, OwnedColumn> {
        &self.columns
    }

    #[must_use]
    pub fn column(&self, name: &str) -> Option<&OwnedColumn> {
        self.columns.get(name)
    }

    pub(super) fn write(
        &mut self,
        name: String,
        column: OwnedColumn,
    ) -> Result<(), VectorCoreError> {
        if column.len() != self.rows {
            return Err(VectorCoreError::ColumnLength {
                column: name,
                actual: column.len(),
                expected: self.rows,
            });
        }
        self.columns.insert(name, column);
        Ok(())
    }
}
