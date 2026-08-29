//! Focused Python bridge for qualifying a compiled mutation program.

use std::collections::{BTreeMap, BTreeSet};

use nfi_vector_core::column::OwnedColumn;
use nfi_vector_core::mutation::{MutationEngine, MutationFrame, MutationProgram};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

#[derive(Clone, Debug)]
enum InputScalar {
    Null,
    Bool(bool),
    Integer(i64),
    Float(f64),
    Text(String),
}

/// Execute one already-compiled Signal or Tag program without strategy Python.
///
/// Inputs preserve homogeneous Python scalar types. Mixed object columns use
/// pandas nullable-string rendering, which is the only mixed-type operation the
/// compiled mutation contract supports.
#[pyfunction]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python containers.
pub(super) fn execute_numeric_mutation_program(
    py: Python<'_>,
    program: &str,
    columns: &Bound<'_, PyDict>,
    metadata: BTreeMap<String, String>,
    requested_outputs: Vec<String>,
) -> PyResult<Py<PyAny>> {
    let program = MutationProgram::from_json(program)
        .map_err(|error| rejected("mutation program", &error.to_string()))?;
    let typed_columns = columns
        .iter()
        .map(|(name, values)| {
            let name = name.extract::<String>()?;
            let values = values
                .cast::<PyList>()
                .map_err(|_| PyValueError::new_err("mutation columns must contain lists"))?;
            Ok((name, input_column(values)?))
        })
        .collect::<PyResult<BTreeMap<_, _>>>()?;
    let source = MutationFrame::new(typed_columns)
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

fn input_column(values: &Bound<'_, PyList>) -> PyResult<OwnedColumn> {
    let scalars = values
        .iter()
        .map(|value| input_scalar(&value))
        .collect::<PyResult<Vec<_>>>()?;
    let has_text = scalars
        .iter()
        .any(|value| matches!(value, InputScalar::Text(_)));
    let has_bool = scalars
        .iter()
        .any(|value| matches!(value, InputScalar::Bool(_)));
    let has_integer = scalars
        .iter()
        .any(|value| matches!(value, InputScalar::Integer(_)));
    let has_float = scalars
        .iter()
        .any(|value| matches!(value, InputScalar::Float(_)));
    if has_text || (has_bool && (has_integer || has_float)) {
        return Ok(OwnedColumn::text(
            scalars.iter().map(nullable_string).collect(),
        ));
    }
    if has_bool {
        return Ok(OwnedColumn::boolean(
            scalars
                .iter()
                .map(|value| match value {
                    InputScalar::Bool(value) => Some(*value),
                    InputScalar::Null
                    | InputScalar::Integer(_)
                    | InputScalar::Float(_)
                    | InputScalar::Text(_) => None,
                })
                .collect(),
        ));
    }
    if has_float {
        let numbers = scalars
            .iter()
            .map(|value| match value {
                InputScalar::Null | InputScalar::Bool(_) | InputScalar::Text(_) => Ok(None),
                InputScalar::Integer(value) => value
                    .to_string()
                    .parse::<f64>()
                    .map(Some)
                    .map_err(|error| PyValueError::new_err(error.to_string())),
                InputScalar::Float(value) => Ok(Some(*value)),
            })
            .collect::<PyResult<Vec<_>>>()?;
        return Ok(OwnedColumn::f64(numbers));
    }
    if has_integer {
        return Ok(OwnedColumn::i64(
            scalars
                .iter()
                .map(|value| match value {
                    InputScalar::Integer(value) => Some(*value),
                    InputScalar::Null
                    | InputScalar::Bool(_)
                    | InputScalar::Float(_)
                    | InputScalar::Text(_) => None,
                })
                .collect(),
        ));
    }
    Ok(OwnedColumn::f64(vec![None; scalars.len()]))
}

fn input_scalar(value: &Bound<'_, PyAny>) -> PyResult<InputScalar> {
    if value.is_none() || value.get_type().name()?.to_str()? == "NAType" {
        return Ok(InputScalar::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return value.extract().map(InputScalar::Bool);
    }
    if value.is_instance_of::<PyInt>() {
        return value.extract().map(InputScalar::Integer);
    }
    if value.is_instance_of::<PyFloat>() {
        return value.extract().map(InputScalar::Float);
    }
    if value.is_instance_of::<PyString>() {
        return value.extract().map(InputScalar::Text);
    }
    Err(PyValueError::new_err(format!(
        "mutation input scalar type {:?} is unsupported",
        value.get_type().name()?
    )))
}

fn nullable_string(value: &InputScalar) -> Option<String> {
    match value {
        InputScalar::Null => None,
        InputScalar::Bool(value) => Some(if *value { "True" } else { "False" }.to_owned()),
        InputScalar::Integer(value) => Some(value.to_string()),
        InputScalar::Float(value) => {
            if value.is_nan() {
                return None;
            }
            let mut rendered = value.to_string();
            if value.fract() == 0.0 && !rendered.contains(['e', 'E']) {
                rendered.push_str(".0");
            }
            Some(rendered)
        }
        InputScalar::Text(value) => Some(value.clone()),
    }
}

fn rejected(context: &str, error: &str) -> PyErr {
    PyValueError::new_err(format!("{context} rejected: {error}"))
}
