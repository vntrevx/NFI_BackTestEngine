use nfi_sim_core::{simulate, SimulationInput};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyfunction]
fn schema_version() -> &'static str {
    nfi_sim_core::TRADE_SURFACE_SCHEMA_VERSION
}

#[pyfunction]
fn simulator_available() -> bool {
    nfi_sim_core::simulator_available()
}

#[pyfunction]
fn simulate_json(input: &str) -> PyResult<String> {
    let document: SimulationInput = serde_json::from_str(input)
        .map_err(|error| PyValueError::new_err(format!("invalid simulation input: {error}")))?;
    let result = simulate(&document)
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
    serde_json::to_string(&result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))
}

#[pymodule]
fn _rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(schema_version, module)?)?;
    module.add_function(wrap_pyfunction!(simulator_available, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_json, module)?)?;
    Ok(())
}
