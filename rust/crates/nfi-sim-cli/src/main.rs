use std::cell::RefCell;
use std::env;
use std::fs;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Seek, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::{Duration, Instant};

use nfi_artifact_publish::{
    publish_result, publish_result_events, publish_result_profile, publish_result_profile_events,
    validate_publication,
};
use nfi_sim_core::{
    parse_simulation_input, serialize_simulation_result, simulate, simulate_profiled,
    simulate_with_execution_observer, simulate_with_execution_observer_profiled,
    simulate_with_observer, simulate_with_observer_profiled, simulate_with_observers,
    simulate_with_observers_profiled, simulate_with_portfolio_observer,
    simulate_with_portfolio_observer_profiled, SimulationInput, SimulationProfile,
    SimulationResult,
};
use nfi_vector_io::{
    load_full_native_vector_manifest_profiled,
    load_full_native_vector_manifest_profiled_with_worker_limit, load_vector_manifest,
    load_vector_manifest_profiled,
};

mod portfolio_envelope;

use portfolio_envelope::PortfolioEnvelopeRequest;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum InputKind {
    SimulationJson,
    FeatherVector,
    FullVector,
}

enum TraceOutput {
    None,
    Scheduler,
    Execution,
    Portfolio(PortfolioEnvelopeRequest),
    PortfolioAndScheduler(PortfolioEnvelopeRequest),
}

struct CliArguments {
    input_kind: InputKind,
    profile_output: Option<PathBuf>,
    pair_worker_limit: Option<usize>,
    portfolio_envelope: Option<(PathBuf, PathBuf)>,
    execution_events: bool,
    input: PathBuf,
    output: PathBuf,
    legacy_trace: Option<PathBuf>,
}

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
    let arguments = parse_arguments()?;
    let portfolio_output = prepare_destinations(
        &arguments.output,
        arguments.profile_output.as_ref(),
        arguments.legacy_trace.as_ref(),
        arguments.portfolio_envelope.as_ref(),
    )?;
    let (document, input_profile) = load_input(
        arguments.input_kind,
        &arguments.input,
        arguments.profile_output.is_some(),
        arguments.pair_worker_limit,
    )?;
    let trace_output = if let Some((request_path, _)) = &arguments.portfolio_envelope {
        let request = portfolio_envelope::load_request(request_path, &arguments.input, &document)?;
        if arguments.legacy_trace.is_some() {
            TraceOutput::PortfolioAndScheduler(request)
        } else {
            TraceOutput::Portfolio(request)
        }
    } else if arguments.execution_events {
        TraceOutput::Execution
    } else if arguments.legacy_trace.is_some() {
        TraceOutput::Scheduler
    } else {
        TraceOutput::None
    };
    let run = execute_simulation(&document, arguments.profile_output.is_some(), trace_output)?;
    serialize_and_publish(
        &arguments.output,
        &run,
        input_profile,
        arguments.profile_output.as_deref(),
        arguments.legacy_trace.as_deref(),
        portfolio_output.map(PathBuf::as_path),
    )
}

fn parse_arguments() -> Result<CliArguments, String> {
    let mut values = env::args_os();
    let _program = values.next();
    let mut input_kind = InputKind::SimulationJson;
    let mut profile_output = None;
    let mut pair_worker_limit = None;
    let mut portfolio_envelope = None;
    let mut execution_events = false;
    let input = loop {
        let argument = values.next().ok_or_else(|| usage().to_owned())?;
        if argument == "--vector-manifest" {
            select_input_kind(&mut input_kind, InputKind::FeatherVector)?;
            continue;
        }
        if argument == "--full-vector-manifest" {
            select_input_kind(&mut input_kind, InputKind::FullVector)?;
            continue;
        }
        if argument == "--profile-output" {
            profile_output = Some(
                values
                    .next()
                    .map(PathBuf::from)
                    .ok_or_else(|| usage().to_owned())?,
            );
            continue;
        }
        if argument == "--portfolio-envelope" {
            let request = values
                .next()
                .map(PathBuf::from)
                .ok_or_else(|| usage().to_owned())?;
            let output = values
                .next()
                .map(PathBuf::from)
                .ok_or_else(|| usage().to_owned())?;
            if portfolio_envelope.replace((request, output)).is_some() {
                return Err("--portfolio-envelope must be declared once".to_owned());
            }
            continue;
        }
        if argument == "--execution-events" {
            if execution_events {
                return Err("--execution-events must be declared once".to_owned());
            }
            execution_events = true;
            continue;
        }
        if argument == "--pair-workers" {
            let raw = values.next().ok_or_else(|| usage().to_owned())?;
            let value = raw
                .to_str()
                .ok_or_else(|| "--pair-workers must be a positive integer".to_owned())?
                .parse::<usize>()
                .map_err(|_| "--pair-workers must be a positive integer".to_owned())?;
            if value == 0 || pair_worker_limit.replace(value).is_some() {
                return Err("--pair-workers must be declared once and be positive".to_owned());
            }
            continue;
        }
        break PathBuf::from(argument);
    };
    let output = values
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| usage().to_owned())?;
    let legacy_trace = values.next().map(PathBuf::from);
    if values.next().is_some() {
        return Err(usage().to_owned());
    }
    if profile_output.is_some() && input_kind == InputKind::SimulationJson {
        return Err("--profile-output requires a vector manifest".to_owned());
    }
    if pair_worker_limit.is_some() && input_kind != InputKind::FullVector {
        return Err("--pair-workers requires --full-vector-manifest".to_owned());
    }
    if execution_events && legacy_trace.is_none() {
        return Err("--execution-events requires an event output path".to_owned());
    }
    if execution_events && portfolio_envelope.is_some() {
        return Err("--execution-events cannot be combined with --portfolio-envelope".to_owned());
    }
    Ok(CliArguments {
        input_kind,
        profile_output,
        pair_worker_limit,
        portfolio_envelope,
        execution_events,
        input,
        output,
        legacy_trace,
    })
}

fn execute_simulation(
    document: &SimulationInput,
    profiled: bool,
    trace_output: TraceOutput,
) -> Result<CompletedRun, String> {
    if profiled {
        let run = run_simulation_profiled(document, trace_output)?;
        Ok(CompletedRun {
            value: run.value.0,
            profile: Some(run.value.1),
            events: run.events,
            portfolio: run.portfolio,
        })
    } else {
        let run = run_simulation(document, trace_output)?;
        Ok(CompletedRun {
            value: run.value,
            profile: None,
            events: run.events,
            portfolio: run.portfolio,
        })
    }
}

fn prepare_destinations<'a>(
    output: &Path,
    profile: Option<&PathBuf>,
    events: Option<&PathBuf>,
    portfolio: Option<&'a (PathBuf, PathBuf)>,
) -> Result<Option<&'a PathBuf>, String> {
    let portfolio_output = portfolio.map(|(_, output)| output);
    ensure_destinations_available(output, profile, events, portfolio_output)?;
    Ok(portfolio_output)
}

fn serialize_and_publish(
    output: &std::path::Path,
    run: &CompletedRun,
    input_profile: Option<serde_json::Value>,
    profile_path: Option<&std::path::Path>,
    events_path: Option<&std::path::Path>,
    portfolio_path: Option<&std::path::Path>,
) -> Result<(), String> {
    let serialization_started = Instant::now();
    let serialized = serialize_simulation_result(&run.value)
        .map_err(|error| format!("cannot serialize simulation result: {error}"))?;
    if let Some(profile_path) = profile_path {
        let input_profile = input_profile
            .ok_or_else(|| "profile output validated without an input profile".to_owned())?;
        let simulation_profile = run
            .profile
            .as_ref()
            .ok_or_else(|| "profile output selected without a simulation profile".to_owned())?;
        let profile = profile_document(
            &input_profile,
            simulation_profile,
            duration_ns(serialization_started.elapsed()),
        );
        let encoded = serde_json::to_vec(&profile)
            .map_err(|error| format!("cannot serialize engine profile: {error}"))?;
        publish_outputs(
            output,
            &serialized,
            Some((profile_path, &encoded)),
            events_path.zip(run.events.as_ref()),
        )
    } else {
        publish_outputs(
            output,
            &serialized,
            None,
            events_path.zip(run.events.as_ref()),
        )
    }?;
    if let Some((path, staged)) = portfolio_path.zip(run.portfolio.as_ref()) {
        publish_portfolio(path, staged)?;
    }
    Ok(())
}

fn ensure_destinations_available(
    output: &std::path::Path,
    profile: Option<&PathBuf>,
    events: Option<&PathBuf>,
    portfolio: Option<&PathBuf>,
) -> Result<(), String> {
    for destination in [
        Some(output),
        profile.map(PathBuf::as_path),
        events.map(PathBuf::as_path),
        portfolio.map(PathBuf::as_path),
    ]
    .into_iter()
    .flatten()
    {
        if path_entry_exists(destination)? {
            return Err(format!(
                "destination already exists: {}",
                destination.display()
            ));
        }
    }
    if path_entry_exists(&nfi_artifact_publish::bundle_path(output))? {
        return Err(format!("destination already exists: {}", output.display()));
    }
    Ok(())
}

fn path_entry_exists(path: &std::path::Path) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!("cannot inspect {}: {error}", path.display())),
    }
}

fn publish_outputs(
    output: &std::path::Path,
    result: &[u8],
    profile: Option<(&std::path::Path, &[u8])>,
    events: Option<(&std::path::Path, &File)>,
) -> Result<(), String> {
    let outcome = match (profile, events) {
        (Some((profile_path, encoded)), Some((events_path, staged))) => {
            publish_result_profile_events(
                output,
                result,
                profile_path,
                encoded,
                events_path,
                staged,
            )
            .and_then(|()| validate_publication(output, Some(profile_path), Some(events_path)))
        }
        (Some((profile_path, encoded)), None) => {
            publish_result_profile(output, result, profile_path, encoded)
                .and_then(|()| validate_publication(output, Some(profile_path), None))
        }
        (None, Some((events_path, staged))) => {
            publish_result_events(output, result, events_path, staged)
                .and_then(|()| validate_publication(output, None, Some(events_path)))
        }
        (None, None) => {
            publish_result(output, result).and_then(|()| validate_publication(output, None, None))
        }
    };
    outcome.map_err(|error| format!("cannot publish simulation artifacts: {error}"))
}

fn publish_portfolio(path: &std::path::Path, staged: &File) -> Result<(), String> {
    let mut source = staged
        .try_clone()
        .map_err(|error| format!("cannot read staged portfolio envelope: {error}"))?;
    source
        .rewind()
        .map_err(|error| format!("cannot rewind staged portfolio envelope: {error}"))?;
    let mut destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| {
            format!(
                "cannot publish portfolio envelope {}: {error}",
                path.display()
            )
        })?;
    std::io::copy(&mut source, &mut destination).map_err(|error| {
        format!(
            "cannot publish portfolio envelope {}: {error}",
            path.display()
        )
    })?;
    destination
        .sync_all()
        .map_err(|error| format!("cannot sync portfolio envelope {}: {error}", path.display()))
}

fn usage() -> &'static str {
    "usage: nfi-sim [--vector-manifest | --full-vector-manifest] \
     [--profile-output profile.json] \
     [--pair-workers positive-integer] \
     [--portfolio-envelope request.json envelope.json] \
     [--execution-events] \
     <input.json> <output.json> [events.jsonl]"
}

fn select_input_kind(current: &mut InputKind, requested: InputKind) -> Result<(), String> {
    if *current != InputKind::SimulationJson {
        return Err("engine input kind may be selected only once".to_owned());
    }
    *current = requested;
    Ok(())
}

fn load_input(
    input_kind: InputKind,
    input: &PathBuf,
    profiled: bool,
    pair_worker_limit: Option<usize>,
) -> Result<(SimulationInput, Option<serde_json::Value>), String> {
    match input_kind {
        InputKind::SimulationJson => {
            let encoded = fs::read(input)
                .map_err(|error| format!("cannot read {}: {error}", input.display()))?;
            let document = parse_simulation_input(&encoded).map_err(|error| {
                format!("invalid simulation input {}: {error}", input.display())
            })?;
            Ok((document, None))
        }
        InputKind::FeatherVector if profiled => {
            let (document, profile) = load_vector_manifest_profiled(input)
                .map_err(|error| format!("invalid vector manifest {}: {error}", input.display()))?;
            Ok((document, Some(profile_value(profile)?)))
        }
        InputKind::FeatherVector => {
            let document = load_vector_manifest(input)
                .map_err(|error| format!("invalid vector manifest {}: {error}", input.display()))?;
            Ok((document, None))
        }
        InputKind::FullVector => {
            let loaded = if let Some(limit) = pair_worker_limit {
                load_full_native_vector_manifest_profiled_with_worker_limit(input, limit)
            } else {
                load_full_native_vector_manifest_profiled(input)
            };
            let (document, profile) = loaded.map_err(|error| {
                format!(
                    "invalid full native vector manifest {}: {error}",
                    input.display()
                )
            })?;
            Ok((
                document,
                if profiled {
                    Some(profile_value(profile)?)
                } else {
                    None
                },
            ))
        }
    }
}

fn profile_value(profile: impl serde::Serialize) -> Result<serde_json::Value, String> {
    serde_json::to_value(profile)
        .map_err(|error| format!("cannot serialize input profile: {error}"))
}

struct CompletedRun {
    value: SimulationResult,
    profile: Option<SimulationProfile>,
    events: Option<File>,
    portfolio: Option<File>,
}

struct SimulationRun<T> {
    value: T,
    events: Option<File>,
    portfolio: Option<File>,
}

fn staged_event_writer() -> Result<(File, RefCell<BufWriter<File>>), String> {
    let temporary =
        tempfile::tempfile().map_err(|error| format!("cannot stage event trace: {error}"))?;
    let file = temporary
        .try_clone()
        .map_err(|error| format!("cannot stage event trace: {error}"))?;
    Ok((temporary, RefCell::new(BufWriter::new(file))))
}

fn finish_event_writer(writer: RefCell<BufWriter<File>>) -> Result<(), String> {
    let mut writer = writer.into_inner();
    writer
        .flush()
        .map_err(|error| format!("cannot flush staged event trace: {error}"))?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|error| format!("cannot sync staged event trace: {error}"))
}

fn run_simulation(
    document: &SimulationInput,
    trace: TraceOutput,
) -> Result<SimulationRun<SimulationResult>, String> {
    match trace {
        TraceOutput::None => Ok(SimulationRun {
            value: simulate(document).map_err(|error| format!("simulation rejected: {error}"))?,
            events: None,
            portfolio: None,
        }),
        TraceOutput::Scheduler => run_scheduler_observer(document),
        TraceOutput::Execution => run_execution_observer(document),
        TraceOutput::Portfolio(request) => {
            let mut events = Vec::new();
            let value =
                simulate_with_portfolio_observer(document, |event| events.push(event.clone()))
                    .map_err(|error| format!("simulation rejected: {error}"))?;
            let staged = portfolio_envelope::stage_envelope(&request, &events)?;
            Ok(SimulationRun {
                value,
                events: None,
                portfolio: Some(staged),
            })
        }
        TraceOutput::PortfolioAndScheduler(request) => {
            let (temporary, writer) = staged_event_writer()?;
            let trace_error = RefCell::new(None);
            let mut portfolio_events = Vec::new();
            let value = simulate_with_observers(
                document,
                |event| write_scheduler_event(&writer, &trace_error, event),
                |event| portfolio_events.push(event.clone()),
            )
            .map_err(|error| format!("simulation rejected: {error}"))?;
            finish_scheduler_trace(writer, trace_error)?;
            let portfolio = portfolio_envelope::stage_envelope(&request, &portfolio_events)?;
            Ok(SimulationRun {
                value,
                events: Some(temporary),
                portfolio: Some(portfolio),
            })
        }
    }
}

fn run_scheduler_observer(
    document: &SimulationInput,
) -> Result<SimulationRun<SimulationResult>, String> {
    let (temporary, writer) = staged_event_writer()?;
    let trace_error = RefCell::new(None);
    let value = simulate_with_observer(document, |event| {
        write_scheduler_event(&writer, &trace_error, event);
    })
    .map_err(|error| format!("simulation rejected: {error}"))?;
    finish_scheduler_trace(writer, trace_error)?;
    Ok(SimulationRun {
        value,
        events: Some(temporary),
        portfolio: None,
    })
}

fn run_execution_observer(
    document: &SimulationInput,
) -> Result<SimulationRun<SimulationResult>, String> {
    let (temporary, writer) = staged_event_writer()?;
    let trace_error = RefCell::new(None);
    let value = simulate_with_execution_observer(document, |event| {
        write_scheduler_event(&writer, &trace_error, event);
    })
    .map_err(|error| format!("simulation rejected: {error}"))?;
    finish_scheduler_trace(writer, trace_error)?;
    Ok(SimulationRun {
        value,
        events: Some(temporary),
        portfolio: None,
    })
}

fn run_simulation_profiled(
    document: &SimulationInput,
    trace: TraceOutput,
) -> Result<SimulationRun<(SimulationResult, SimulationProfile)>, String> {
    match trace {
        TraceOutput::None => Ok(SimulationRun {
            value: simulate_profiled(document)
                .map_err(|error| format!("simulation rejected: {error}"))?,
            events: None,
            portfolio: None,
        }),
        TraceOutput::Scheduler => {
            let (temporary, writer) = staged_event_writer()?;
            let trace_error = RefCell::new(None);
            let value = simulate_with_observer_profiled(document, |event| {
                write_scheduler_event(&writer, &trace_error, event);
            })
            .map_err(|error| format!("simulation rejected: {error}"))?;
            finish_scheduler_trace(writer, trace_error)?;
            Ok(SimulationRun {
                value,
                events: Some(temporary),
                portfolio: None,
            })
        }
        TraceOutput::Execution => {
            let (temporary, writer) = staged_event_writer()?;
            let trace_error = RefCell::new(None);
            let value = simulate_with_execution_observer_profiled(document, |event| {
                write_scheduler_event(&writer, &trace_error, event);
            })
            .map_err(|error| format!("simulation rejected: {error}"))?;
            finish_scheduler_trace(writer, trace_error)?;
            Ok(SimulationRun {
                value,
                events: Some(temporary),
                portfolio: None,
            })
        }
        TraceOutput::Portfolio(request) => {
            let mut events = Vec::new();
            let value = simulate_with_portfolio_observer_profiled(document, |event| {
                events.push(event.clone());
            })
            .map_err(|error| format!("simulation rejected: {error}"))?;
            let staged = portfolio_envelope::stage_envelope(&request, &events)?;
            Ok(SimulationRun {
                value,
                events: None,
                portfolio: Some(staged),
            })
        }
        TraceOutput::PortfolioAndScheduler(request) => {
            let (temporary, writer) = staged_event_writer()?;
            let trace_error = RefCell::new(None);
            let mut portfolio_events = Vec::new();
            let value = simulate_with_observers_profiled(
                document,
                |event| write_scheduler_event(&writer, &trace_error, event),
                |event| portfolio_events.push(event.clone()),
            )
            .map_err(|error| format!("simulation rejected: {error}"))?;
            finish_scheduler_trace(writer, trace_error)?;
            let portfolio = portfolio_envelope::stage_envelope(&request, &portfolio_events)?;
            Ok(SimulationRun {
                value,
                events: Some(temporary),
                portfolio: Some(portfolio),
            })
        }
    }
}

fn write_scheduler_event<T: serde::Serialize>(
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

fn finish_scheduler_trace(
    writer: RefCell<BufWriter<File>>,
    trace_error: RefCell<Option<serde_json::Error>>,
) -> Result<(), String> {
    if let Some(error) = trace_error.into_inner() {
        return Err(format!("cannot write staged event trace: {error}"));
    }
    finish_event_writer(writer)
}

fn profile_document(
    input: &serde_json::Value,
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
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    use super::*;

    #[test]
    fn portfolio_envelope_streams_the_production_observer_in_order(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let document = parse_simulation_input(
            br#"{
                "schema_version":"1.0.0",
                "config":{
                    "starting_balance":1000.0,
                    "max_open_trades":1,
                    "stake_amount":100.0,
                    "fee_rate":0.0,
                    "stoploss_ratio":-0.99,
                    "amount_step":0.001,
                    "price_step":0.01
                },
                "pairs":[{
                    "pair":"BTC/USDT",
                    "candles":[
                        {"timestamp_ms":1,"open":100.0,"high":100.0,"low":100.0,"close":100.0,"volume":1.0,"enter_long":{"tag":"entry"}},
                        {"timestamp_ms":2,"open":101.0,"high":101.0,"low":101.0,"close":101.0,"volume":1.0}
                    ]
                }]
            }"#,
        )?;
        let header: PortfolioEnvelopeRequest = serde_json::from_value(serde_json::json!({
            "fixture_id": "fixture",
            "fixture_manifest_sha256": "0".repeat(64),
            "scheduler_contract_sha256": "1".repeat(64),
            "scheduler_contract_fingerprint": "2".repeat(64),
            "portfolio_contract_sha256": "3".repeat(64),
            "portfolio_contract_fingerprint": "4".repeat(64),
            "source_sha256": "5".repeat(64),
            "config_sha256": "6".repeat(64),
            "data_sha256": "7".repeat(64),
            "official_trace_sha256": "8".repeat(64),
            "native_input_sha256": "9".repeat(64),
            "native_timerange": "1-2",
            "configured_pairs": ["BTC/USDT"],
            "slot_limit": 1
        }))?;

        let run = run_simulation(&document, TraceOutput::Portfolio(header))?;
        let staged = run.portfolio.ok_or("portfolio envelope was not staged")?;
        let envelope: serde_json::Value = serde_json::from_reader(staged)?;
        let events = envelope["portfolio_events"]
            .as_array()
            .ok_or("portfolio events are not an array")?;

        assert_eq!(envelope["schema_version"], "native-portfolio-events-v1");
        assert_eq!(envelope["portfolio_header"]["fixture_id"], "fixture");
        assert_eq!(
            envelope["portfolio_header"]["configured_pairs"][0],
            "BTC/USDT"
        );
        assert_eq!(events.first().ok_or("missing first event")?["sequence"], 0);
        assert_eq!(
            events.last().ok_or("missing last event")?["boundary"],
            "force_exit"
        );
        assert_eq!(
            envelope["final_force_exit_trade_ids"],
            serde_json::json!([1])
        );
        assert_eq!(envelope["final_trades"][0]["trade_id"], 1);
        assert_eq!(
            envelope["final_trades"][0]["order_ids"],
            serde_json::json!([1, 2])
        );
        Ok(())
    }

    #[test]
    fn cli_publication_preserves_preexisting_result_and_profile(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        fs::write(&result, b"old-result")?;
        fs::write(&profile, b"old-profile")?;
        let outcome = publish_outputs(
            &result,
            b"new-result",
            Some((&profile, b"new-profile")),
            None,
        );
        assert!(outcome.is_err());
        assert_eq!(fs::read(result)?, b"old-result");
        assert_eq!(fs::read(profile)?, b"old-profile");
        Ok(())
    }

    #[test]
    fn cli_profiled_publication_commits_one_bundle() -> Result<(), Box<dyn std::error::Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        publish_outputs(&result, b"result", Some((&profile, b"profile")), None)?;
        let bundle = nfi_artifact_publish::bundle_path(&result);
        assert_eq!(fs::read(bundle.join("result.json"))?, b"result");
        assert_eq!(fs::read(bundle.join("profile.json"))?, b"profile");
        assert!(bundle.join("publication.json").is_file());
        Ok(())
    }

    #[test]
    fn cli_profiled_events_are_manifest_members() -> Result<(), Box<dyn std::error::Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        let events = directory.path().join("events.jsonl");
        let mut staged = tempfile::NamedTempFile::new_in(directory.path())?;
        staged.write_all(b"event\n")?;
        staged.as_file().sync_all()?;

        publish_outputs(
            &result,
            b"result",
            Some((&profile, b"profile")),
            Some((&events, staged.as_file())),
        )?;

        let bundle = nfi_artifact_publish::bundle_path(&result);
        let manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(bundle.join("publication.json"))?)?;
        assert_eq!(
            manifest["artifacts"]
                .as_array()
                .ok_or("artifacts is not an array")?
                .iter()
                .map(|artifact| artifact["name"].as_str())
                .collect::<Vec<_>>(),
            vec![
                Some("result.json"),
                Some("profile.json"),
                Some("events.jsonl")
            ]
        );
        assert_eq!(fs::read(events)?, b"event\n");
        Ok(())
    }

    #[test]
    fn cli_concurrent_writers_use_a_bounded_start_barrier() -> Result<(), Box<dyn std::error::Error>>
    {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        let (ready_tx, ready_rx) = mpsc::sync_channel(2);
        let (done_tx, done_rx) = mpsc::sync_channel(2);
        let mut triggers = Vec::new();
        let mut children = Vec::new();
        for identity in *b"ab" {
            let result = result.clone();
            let profile = profile.clone();
            let ready_tx = ready_tx.clone();
            let done_tx = done_tx.clone();
            let (trigger_tx, trigger_rx) = mpsc::sync_channel(1);
            triggers.push(trigger_tx);
            children.push(thread::spawn(move || {
                ready_tx.send(()).map_err(|error| error.to_string())?;
                trigger_rx
                    .recv_timeout(Duration::from_secs(5))
                    .map_err(|error| error.to_string())?;
                let outcome =
                    publish_outputs(&result, &[identity], Some((&profile, &[identity])), None);
                done_tx.send(outcome).map_err(|error| error.to_string())
            }));
        }
        ready_rx.recv_timeout(Duration::from_secs(5))?;
        ready_rx.recv_timeout(Duration::from_secs(5))?;
        for trigger in triggers {
            trigger.send(())?;
        }
        let outcomes = [
            done_rx.recv_timeout(Duration::from_secs(5))?,
            done_rx.recv_timeout(Duration::from_secs(5))?,
        ];
        for child in children {
            child
                .join()
                .map_err(|_| "thread panicked")?
                .map_err(|error| error.clone())?;
        }
        assert_eq!(outcomes.iter().filter(|outcome| outcome.is_ok()).count(), 1);
        Ok(())
    }

    #[test]
    fn cli_event_conflict_preserves_every_destination() -> Result<(), Box<dyn std::error::Error>> {
        let directory = tempfile::tempdir()?;
        let result = directory.path().join("result.json");
        let profile = directory.path().join("profile.json");
        let events = directory.path().join("events.jsonl");
        let mut staged = tempfile::NamedTempFile::new_in(directory.path())?;
        staged.write_all(b"new-event")?;
        fs::write(&events, b"old-event")?;

        let outcome = publish_outputs(
            &result,
            b"result",
            Some((&profile, b"profile")),
            Some((&events, staged.as_file())),
        );

        assert!(outcome.is_err());
        assert_eq!(fs::read(events)?, b"old-event");
        assert!(!result.exists() && !profile.exists());
        assert!(!nfi_artifact_publish::bundle_path(&result).exists());
        Ok(())
    }
}
