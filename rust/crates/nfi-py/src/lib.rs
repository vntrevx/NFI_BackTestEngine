use std::cell::RefCell;
use std::fs;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use nfi_artifact_publish::{
    publish_result, publish_result_events, publish_result_profile, publish_result_profile_events,
    recover_publication_with_events, validate_publication,
};
use nfi_sim_core::{
    parse_simulation_input, serialize_simulation_result, simulate, simulate_profiled,
    simulate_with_execution_observer, simulate_with_execution_observer_profiled,
    simulate_with_observer, simulate_with_observer_profiled, SimulationInput, SimulationProfile,
    SimulationResult,
};
use nfi_vector_io::{
    load_full_native_vector_manifest_profiled,
    load_full_native_vector_manifest_profiled_with_worker_limit, load_vector_manifest,
    load_vector_manifest_profiled,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::Serialize;

mod full_vector;
mod mutation_vector;

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
fn scheduler_contract_json() -> &'static str {
    nfi_sim_core::scheduler_contract_json()
}

#[pyfunction]
fn execution_contract_json() -> &'static str {
    nfi_sim_core::execution_contract_json()
}

#[pyfunction]
fn futures_contract_json() -> &'static str {
    nfi_sim_core::futures_contract_json()
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

#[pyfunction(signature = (output_path, profile_path=None, events_path=None))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn recover_result_publication(
    output_path: PathBuf,
    profile_path: Option<PathBuf>,
    events_path: Option<PathBuf>,
) -> PyResult<bool> {
    recover_publication_with_events(
        &output_path,
        profile_path.as_deref(),
        events_path.as_deref(),
    )
    .map_err(|error| PyValueError::new_err(format!("cannot recover publication: {error}")))
}

#[pyfunction(signature = (output_path, profile_path=None, events_path=None))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn validate_result_publication(
    output_path: PathBuf,
    profile_path: Option<PathBuf>,
    events_path: Option<PathBuf>,
) -> PyResult<()> {
    validate_publication(
        &output_path,
        profile_path.as_deref(),
        events_path.as_deref(),
    )
    .map_err(|error| PyValueError::new_err(format!("cannot validate publication: {error}")))
}

#[pyfunction(signature = (input_path, output_path, events_path=None, execution_events=false))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_file(
    input_path: PathBuf,
    output_path: PathBuf,
    events_path: Option<PathBuf>,
    execution_events: bool,
) -> PyResult<()> {
    let input_display = input_path.display().to_string();
    let encoded = fs::read(input_path)
        .map_err(|error| PyValueError::new_err(format!("cannot read {input_display}: {error}")))?;
    let document = parse_simulation_input(&encoded).map_err(|error| {
        PyValueError::new_err(format!("invalid simulation input {input_display}: {error}"))
    })?;

    let run = run_simulation(&document, events_path, execution_events)?;
    write_result(&output_path, &run.value, run.events.as_ref())
}

#[pyfunction(signature = (
    manifest_path,
    output_path,
    events_path=None,
    execution_events=false
))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_vector_file(
    manifest_path: PathBuf,
    output_path: PathBuf,
    events_path: Option<PathBuf>,
    execution_events: bool,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let document = load_vector_manifest(&manifest_path).map_err(|error| {
        PyValueError::new_err(format!(
            "invalid vector manifest {manifest_display}: {error}"
        ))
    })?;
    let run = run_simulation(&document, events_path, execution_events)?;
    write_result(&output_path, &run.value, run.events.as_ref())
}

#[pyfunction(signature = (
    manifest_path,
    output_path,
    profile_path,
    events_path=None,
    execution_events=false
))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_vector_file_profiled(
    manifest_path: PathBuf,
    output_path: PathBuf,
    profile_path: PathBuf,
    events_path: Option<PathBuf>,
    execution_events: bool,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let (document, input_profile) =
        load_vector_manifest_profiled(&manifest_path).map_err(|error| {
            PyValueError::new_err(format!(
                "invalid vector manifest {manifest_display}: {error}"
            ))
        })?;
    let run = run_simulation_profiled(&document, events_path, execution_events)?;
    write_profiled_result(
        &output_path,
        &profile_path,
        &run.value.0,
        &input_profile,
        &run.value.1,
        run.events.as_ref(),
    )
}

#[pyfunction(signature = (
    manifest_path,
    output_path,
    events_path=None,
    pair_worker_limit=None,
    execution_events=false
))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_full_vector_file(
    manifest_path: PathBuf,
    output_path: PathBuf,
    events_path: Option<PathBuf>,
    pair_worker_limit: Option<usize>,
    execution_events: bool,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let loaded = if let Some(limit) = pair_worker_limit {
        load_full_native_vector_manifest_profiled_with_worker_limit(&manifest_path, limit)
    } else {
        load_full_native_vector_manifest_profiled(&manifest_path)
    };
    let (document, _) = loaded.map_err(|error| {
        PyValueError::new_err(format!(
            "invalid full native vector manifest {manifest_display}: {error}"
        ))
    })?;
    let run = run_simulation(&document, events_path, execution_events)?;
    write_result(&output_path, &run.value, run.events.as_ref())
}

#[pyfunction(signature = (
    manifest_path,
    output_path,
    profile_path,
    events_path=None,
    pair_worker_limit=None,
    execution_events=false
))]
#[allow(clippy::needless_pass_by_value)] // PyO3 extracts owned Python path arguments.
fn simulate_full_vector_file_profiled(
    manifest_path: PathBuf,
    output_path: PathBuf,
    profile_path: PathBuf,
    events_path: Option<PathBuf>,
    pair_worker_limit: Option<usize>,
    execution_events: bool,
) -> PyResult<()> {
    let manifest_display = manifest_path.display().to_string();
    let loaded = if let Some(limit) = pair_worker_limit {
        load_full_native_vector_manifest_profiled_with_worker_limit(&manifest_path, limit)
    } else {
        load_full_native_vector_manifest_profiled(&manifest_path)
    };
    let (document, input_profile) = loaded.map_err(|error| {
        PyValueError::new_err(format!(
            "invalid full native vector manifest {manifest_display}: {error}"
        ))
    })?;
    let run = run_simulation_profiled(&document, events_path, execution_events)?;
    write_profiled_result(
        &output_path,
        &profile_path,
        &run.value.0,
        &input_profile,
        &run.value.1,
        run.events.as_ref(),
    )
}

struct SimulationRun<T> {
    value: T,
    events: Option<StagedEvents>,
}

struct StagedEvents {
    destination: PathBuf,
    temporary: File,
}

fn event_writer(destination: &Path) -> PyResult<(File, RefCell<BufWriter<File>>)> {
    let temporary = tempfile::tempfile().map_err(|error| {
        PyValueError::new_err(format!("cannot stage {}: {error}", destination.display()))
    })?;
    let file = temporary.try_clone().map_err(|error| {
        PyValueError::new_err(format!("cannot stage {}: {error}", destination.display()))
    })?;
    Ok((temporary, RefCell::new(BufWriter::new(file))))
}

fn write_event<T: Serialize>(
    writer: &RefCell<BufWriter<File>>,
    trace_error: &RefCell<Option<serde_json::Error>>,
    event: &T,
) {
    if trace_error.borrow().is_some() {
        return;
    }
    let mut writer = writer.borrow_mut();
    if let Err(error) = serde_json::to_writer(&mut *writer, event)
        .and_then(|()| writer.write_all(b"\n").map_err(serde_json::Error::io))
    {
        *trace_error.borrow_mut() = Some(error);
    }
}

fn finish_event_writer(writer: RefCell<BufWriter<File>>, destination: &Path) -> PyResult<()> {
    let mut writer = writer.into_inner();
    writer.flush().map_err(|error| {
        PyValueError::new_err(format!("cannot flush {}: {error}", destination.display()))
    })?;
    writer.get_ref().sync_all().map_err(|error| {
        PyValueError::new_err(format!("cannot sync {}: {error}", destination.display()))
    })
}

fn run_simulation(
    document: &SimulationInput,
    events_path: Option<PathBuf>,
    execution_events: bool,
) -> PyResult<SimulationRun<SimulationResult>> {
    if let Some(trace_path) = events_path {
        let (temporary, writer) = event_writer(&trace_path)?;
        let trace_error = RefCell::new(None);
        let result = if execution_events {
            simulate_with_execution_observer(document, |event| {
                write_event(&writer, &trace_error, event);
            })
        } else {
            simulate_with_observer(document, |event| {
                write_event(&writer, &trace_error, event);
            })
        }
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(PyValueError::new_err(format!(
                "cannot write {}: {error}",
                trace_path.display()
            )));
        }
        finish_event_writer(writer, &trace_path)?;
        Ok(SimulationRun {
            value: result,
            events: Some(StagedEvents {
                destination: trace_path,
                temporary,
            }),
        })
    } else {
        let value = simulate(document)
            .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        Ok(SimulationRun {
            value,
            events: None,
        })
    }
}

fn run_simulation_profiled(
    document: &SimulationInput,
    events_path: Option<PathBuf>,
    execution_events: bool,
) -> PyResult<SimulationRun<(SimulationResult, SimulationProfile)>> {
    if let Some(trace_path) = events_path {
        let (temporary, writer) = event_writer(&trace_path)?;
        let trace_error = RefCell::new(None);
        let result = if execution_events {
            simulate_with_execution_observer_profiled(document, |event| {
                write_event(&writer, &trace_error, event);
            })
        } else {
            simulate_with_observer_profiled(document, |event| {
                write_event(&writer, &trace_error, event);
            })
        }
        .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(PyValueError::new_err(format!(
                "cannot write {}: {error}",
                trace_path.display()
            )));
        }
        finish_event_writer(writer, &trace_path)?;
        Ok(SimulationRun {
            value: result,
            events: Some(StagedEvents {
                destination: trace_path,
                temporary,
            }),
        })
    } else {
        let value = simulate_profiled(document)
            .map_err(|error| PyValueError::new_err(format!("simulation rejected: {error}")))?;
        Ok(SimulationRun {
            value,
            events: None,
        })
    }
}

fn write_result(
    output_path: &Path,
    result: &SimulationResult,
    events: Option<&StagedEvents>,
) -> PyResult<()> {
    let serialized = serialize_simulation_result(result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))?;
    let outcome = if let Some(events) = events {
        publish_result_events(
            output_path,
            &serialized,
            &events.destination,
            &events.temporary,
        )
    } else {
        publish_result(output_path, &serialized)
    };
    outcome.map_err(|error| PyValueError::new_err(format!("cannot write result: {error}")))
}

fn write_profiled_result<InputProfile: Serialize>(
    output_path: &Path,
    profile_path: &Path,
    result: &SimulationResult,
    input_profile: &InputProfile,
    simulation_profile: &SimulationProfile,
    events: Option<&StagedEvents>,
) -> PyResult<()> {
    let serialization_started = Instant::now();
    let serialized = serialize_simulation_result(result)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize result: {error}")))?;
    let profile = profile_document(
        input_profile,
        simulation_profile,
        duration_ns(serialization_started.elapsed()),
    );
    let encoded_profile = serde_json::to_vec(&profile)
        .map_err(|error| PyValueError::new_err(format!("cannot serialize profile: {error}")))?;
    let outcome = if let Some(events) = events {
        publish_result_profile_events(
            output_path,
            &serialized,
            profile_path,
            &encoded_profile,
            &events.destination,
            &events.temporary,
        )
    } else {
        publish_result_profile(output_path, &serialized, profile_path, &encoded_profile)
    };
    outcome.map_err(|error| {
        PyValueError::new_err(format!("cannot publish result and profile: {error}"))
    })
}

fn profile_document<InputProfile: Serialize>(
    input: &InputProfile,
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

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::process::CommandExt as _;
    #[cfg(unix)]
    use std::process::Command;
    #[cfg(unix)]
    use wait_timeout::ChildExt as _;

    fn exact_overflow_input() -> Result<SimulationInput, Box<dyn std::error::Error>> {
        let encoded = format!(
            r#"{{
                "schema_version":"1.0.0",
                "config":{{
                    "starting_balance":1000.0,
                    "max_open_trades":1,
                    "stake_amount":100.0,
                    "fee_rate":0.001,
                    "stoploss_ratio":-0.01,
                    "amount_step":0.00001,
                    "price_step":0.01
                }},
                "pairs":[{{
                    "pair":"MAX/USDT",
                    "candles":[
                        {{"timestamp_ms":1,"open":1.0,"high":1.0,"low":1.0,"close":1.0,"volume":1.0,"enter_long":{{"tag":null}}}},
                        {{"timestamp_ms":2,"open":{maximum},"high":{maximum},"low":1.0,"close":{maximum},"volume":1.0,"exit_long":{{"reason":"overflow"}}}}
                    ]
                }}]
            }}"#,
            maximum = format_args!("{:e}", f64::MAX)
        );
        Ok(parse_simulation_input(encoded.as_bytes())?)
    }

    #[test]
    fn exact_failure_never_opens_or_truncates_an_unowned_event_destination(
    ) -> Result<(), Box<dyn std::error::Error>> {
        Python::initialize();
        let directory = tempfile::tempdir()?;
        let events = directory.path().join("events.jsonl");
        fs::write(&events, b"other-writer-event")?;
        let document = exact_overflow_input()?;

        let error = run_simulation(&document, Some(events.clone()), false)
            .err()
            .ok_or("exact overflow unexpectedly succeeded")?;

        assert!(error.to_string().contains("code=exact_arithmetic"));
        assert_eq!(fs::read(&events)?, b"other-writer-event");
        assert_eq!(fs::read_dir(directory.path())?.count(), 1);
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn abrupt_termination_after_first_event_sync_leaves_no_staging_orphan(
    ) -> Result<(), Box<dyn std::error::Error>> {
        const CHILD_ENV: &str = "NFI_TEST_ABORT_AFTER_EVENT_SYNC";
        if std::env::var_os(CHILD_ENV).is_some() {
            let destination = std::env::temp_dir().join("events.jsonl");
            let (temporary, writer) = event_writer(&destination)?;
            writer.borrow_mut().write_all(b"{\"event\":1}\n")?;
            finish_event_writer(writer, &destination)?;
            // Keep the anonymous staging descriptor live so process death,
            // rather than ordinary Drop cleanup, owns the assertion.
            std::mem::forget(temporary);
            std::process::abort();
        }

        let directory = tempfile::tempdir()?;
        let mut command = Command::new(std::env::current_exe()?);
        command
            .arg("--exact")
            .arg("tests::abrupt_termination_after_first_event_sync_leaves_no_staging_orphan")
            .arg("--nocapture")
            .env(CHILD_ENV, "1")
            .env("TMPDIR", directory.path())
            .process_group(0);
        let mut child = command.spawn()?;
        let Some(status) = child.wait_timeout(Duration::from_secs(10))? else {
            let group_kill_error = match i32::try_from(child.id()) {
                Ok(id) => nix::sys::signal::killpg(
                    nix::unistd::Pid::from_raw(id),
                    nix::sys::signal::Signal::SIGKILL,
                )
                .err()
                .map(|error| error.to_string()),
                Err(error) => Some(format!("invalid child process-group ID: {error}")),
            };
            let fallback_kill_error = group_kill_error
                .as_ref()
                .and_then(|_| child.kill().err())
                .map(|error| error.to_string());
            let reap_error = child.wait().err().map(|error| error.to_string());
            return Err(format!(
                "event-sync child exceeded 10 seconds; killed process group and reaped child; \
                 group_kill_error={group_kill_error:?} \
                 fallback_kill_error={fallback_kill_error:?} reap_error={reap_error:?}"
            )
            .into());
        };

        assert!(
            !status.success(),
            "child must terminate abruptly after fsync"
        );
        assert_eq!(
            fs::read_dir(directory.path())?.count(),
            0,
            "anonymous event staging must leave no pathname for a verifier to clean"
        );
        Ok(())
    }

    #[test]
    fn profiled_publication_uses_committed_bundle() -> Result<(), Box<dyn std::error::Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        nfi_artifact_publish::publish_result_profile(&result, b"result", &profile, b"profile")?;
        let bundle = nfi_artifact_publish::bundle_path(&result);
        assert_eq!(fs::read(bundle.join("result.json"))?, b"result");
        assert_eq!(fs::read(bundle.join("profile.json"))?, b"profile");
        assert!(bundle.join("publication.json").is_file());
        Ok(())
    }
}

#[pymodule]
fn _rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(schema_version, module)?)?;
    module.add_function(wrap_pyfunction!(simulator_available, module)?)?;
    module.add_function(wrap_pyfunction!(source_fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(scheduler_contract_json, module)?)?;
    module.add_function(wrap_pyfunction!(execution_contract_json, module)?)?;
    module.add_function(wrap_pyfunction!(futures_contract_json, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_json, module)?)?;
    module.add_function(wrap_pyfunction!(recover_result_publication, module)?)?;
    module.add_function(wrap_pyfunction!(validate_result_publication, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_file, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_vector_file, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_vector_file_profiled, module)?)?;
    module.add_function(wrap_pyfunction!(simulate_full_vector_file, module)?)?;
    module.add_function(wrap_pyfunction!(
        simulate_full_vector_file_profiled,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(full_vector::execute_full_vector, module)?)?;
    module.add_function(wrap_pyfunction!(
        mutation_vector::execute_numeric_mutation_program,
        module
    )?)?;
    Ok(())
}
