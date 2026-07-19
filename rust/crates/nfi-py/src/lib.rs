use std::cell::RefCell;
use std::fs;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use nfi_sim_core::{
    parse_simulation_input, simulate, simulate_profiled, simulate_with_observer,
    simulate_with_observer_profiled, SimulationInput, SimulationProfile, SimulationResult,
};
use nfi_vector_io::{load_vector_manifest, load_vector_manifest_profiled, VectorLoadProfile};
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
fn source_fingerprint() -> &'static str {
    env!("NFI_RUST_SOURCE_FINGERPRINT")
}

#[pyfunction]
fn simulate_json(input: &str) -> PyResult<String> {
    let document = parse_simulation_input(input.as_bytes())
        .map_err(|error| PyValueError::new_err(format!("invalid simulation input: {error}")))?;
    let result = simulate(&document)
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
    serde_json::to_string(&result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))
}

#[pyfunction(signature = (input_path, output_path, events_path=None))]
fn simulate_file(
    input_path: PathBuf,
    output_path: PathBuf,
    events_path: Option<PathBuf>,
) -> PyResult<()> {
    let input_display = input_path.display().to_string();
    let encoded = fs::read(input_path)
        .map_err(|error| PyValueError::new_err(format!("cannot read {input_display}: {error}")))?;
    let document = parse_simulation_input(&encoded).map_err(|error| {
        PyValueError::new_err(format!("invalid simulation input {input_display}: {error}"))
    })?;

    let result = run_simulation(&document, events_path)?;
    write_result(output_path, &result)
}

#[pyfunction(signature = (manifest_path, output_path, events_path=None))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_vector_file(
    manifest_path: PathBuf,
    output_path: PathBuf,
    events_path: Option<PathBuf>,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let document = load_vector_manifest(&manifest_path).map_err(|error| {
        PyValueError::new_err(format!(
            "invalid vector manifest {manifest_display}: {error}"
        ))
    })?;
    let result = run_simulation(&document, events_path)?;
    write_result(output_path, &result)
}

#[pyfunction(signature = (manifest_path, output_path, profile_path, events_path=None))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_vector_file_profiled(
    manifest_path: PathBuf,
    output_path: PathBuf,
    profile_path: PathBuf,
    events_path: Option<PathBuf>,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let (document, input_profile) =
        load_vector_manifest_profiled(&manifest_path).map_err(|error| {
            PyValueError::new_err(format!(
                "invalid vector manifest {manifest_display}: {error}"
            ))
        })?;
    let (result, simulation_profile) = run_simulation_profiled(&document, events_path)?;
    let serialization_started = Instant::now();
    let serialized = serde_json::to_vec(&result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))?;
    atomic_write(output_path.clone(), &serialized)
        .map_err(|error| PyValueError::new_err(format!("cannot write result: {error}")))?;
    let profile = profile_document(
        &input_profile,
        &simulation_profile,
        duration_ns(serialization_started.elapsed()),
    );
    let encoded_profile = serde_json::to_vec(&profile)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize profile: {error}")))?;
    if let Err(error) = atomic_write(profile_path, &encoded_profile) {
        let _ = fs::remove_file(output_path);
        return Err(PyValueError::new_err(format!(
            "cannot write engine profile: {error}"
        )));
    }
    Ok(())
}

fn run_simulation(
    document: &SimulationInput,
    events_path: Option<PathBuf>,
) -> PyResult<SimulationResult> {
    if let Some(trace_path) = events_path {
        let trace_file = File::create(&trace_path).map_err(|error| {
            PyValueError::new_err(format!("cannot create {}: {error}", trace_path.display()))
        })?;
        let writer = RefCell::new(BufWriter::new(trace_file));
        let trace_error = RefCell::new(None);
        let result = simulate_with_observer(document, |event| {
            if trace_error.borrow().is_some() {
                return;
            }
            let mut writer = writer.borrow_mut();
            if let Err(error) = serde_json::to_writer(&mut *writer, event)
                .and_then(|()| writer.write_all(b"\n").map_err(serde_json::Error::io))
            {
                *trace_error.borrow_mut() = Some(error);
            }
        })
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(PyValueError::new_err(format!(
                "cannot write {}: {error}",
                trace_path.display()
            )));
        }
        writer.into_inner().flush().map_err(|error| {
            PyValueError::new_err(format!("cannot flush {}: {error}", trace_path.display()))
        })?;
        Ok(result)
    } else {
        simulate(document)
            .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))
    }
}

fn run_simulation_profiled(
    document: &SimulationInput,
    events_path: Option<PathBuf>,
) -> PyResult<(SimulationResult, SimulationProfile)> {
    if let Some(trace_path) = events_path {
        let trace_file = File::create(&trace_path).map_err(|error| {
            PyValueError::new_err(format!("cannot create {}: {error}", trace_path.display()))
        })?;
        let writer = RefCell::new(BufWriter::new(trace_file));
        let trace_error = RefCell::new(None);
        let result = simulate_with_observer_profiled(document, |event| {
            if trace_error.borrow().is_some() {
                return;
            }
            let mut writer = writer.borrow_mut();
            if let Err(error) = serde_json::to_writer(&mut *writer, event)
                .and_then(|()| writer.write_all(b"\n").map_err(serde_json::Error::io))
            {
                *trace_error.borrow_mut() = Some(error);
            }
        })
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(PyValueError::new_err(format!(
                "cannot write {}: {error}",
                trace_path.display()
            )));
        }
        writer.into_inner().flush().map_err(|error| {
            PyValueError::new_err(format!("cannot flush {}: {error}", trace_path.display()))
        })?;
        Ok(result)
    } else {
        simulate_profiled(document)
            .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))
    }
}

fn write_result(output_path: PathBuf, result: &SimulationResult) -> PyResult<()> {
    let serialized = serde_json::to_vec(&result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))?;
    atomic_write(output_path, &serialized)
        .map_err(|error| PyValueError::new_err(format!("cannot write result: {error}")))
}

fn atomic_write(path: PathBuf, contents: &[u8]) -> Result<(), String> {
    let temporary = path.with_extension("tmp");
    let path_display = path.display().to_string();
    fs::write(&temporary, contents)
        .map_err(|error| format!("cannot write {}: {error}", temporary.display()))?;
    fs::rename(&temporary, path).map_err(|error| {
        format!(
            "cannot replace {path_display} with {}: {error}",
            temporary.display()
        )
    })
}

fn profile_document(
    input: &VectorLoadProfile,
    simulation: &SimulationProfile,
    serialization_ns: u64,
) -> serde_json::Value {
    serde_json::json!({
        "schema_version": "1.0.0",
        "input": input,
        "simulation": simulation,
        "serialization_ns": serialization_ns,
    })
}

fn duration_ns(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

#[pymodule]
fn _rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(schema_version, module)?)?;
    module.add_function(wrap_pyfunction!(simulator_available, module)?)?;
    module.add_function(wrap_pyfunction!(source_fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_json, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_file, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_vector_file, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_vector_file_profiled, module)?)?;
    Ok(())
}
