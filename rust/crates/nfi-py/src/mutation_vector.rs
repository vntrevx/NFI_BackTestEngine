//! Focused Python bridge for qualifying a compiled mutation program.

use std::collections::{BTreeMap, BTreeSet};

use nfi_vector_core::column::OwnedColumn;
use nfi_vector_core::mutation::{MutationEngine, MutationFrame, MutationProgram};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

type NumericColumns = BTreeMap<String, Vec<Option<f64>>>;

/// Execute one already-compiled Signal or Tag program without strategy Python.
///
/// This deliberately accepts only numeric input columns. It is a qualification
/// surface for source-derived NFI Signal inputs, not a second dataframe transport.
#[pyfunction]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python containers.
pub(super) fn execute_numeric_mutation_program(
    py: Python<'_>,
    program: &str,
    columns: NumericColumns,
    metadata: BTreeMap<String, String>,
    requested_outputs: Vec<String>,
) -> PyResult<Py<PyAny>> {
    let program = MutationProgram::from_json(program)
        .map_err(|error| rejected("mutation program", &error.to_string()))?;
    let source = MutationFrame::new(
        columns
            .into_iter()
            .map(|(name, values)| (name, OwnedColumn::f64(values)))
            .collect(),
    )
    .map_err(|error| rejected("mutation input", &error.to_string()))?;
    let output = MutationEngine::new(&program)
        .and_then(|engine| engine.execute_with_metadata(source, &metadata))
        .map_err(|error| rejected("mutation execution", &error.to_string()))?;

    let mut seen = BTreeSet::new();
    let encoded = PyDict::new(py);
    for name in requested_outputs {
        if name.is_empty() || !seen.insert(name.clone()) {
            return Err(PyValueError::new_err(
                "requested mutation outputs must be nonempty and unique",
            ));
        }
        let column = output
            .column(&name)
            .ok_or_else(|| PyValueError::new_err(format!("mutation output {name:?} is missing")))?;
        let value = PyDict::new(py);
        value.set_item("value_type", column.as_view().value_type().label())?;
        super::full_vector::set_column_values(&value, column)?;
        encoded.set_item(name, value)?;
    }
    Ok(encoded.into_any().unbind())
}

fn rejected(context: &str, error: &str) -> PyErr {
    PyValueError::new_err(format!("{context} rejected: {error}"))
}
