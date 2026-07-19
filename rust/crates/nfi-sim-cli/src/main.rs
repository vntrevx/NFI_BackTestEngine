use std::cell::RefCell;
use std::env;
use std::fs;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use nfi_sim_core::{
    parse_simulation_input, simulate, simulate_profiled, simulate_with_observer,
    simulate_with_observer_profiled, SimulationInput, SimulationProfile, SimulationResult,
};
use nfi_vector_io::{load_vector_manifest, load_vector_manifest_profiled, VectorLoadProfile};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("nfi-sim: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let mut arguments = env::args_os();
    let _program = arguments.next();
    let mut vector_manifest = false;
    let mut profile_output = None;
    let input = loop {
        let argument = arguments.next().ok_or_else(|| usage().to_owned())?;
        if argument == "--vector-manifest" {
            vector_manifest = true;
            continue;
        }
        if argument == "--profile-output" {
            profile_output = Some(
                arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or_else(|| usage().to_owned())?,
            );
            continue;
        }
        break PathBuf::from(argument);
    };
    let output = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| usage().to_owned())?;
    let trace = arguments.next().map(PathBuf::from);
    if arguments.next().is_some() {
        return Err(usage().to_owned());
    }
    if profile_output.is_some() && !vector_manifest {
        return Err("--profile-output requires --vector-manifest".to_owned());
    }

    let (document, input_profile) = if vector_manifest && profile_output.is_some() {
        let (document, profile) = load_vector_manifest_profiled(&input)
            .map_err(|error| format!("invalid vector manifest {}: {error}", input.display()))?;
        (document, Some(profile))
    } else if vector_manifest {
        (
            load_vector_manifest(&input)
                .map_err(|error| format!("invalid vector manifest {}: {error}", input.display()))?,
            None,
        )
    } else {
        let encoded = fs::read(&input)
            .map_err(|error| format!("cannot read {}: {error}", input.display()))?;
        (
            parse_simulation_input(&encoded).map_err(|error| {
                format!("invalid simulation input {}: {error}", input.display())
            })?,
            None,
        )
    };
    let (result, simulation_profile) = if profile_output.is_some() {
        let (result, profile) = run_simulation_profiled(&document, trace)?;
        (result, Some(profile))
    } else {
        (run_simulation(&document, trace)?, None)
    };
    let serialization_started = Instant::now();
    let serialized = serde_json::to_vec(&result)
        .map_err(|error| format!("cannot serialize simulation result: {error}"))?;

    let temporary = output.with_extension("tmp");
    fs::write(&temporary, serialized)
        .map_err(|error| format!("cannot write {}: {error}", temporary.display()))?;
    fs::rename(&temporary, &output).map_err(|error| {
        format!(
            "cannot replace {} with {}: {error}",
            output.display(),
            temporary.display()
        )
    })?;
    if let Some(profile_path) = profile_output {
        let input_profile = input_profile.expect("profile output validated vector input");
        let simulation_profile =
            simulation_profile.expect("profile output selected profiled simulation");
        let profile = profile_document(
            &input_profile,
            &simulation_profile,
            duration_ns(serialization_started.elapsed()),
        );
        let encoded = serde_json::to_vec(&profile)
            .map_err(|error| format!("cannot serialize engine profile: {error}"))?;
        let temporary = profile_path.with_extension("tmp");
        fs::write(&temporary, encoded)
            .map_err(|error| format!("cannot write {}: {error}", temporary.display()))?;
        fs::rename(&temporary, &profile_path)
            .map_err(|error| format!("cannot replace {}: {error}", profile_path.display()))?;
    }
    Ok(())
}

fn usage() -> &'static str {
    "usage: nfi-sim [--vector-manifest] [--profile-output profile.json] \
     <input.json> <output.json> [events.jsonl]"
}

fn run_simulation(
    document: &SimulationInput,
    trace: Option<PathBuf>,
) -> Result<SimulationResult, String> {
    if let Some(trace_path) = trace {
        let trace_file = File::create(&trace_path)
            .map_err(|error| format!("cannot create {}: {error}", trace_path.display()))?;
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
        .map_err(|error| format!("simulation rejected: {error}"))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(format!("cannot write {}: {error}", trace_path.display()));
        }
        writer
            .into_inner()
            .flush()
            .map_err(|error| format!("cannot flush {}: {error}", trace_path.display()))?;
        Ok(result)
    } else {
        simulate(document).map_err(|error| format!("simulation rejected: {error}"))
    }
}

fn run_simulation_profiled(
    document: &SimulationInput,
    trace: Option<PathBuf>,
) -> Result<(SimulationResult, SimulationProfile), String> {
    if let Some(trace_path) = trace {
        let trace_file = File::create(&trace_path)
            .map_err(|error| format!("cannot create {}: {error}", trace_path.display()))?;
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
        .map_err(|error| format!("simulation rejected: {error}"))?;
        if let Some(error) = trace_error.into_inner() {
            return Err(format!("cannot write {}: {error}", trace_path.display()));
        }
        writer
            .into_inner()
            .flush()
            .map_err(|error| format!("cannot flush {}: {error}", trace_path.display()))?;
        Ok(result)
    } else {
        simulate_profiled(document).map_err(|error| format!("simulation rejected: {error}"))
    }
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
