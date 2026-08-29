use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Seek, Write};
use std::path::Path;

use nfi_sim_core::{PortfolioBoundary, PortfolioBoundaryEvent, SimulationInput};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const ENVELOPE_VERSION: &str = "native-portfolio-events-v1";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PortfolioEnvelopeRequest {
    fixture_id: String,
    fixture_manifest_sha256: String,
    scheduler_contract_sha256: String,
    scheduler_contract_fingerprint: String,
    portfolio_contract_sha256: String,
    portfolio_contract_fingerprint: String,
    source_sha256: String,
    config_sha256: String,
    data_sha256: String,
    official_trace_sha256: String,
    native_input_sha256: String,
    native_timerange: String,
    configured_pairs: Vec<String>,
    slot_limit: usize,
}

#[derive(Serialize)]
struct PortfolioEnvelopeHeader<'request> {
    #[serde(flatten)]
    request: &'request PortfolioEnvelopeRequest,
    native_binary_sha256: String,
}

#[derive(Serialize)]
struct FinalTrade<'event> {
    force_exit_sequence: usize,
    trade_id: u64,
    pair: &'event str,
    order_ids: &'event [u64],
}

#[derive(Serialize)]
struct PortfolioEnvelope<'request, 'event> {
    schema_version: &'static str,
    portfolio_header: PortfolioEnvelopeHeader<'request>,
    portfolio_events: &'event [PortfolioBoundaryEvent],
    final_force_exit_trade_ids: Vec<u64>,
    final_trades: Vec<FinalTrade<'event>>,
}

pub(crate) fn load_request(
    path: &Path,
    input_path: &Path,
    input: &SimulationInput,
) -> Result<PortfolioEnvelopeRequest, String> {
    let encoded = std::fs::read(path).map_err(|error| {
        format!(
            "cannot read portfolio envelope request {}: {error}",
            path.display()
        )
    })?;
    let request: PortfolioEnvelopeRequest = serde_json::from_slice(&encoded).map_err(|error| {
        format!(
            "invalid portfolio envelope request {}: {error}",
            path.display()
        )
    })?;
    validate_request(&request, input)?;
    let input_sha256 = sha256_reader(BufReader::new(File::open(input_path).map_err(|error| {
        format!("cannot read Native input {}: {error}", input_path.display())
    })?))?;
    if request.native_input_sha256 != input_sha256 {
        return Err("portfolio envelope Native input identity differs".to_owned());
    }
    Ok(request)
}

fn validate_request(
    request: &PortfolioEnvelopeRequest,
    input: &SimulationInput,
) -> Result<(), String> {
    let hashes = [
        &request.fixture_manifest_sha256,
        &request.scheduler_contract_sha256,
        &request.scheduler_contract_fingerprint,
        &request.portfolio_contract_sha256,
        &request.portfolio_contract_fingerprint,
        &request.source_sha256,
        &request.config_sha256,
        &request.data_sha256,
        &request.official_trace_sha256,
        &request.native_input_sha256,
    ];
    if request.fixture_id.is_empty()
        || request.native_timerange.is_empty()
        || hashes.into_iter().any(|value| !is_sha256(value))
    {
        return Err("portfolio envelope request has an invalid identity".to_owned());
    }
    let configured = input
        .pairs
        .iter()
        .map(|pair| pair.pair.as_str())
        .collect::<Vec<_>>();
    if request
        .configured_pairs
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        != configured
    {
        return Err(
            "portfolio envelope configured pair order differs from simulation input".to_owned(),
        );
    }
    if request.slot_limit != input.config.max_open_trades {
        return Err("portfolio envelope slot limit differs from simulation input".to_owned());
    }
    Ok(())
}

pub(crate) fn stage_envelope(
    request: &PortfolioEnvelopeRequest,
    events: &[PortfolioBoundaryEvent],
) -> Result<File, String> {
    for (sequence, event) in events.iter().enumerate() {
        if event.sequence != sequence as u64 {
            return Err("portfolio observer sequence is not contiguous".to_owned());
        }
    }
    let force_events = events
        .iter()
        .filter(|event| event.boundary == PortfolioBoundary::ForceExit)
        .collect::<Vec<_>>();
    let mut final_ids = Vec::with_capacity(force_events.len());
    let mut final_trades = Vec::with_capacity(force_events.len());
    for (sequence, event) in force_events.into_iter().enumerate() {
        if event.force_exit_index != Some(sequence) {
            return Err("portfolio force-exit sequence is not contiguous".to_owned());
        }
        let trade_id = event
            .force_exit_trade_id
            .ok_or_else(|| "portfolio force-exit event has no trade identity".to_owned())?;
        if event.force_exit_order_ids.is_empty() {
            return Err("portfolio force-exit event has no order identities".to_owned());
        }
        final_ids.push(trade_id);
        final_trades.push(FinalTrade {
            force_exit_sequence: sequence,
            trade_id,
            pair: &event.pair,
            order_ids: &event.force_exit_order_ids,
        });
    }
    let binary_sha256 = sha256_reader(BufReader::new(
        File::open(
            std::env::current_exe()
                .map_err(|error| format!("cannot identify Native binary: {error}"))?,
        )
        .map_err(|error| format!("cannot read Native binary: {error}"))?,
    ))?;
    let envelope = PortfolioEnvelope {
        schema_version: ENVELOPE_VERSION,
        portfolio_header: PortfolioEnvelopeHeader {
            request,
            native_binary_sha256: binary_sha256,
        },
        portfolio_events: events,
        final_force_exit_trade_ids: final_ids,
        final_trades,
    };
    let temporary = tempfile::tempfile()
        .map_err(|error| format!("cannot stage portfolio envelope: {error}"))?;
    let mut writer = BufWriter::new(
        temporary
            .try_clone()
            .map_err(|error| format!("cannot stage portfolio envelope: {error}"))?,
    );
    serde_json::to_writer(&mut writer, &envelope)
        .map_err(|error| format!("cannot serialize portfolio envelope: {error}"))?;
    writer
        .write_all(b"\n")
        .map_err(|error| format!("cannot serialize portfolio envelope: {error}"))?;
    writer
        .flush()
        .map_err(|error| format!("cannot flush portfolio envelope: {error}"))?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|error| format!("cannot sync portfolio envelope: {error}"))?;
    drop(writer);
    let mut temporary = temporary;
    temporary
        .rewind()
        .map_err(|error| format!("cannot rewind portfolio envelope: {error}"))?;
    Ok(temporary)
}

fn sha256_reader(mut reader: impl Read) -> Result<String, String> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| format!("cannot hash Native binary: {error}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}
