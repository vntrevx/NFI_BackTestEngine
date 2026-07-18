use std::cell::RefCell;
use std::env;
use std::fs;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use nfi_sim_core::{simulate, simulate_with_observer, SimulationInput};

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
    let input = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| "usage: nfi-sim <input.json> <output.json>".to_owned())?;
    let output = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| "usage: nfi-sim <input.json> <output.json>".to_owned())?;
    let trace = arguments.next().map(PathBuf::from);
    if arguments.next().is_some() {
        return Err("usage: nfi-sim <input.json> <output.json> [events.jsonl]".to_owned());
    }

    let encoded =
        fs::read(&input).map_err(|error| format!("cannot read {}: {error}", input.display()))?;
    let document: SimulationInput = serde_json::from_slice(&encoded)
        .map_err(|error| format!("invalid simulation input {}: {error}", input.display()))?;
    let result = if let Some(trace_path) = trace {
        let trace_file = File::create(&trace_path)
            .map_err(|error| format!("cannot create {}: {error}", trace_path.display()))?;
        let writer = RefCell::new(BufWriter::new(trace_file));
        let trace_error = RefCell::new(None);
        let result = simulate_with_observer(&document, |event| {
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
        result
    } else {
        simulate(&document).map_err(|error| format!("simulation rejected: {error}"))?
    };
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
    Ok(())
}
