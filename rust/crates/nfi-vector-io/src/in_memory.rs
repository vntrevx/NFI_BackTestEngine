//! Exact in-memory handoff from generic Rust vector output to the simulator.

use std::collections::{BTreeMap, BTreeSet};
use std::mem::size_of;
use std::time::{Duration, Instant};

use nfi_sim_core::{
    Candle, EntrySignal, ExitSignal, FeatureColumn, PairSeries, PortfolioConfig, PriceStepChange,
    SimulationInput, SIMULATOR_SCHEMA_VERSION,
};
use nfi_vector_core::column::{OwnedColumn, ValueType};
use nfi_vector_core::mutation::MutationFrame;
use rayon::prelude::*;
use serde::Serialize;

use crate::VectorInputError;

const EMPTY_TAG_TRANSPORT_SENTINEL: &str = "__nfi_bte_empty_tag_column__";

/// One pair whose complete typed vector frame already lives in Rust memory.
///
/// The frame uses the same `nfi_exec_*` columns as the sealed Feather contract.
/// Pair preparation may run in parallel, while the returned `SimulationInput`
/// preserves this vector's order for the single chronological wallet loop.
#[derive(Debug)]
pub struct InMemoryVectorPair {
    pub pair: String,
    pub execution_start_index: usize,
    pub amount_step: Option<f64>,
    pub price_step: Option<f64>,
    pub price_steps: Vec<PriceStepChange>,
    pub minimum_stake: Option<f64>,
    pub minimum_amount: Option<f64>,
    pub minimum_cost: Option<f64>,
    pub feature_columns: Vec<String>,
    pub options: VectorPairOptions,
    pub frame: MutationFrame,
}

/// Compact pair capabilities shared by the direct and Feather semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VectorPairOptions(u8);

impl Default for VectorPairOptions {
    fn default() -> Self {
        Self(Self::USE_EXIT_SIGNAL)
    }
}

impl VectorPairOptions {
    const CAN_SHORT: u8 = 1;
    const INCLUDE_FUNDING: u8 = 1 << 1;
    const USE_EXIT_SIGNAL: u8 = 1 << 2;
    const INCLUDE_PREVIOUS_CLOSE: u8 = 1 << 3;

    #[must_use]
    pub const fn with_can_short(mut self, enabled: bool) -> Self {
        self.set(Self::CAN_SHORT, enabled);
        self
    }

    #[must_use]
    pub const fn with_funding(mut self, enabled: bool) -> Self {
        self.set(Self::INCLUDE_FUNDING, enabled);
        self
    }

    #[must_use]
    pub const fn with_exit_signal(mut self, enabled: bool) -> Self {
        self.set(Self::USE_EXIT_SIGNAL, enabled);
        self
    }

    #[must_use]
    pub const fn with_previous_close(mut self, enabled: bool) -> Self {
        self.set(Self::INCLUDE_PREVIOUS_CLOSE, enabled);
        self
    }

    const fn set(&mut self, flag: u8, enabled: bool) {
        if enabled {
            self.0 |= flag;
        } else {
            self.0 &= !flag;
        }
    }

    const fn can_short(self) -> bool {
        self.0 & Self::CAN_SHORT != 0
    }

    const fn include_funding(self) -> bool {
        self.0 & Self::INCLUDE_FUNDING != 0
    }

    const fn use_exit_signal(self) -> bool {
        self.0 & Self::USE_EXIT_SIGNAL != 0
    }

    const fn include_previous_close(self) -> bool {
        self.0 & Self::INCLUDE_PREVIOUS_CLOSE != 0
    }
}

/// Wall-clock and retained-memory evidence for the direct transport.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct InMemoryVectorProfile {
    pub schema_version: &'static str,
    pub vector_execute_ns: u64,
    pub pair_prepare_ns: u64,
    pub pair_count: usize,
    pub row_count: usize,
    pub feature_column_count: usize,
    pub estimated_source_column_bytes: usize,
    pub estimated_simulation_owned_bytes: usize,
    pub pair_prepare_worker_limit: usize,
}

struct PreparedPair {
    pair: String,
    execution_start_index: usize,
    amount_step: Option<f64>,
    price_step: Option<f64>,
    price_steps: Vec<PriceStepChange>,
    minimum_stake: Option<f64>,
    minimum_amount: Option<f64>,
    minimum_cost: Option<f64>,
    feature_columns: BTreeMap<String, PreparedFeature>,
    candles: Vec<Candle>,
    rows: usize,
    features: usize,
    source_bytes: usize,
    owned_bytes: usize,
}

enum PreparedFeature {
    Numbers(Vec<f64>),
    Booleans(Vec<bool>),
}

impl PreparedPair {
    fn into_pair_series(self) -> PairSeries {
        PairSeries {
            pair: self.pair,
            execution_start_index: self.execution_start_index,
            amount_step: self.amount_step,
            price_step: self.price_step,
            price_steps: self.price_steps,
            minimum_stake: self.minimum_stake,
            minimum_amount: self.minimum_amount,
            minimum_cost: self.minimum_cost,
            feature_columns: self
                .feature_columns
                .into_iter()
                .map(|(name, values)| {
                    let column = match values {
                        PreparedFeature::Numbers(values) => FeatureColumn::numbers(values),
                        PreparedFeature::Booleans(values) => FeatureColumn::booleans(values),
                    };
                    (name, column)
                })
                .collect(),
            candles: self.candles.into(),
        }
    }
}

/// Convert typed vector outputs directly into the simulator's owned input.
///
/// # Errors
///
/// Returns a missing-column, type, null, duplicate-pair, or range error before
/// the chronological simulator is entered.
pub fn assemble_in_memory_vectors(
    config: PortfolioConfig,
    pairs: Vec<InMemoryVectorPair>,
) -> Result<SimulationInput, VectorInputError> {
    assemble_in_memory_vectors_profiled(config, pairs).map(|(input, _)| input)
}

/// Convert typed vector outputs and report pair-parallel preparation costs.
///
/// # Errors
///
/// Returns the same fail-closed transport errors as
/// [`assemble_in_memory_vectors`].
pub fn assemble_in_memory_vectors_profiled(
    config: PortfolioConfig,
    pairs: Vec<InMemoryVectorPair>,
) -> Result<(SimulationInput, InMemoryVectorProfile), VectorInputError> {
    if pairs.is_empty() {
        return Err(VectorInputError::EmptyPairs);
    }
    let mut names = BTreeSet::new();
    for pair in &pairs {
        if pair.pair.is_empty() || !names.insert(pair.pair.as_str()) {
            return Err(VectorInputError::InvalidPair(pair.pair.clone()));
        }
        validate_feature_names(pair)?;
    }

    let started = Instant::now();
    // Indexed parallel iteration keeps the caller's deterministic pair order.
    // It ends before `SimulationInput` reaches the single-threaded wallet loop.
    let prepared = pairs
        .into_par_iter()
        .map(prepare_pair)
        .collect::<Vec<_>>()
        .into_iter()
        .collect::<Result<Vec<_>, _>>()?;
    let profile = InMemoryVectorProfile {
        schema_version: "1.0.0",
        vector_execute_ns: 0,
        pair_prepare_ns: duration_ns(started.elapsed()),
        pair_count: prepared.len(),
        row_count: prepared
            .iter()
            .map(|pair| pair.rows)
            .fold(0, usize::saturating_add),
        feature_column_count: prepared
            .iter()
            .map(|pair| pair.features)
            .fold(0, usize::saturating_add),
        estimated_source_column_bytes: prepared
            .iter()
            .map(|pair| pair.source_bytes)
            .fold(0, usize::saturating_add),
        estimated_simulation_owned_bytes: prepared
            .iter()
            .map(|pair| pair.owned_bytes)
            .fold(0, usize::saturating_add),
        pair_prepare_worker_limit: rayon::current_num_threads(),
    };
    Ok((
        SimulationInput {
            schema_version: SIMULATOR_SCHEMA_VERSION.to_owned(),
            config,
            pairs: prepared
                .into_iter()
                .map(PreparedPair::into_pair_series)
                .collect(),
        },
        profile,
    ))
}

/// Execute independent pair vector DAGs in parallel, then build one ordered
/// simulator input for the chronological wallet loop.
///
/// The executor receives one caller-defined task and must return the complete
/// typed frame for that pair. Indexed Rayon collection preserves task order;
/// no simulation or wallet mutation occurs inside the executor.
///
/// # Errors
///
/// Returns the first vector or transport error without exposing a partial
/// `SimulationInput`.
pub fn execute_in_memory_pair_dag_profiled<Task, Execute>(
    config: PortfolioConfig,
    tasks: Vec<Task>,
    execute: Execute,
) -> Result<(SimulationInput, InMemoryVectorProfile), VectorInputError>
where
    Task: Send,
    Execute: Fn(Task) -> Result<InMemoryVectorPair, VectorInputError> + Send + Sync,
{
    let started = Instant::now();
    let pairs = tasks
        .into_par_iter()
        .map(execute)
        .collect::<Vec<_>>()
        .into_iter()
        .collect::<Result<Vec<_>, _>>()?;
    let vector_execute_ns = duration_ns(started.elapsed());
    let (input, mut profile) = assemble_in_memory_vectors_profiled(config, pairs)?;
    profile.vector_execute_ns = vector_execute_ns;
    Ok((input, profile))
}

fn prepare_pair(pair: InMemoryVectorPair) -> Result<PreparedPair, VectorInputError> {
    let rows = pair.frame.len();
    if pair.execution_start_index >= rows {
        return Err(VectorInputError::ExecutionStart {
            pair: pair.pair,
            index: pair.execution_start_index,
            rows,
        });
    }
    let source_bytes = pair
        .frame
        .columns()
        .values()
        .map(OwnedColumn::estimated_bytes)
        .fold(0_usize, usize::saturating_add);
    let candles = build_candles(&pair)?;
    let feature_columns = pair
        .feature_columns
        .iter()
        .map(|name| build_feature(&pair, name))
        .collect::<Result<BTreeMap<_, _>, _>>()?;
    let owned_bytes = candles
        .len()
        .saturating_mul(size_of::<Candle>())
        .saturating_add(candle_string_bytes(&candles))
        .saturating_add(pair.pair.len())
        .saturating_add(
            pair.feature_columns
                .iter()
                .map(String::len)
                .fold(0_usize, usize::saturating_add),
        )
        .saturating_add(
            feature_columns
                .values()
                .map(feature_bytes)
                .fold(0_usize, usize::saturating_add),
        );
    Ok(PreparedPair {
        pair: pair.pair,
        execution_start_index: pair.execution_start_index,
        amount_step: pair.amount_step,
        price_step: pair.price_step,
        price_steps: pair.price_steps,
        minimum_stake: pair.minimum_stake,
        minimum_amount: pair.minimum_amount,
        minimum_cost: pair.minimum_cost,
        feature_columns,
        candles,
        rows,
        features: pair.feature_columns.len(),
        source_bytes,
        owned_bytes,
    })
}

fn build_candles(pair: &InMemoryVectorPair) -> Result<Vec<Candle>, VectorInputError> {
    let mut candles = Vec::with_capacity(pair.frame.len());
    let mut previous_close = None;
    for row in 0..pair.frame.len() {
        let close = required_number(pair, "close", row)?;
        let entry_tag = optional_text(pair, "nfi_exec_enter_tag", row)?;
        let exit_tag = optional_text(pair, "nfi_exec_exit_tag", row)?;
        let enter_long = enabled(pair, "nfi_exec_enter_long", row)?;
        let enter_short = if pair.options.can_short() {
            enabled(pair, "nfi_exec_enter_short", row)?
        } else {
            false
        };
        let exit_long = if pair.options.use_exit_signal() {
            enabled(pair, "nfi_exec_exit_long", row)?
        } else {
            false
        };
        let exit_short = if pair.options.can_short() && pair.options.use_exit_signal() {
            enabled(pair, "nfi_exec_exit_short", row)?
        } else {
            false
        };
        let funding_rate = if pair.options.include_funding() {
            optional_number(pair, "nfi_exec_funding_rate", row)?
        } else {
            None
        };
        let funding_mark_price = if pair.options.include_funding() {
            optional_number(pair, "nfi_exec_funding_mark_price", row)?
        } else {
            None
        };
        let exit_reason = || exit_tag.clone().unwrap_or_else(|| "exit_signal".to_owned());
        candles.push(Candle {
            timestamp_ms: required_timestamp(pair, "date", row)?,
            open: required_number(pair, "open", row)?,
            high: required_number(pair, "high", row)?,
            low: required_number(pair, "low", row)?,
            close,
            volume: required_number(pair, "volume", row)?,
            previous_close: pair
                .options
                .include_previous_close()
                .then_some(previous_close)
                .flatten(),
            enter_long: enter_long.then(|| EntrySignal {
                tag: entry_tag.clone(),
                leverage: None,
                liquidation_price: None,
            }),
            enter_short: enter_short.then_some(EntrySignal {
                tag: entry_tag,
                leverage: None,
                liquidation_price: None,
            }),
            exit_long: exit_long.then(|| ExitSignal {
                reason: exit_reason(),
            }),
            exit_short: exit_short.then(|| ExitSignal {
                reason: exit_reason(),
            }),
            funding_rate,
            funding_mark_price,
            adjustment: None,
        });
        previous_close = Some(close);
    }
    Ok(candles)
}

fn validate_feature_names(pair: &InMemoryVectorPair) -> Result<(), VectorInputError> {
    let mut names = BTreeSet::new();
    for column in &pair.feature_columns {
        if column.is_empty() || !names.insert(column.as_str()) {
            return Err(VectorInputError::InvalidFeatureName {
                pair: pair.pair.clone(),
                column: column.clone(),
            });
        }
    }
    Ok(())
}

fn column<'a>(
    pair: &'a InMemoryVectorPair,
    name: &str,
) -> Result<&'a OwnedColumn, VectorInputError> {
    pair.frame
        .column(name)
        .ok_or_else(|| VectorInputError::MissingColumn {
            pair: pair.pair.clone(),
            column: name.to_owned(),
        })
}

fn required_timestamp(
    pair: &InMemoryVectorPair,
    name: &str,
    row: usize,
) -> Result<i64, VectorInputError> {
    let column = column(pair, name)?;
    if column.as_view().value_type() != ValueType::TimestampMs {
        return Err(type_error(pair, name, column, "Timestamp(Millisecond)"));
    }
    column
        .as_view()
        .timestamp_ms_at(row)
        .ok_or_else(|| null_error(pair, name, row))
}

#[allow(clippy::cast_precision_loss)]
// Feather converts Arrow Int64 through Python-compatible float semantics too.
fn required_number(
    pair: &InMemoryVectorPair,
    name: &str,
    row: usize,
) -> Result<f64, VectorInputError> {
    let column = column(pair, name)?;
    match column.as_view().value_type() {
        ValueType::F64 => column
            .as_view()
            .f64_at(row)
            .ok_or_else(|| null_error(pair, name, row)),
        ValueType::I64 => column
            .as_view()
            .i64_at(row)
            .map(|value| value as f64)
            .ok_or_else(|| null_error(pair, name, row)),
        _ => Err(type_error(pair, name, column, "numeric")),
    }
}

fn optional_number(
    pair: &InMemoryVectorPair,
    name: &str,
    row: usize,
) -> Result<Option<f64>, VectorInputError> {
    let column = column(pair, name)?;
    let value = match column.as_view().value_type() {
        ValueType::F64 => column.as_view().f64_at(row),
        ValueType::I64 => column.as_view().i64_at(row).map(integer_as_number),
        _ => return Err(type_error(pair, name, column, "numeric")),
    };
    Ok(value.filter(|value| !value.is_nan()))
}

fn enabled(pair: &InMemoryVectorPair, name: &str, row: usize) -> Result<bool, VectorInputError> {
    let column = column(pair, name)?;
    Ok(match column.as_view().value_type() {
        ValueType::F64 => column.as_view().f64_at(row) == Some(1.0),
        ValueType::I64 => column.as_view().i64_at(row) == Some(1),
        ValueType::Bool => column.as_view().bool_at(row) == Some(true),
        _ => return Err(type_error(pair, name, column, "numeric or Boolean")),
    })
}

fn optional_text(
    pair: &InMemoryVectorPair,
    name: &str,
    row: usize,
) -> Result<Option<String>, VectorInputError> {
    let Some(column) = pair.frame.column(name) else {
        return Ok(None);
    };
    if column.as_view().value_type() != ValueType::Text {
        return Err(type_error(pair, name, column, "UTF-8 string"));
    }
    Ok(column.as_view().text_at(row).and_then(|value| {
        (!value.is_empty() && value != EMPTY_TAG_TRANSPORT_SENTINEL).then(|| value.to_owned())
    }))
}

fn build_feature(
    pair: &InMemoryVectorPair,
    name: &str,
) -> Result<(String, PreparedFeature), VectorInputError> {
    let column = column(pair, name)?;
    let values = match column.as_view().value_type() {
        ValueType::F64 => PreparedFeature::Numbers(
            (0..column.len())
                .map(|row| column.as_view().f64_at(row).unwrap_or(f64::NAN))
                .collect(),
        ),
        ValueType::I64 => PreparedFeature::Numbers(
            (0..column.len())
                .map(|row| {
                    column
                        .as_view()
                        .i64_at(row)
                        .map_or(f64::NAN, integer_as_number)
                })
                .collect(),
        ),
        ValueType::Bool => PreparedFeature::Booleans(
            (0..column.len())
                .map(|row| {
                    column
                        .as_view()
                        .bool_at(row)
                        .ok_or_else(|| null_error(pair, name, row))
                })
                .collect::<Result<Vec<_>, _>>()?,
        ),
        _ => return Err(type_error(pair, name, column, "numeric or Boolean")),
    };
    Ok((name.to_owned(), values))
}

fn feature_bytes(column: &PreparedFeature) -> usize {
    match column {
        PreparedFeature::Numbers(values) => values.len().saturating_mul(size_of::<f64>()),
        PreparedFeature::Booleans(values) => values.len().saturating_mul(size_of::<bool>()),
    }
}

fn candle_string_bytes(candles: &[Candle]) -> usize {
    candles
        .iter()
        .map(|candle| {
            candle
                .enter_long
                .as_ref()
                .and_then(|signal| signal.tag.as_ref())
                .map_or(0, String::len)
                .saturating_add(
                    candle
                        .enter_short
                        .as_ref()
                        .and_then(|signal| signal.tag.as_ref())
                        .map_or(0, String::len),
                )
                .saturating_add(
                    candle
                        .exit_long
                        .as_ref()
                        .map_or(0, |signal| signal.reason.len()),
                )
                .saturating_add(
                    candle
                        .exit_short
                        .as_ref()
                        .map_or(0, |signal| signal.reason.len()),
                )
        })
        .fold(0_usize, usize::saturating_add)
}

fn type_error(
    pair: &InMemoryVectorPair,
    name: &str,
    column: &OwnedColumn,
    expected: &'static str,
) -> VectorInputError {
    VectorInputError::InMemoryColumnType {
        pair: pair.pair.clone(),
        column: name.to_owned(),
        actual: column.as_view().value_type().label(),
        expected,
    }
}

fn null_error(pair: &InMemoryVectorPair, name: &str, row: usize) -> VectorInputError {
    VectorInputError::NullValue {
        pair: pair.pair.clone(),
        column: name.to_owned(),
        row,
    }
}

fn duration_ns(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

#[allow(clippy::cast_precision_loss)]
// Matches the existing Feather/Python integer-to-number boundary exactly.
fn integer_as_number(value: i64) -> f64 {
    value as f64
}

#[cfg(test)]
mod tests {
    use std::fs::File;

    use arrow2::array::Array;
    use arrow2::chunk::Chunk;
    use arrow2::datatypes::{DataType, Field, Schema, TimeUnit};
    use arrow2::io::ipc::write::{FileWriter, WriteOptions};
    use nfi_sim_core::{simulate, simulate_with_observer, SimulationEvent};
    use nfi_vector_core::mutation::{
        materialize_execution_signals, MutationEngine, MutationProgram,
    };
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};

    use super::*;
    use crate::load_vector_manifest_profiled;

    const TAG_PROGRAM: &str =
        include_str!("../../../../benchmarks/reference/vector-shadow/tag-program.json");

    #[test]
    fn direct_transport_is_full_state_exact_to_sealed_feather_replay() {
        let temporary = tempfile::tempdir().expect("temporary fixture directory");
        let vector = temporary.path().join("pair.feather");
        let frame = fixture_frame();
        write_feather(&vector, &frame);
        let encoded = std::fs::read(&vector).expect("fixture vector bytes");
        let vector_sha = format!("{:x}", Sha256::digest(encoded));
        let config = config_document();
        let manifest = json!({
            "schema_version": crate::VECTOR_MANIFEST_SCHEMA_VERSION,
            "config": config,
            "pairs": [{
                "pair": "AAA/USDT",
                "execution_start_index": 1,
                "amount_step": null,
                "price_step": null,
                "price_steps": [],
                "minimum_stake": null,
                "minimum_amount": null,
                "minimum_cost": null,
                "vector": {
                    "path": "pair.feather",
                    "sha256": vector_sha,
                    "rows": frame.len(),
                    "format": "feather-ipc"
                },
                "feature_columns": ["score"],
                "can_short": false,
                "include_funding": false,
                "use_exit_signal": true,
                "include_previous_close": true
            }]
        });
        let manifest_path = temporary.path().join("manifest.json");
        std::fs::write(
            &manifest_path,
            serde_json::to_vec(&manifest).expect("manifest serializes"),
        )
        .expect("manifest writes");

        let (feather, feather_profile) =
            load_vector_manifest_profiled(&manifest_path).expect("sealed replay loads");
        let (direct, direct_profile) = assemble_in_memory_vectors_profiled(
            serde_json::from_value(config).expect("portfolio config"),
            vec![fixture_pair(frame)],
        )
        .expect("direct transport loads");
        let (feather_result, feather_events) = simulate_with_events(&feather);
        let (direct_result, direct_events) = simulate_with_events(&direct);

        assert_eq!(direct_result, feather_result);
        assert_eq!(direct_events, feather_events);
        assert!(matches!(
            direct.pairs[0].candles,
            nfi_sim_core::CandleSeries::Owned(_)
        ));
        assert!(matches!(
            feather.pairs[0].candles,
            nfi_sim_core::CandleSeries::FileBacked(_)
        ));
        println!(
            "M21_TRANSPORT_PROFILE={}",
            serde_json::to_string(&json!({
                "schema_version": "1.0.0",
                "scope": "four-row exact transport diagnostic; not a release speed claim",
                "direct": direct_profile,
                "feather": feather_profile,
                "trade_surface_exact": true,
                "full_state_exact": true
            }))
            .expect("profile serializes")
        );
    }

    #[test]
    fn pair_parallel_preparation_preserves_input_order_and_reports_memory() {
        let config = serde_json::from_value(config_document()).expect("portfolio config");
        let pairs = ["CCC/USDT", "AAA/USDT", "BBB/USDT"]
            .into_iter()
            .map(|name| {
                let mut pair = fixture_pair(fixture_frame());
                pair.pair = name.to_owned();
                pair
            })
            .collect();
        let (input, profile) =
            assemble_in_memory_vectors_profiled(config, pairs).expect("parallel pair prepare");

        assert_eq!(
            input
                .pairs
                .iter()
                .map(|pair| pair.pair.as_str())
                .collect::<Vec<_>>(),
            vec!["CCC/USDT", "AAA/USDT", "BBB/USDT"]
        );
        assert_eq!(profile.pair_count, 3);
        assert_eq!(profile.row_count, 12);
        assert_eq!(profile.feature_column_count, 3);
        assert!(profile.estimated_source_column_bytes > 0);
        assert!(profile.estimated_simulation_owned_bytes > 0);
        assert!(profile.pair_prepare_worker_limit > 0);
    }

    #[test]
    fn generic_rust_vector_dag_feeds_the_simulator_without_feather() {
        let config = serde_json::from_value(config_document()).expect("portfolio config");
        let program = MutationProgram::from_json(TAG_PROGRAM).expect("sealed Tag program");
        let tasks = vec!["AAA/USDT", "BBB/USDT"];
        let (input, profile) = execute_in_memory_pair_dag_profiled(config, tasks, |pair| {
            let source = tag_source_frame();
            let mutated = MutationEngine::new(&program)?.execute(source)?;
            let execution = materialize_execution_signals(&mutated, 1, 1)?;
            let mut columns = execution.frame.columns().clone();
            columns.extend(market_columns(8));
            columns.insert(
                "score".to_owned(),
                mutated.column("score").expect("source score").clone(),
            );
            Ok(InMemoryVectorPair {
                pair: pair.to_owned(),
                execution_start_index: 1,
                amount_step: None,
                price_step: None,
                price_steps: Vec::new(),
                minimum_stake: None,
                minimum_amount: None,
                minimum_cost: None,
                feature_columns: vec!["score".to_owned()],
                options: VectorPairOptions::default()
                    .with_exit_signal(true)
                    .with_previous_close(true),
                frame: MutationFrame::new(columns)?,
            })
        })
        .expect("pair DAG executes");

        assert_eq!(profile.pair_count, 2);
        assert!(profile.vector_execute_ns > 0);
        assert_eq!(input.pairs[0].pair, "AAA/USDT");
        assert_eq!(input.pairs[1].pair, "BBB/USDT");
        assert!(input.pairs.iter().all(|pair| {
            pair.candles
                .iter()
                .skip(pair.execution_start_index)
                .any(|candle| candle.enter_long.is_some())
        }));
        let mut events = Vec::new();
        simulate_with_observer(&input, |event| events.push(event.clone()))
            .expect("DAG-fed simulation");
        assert!(!events.is_empty());
    }

    #[test]
    #[ignore = "explicit release-mode transport diagnostic"]
    fn large_pair_transport_profile_is_reported_without_a_speed_assertion() {
        const ROWS_PER_PAIR: usize = 25_000;
        let temporary = tempfile::tempdir().expect("temporary diagnostic directory");
        let frame = diagnostic_frame(ROWS_PER_PAIR);
        let names = ["AAA/USDT", "BBB/USDT", "CCC/USDT", "DDD/USDT"];
        let config = config_document();
        let mut manifest_pairs = Vec::new();
        for (index, name) in names.iter().enumerate() {
            let file_name = format!("pair-{index}.feather");
            let path = temporary.path().join(&file_name);
            write_feather(&path, &frame);
            let vector_sha = format!(
                "{:x}",
                Sha256::digest(std::fs::read(&path).expect("diagnostic vector bytes"))
            );
            manifest_pairs.push(json!({
                "pair": name,
                "execution_start_index": 1,
                "amount_step": null,
                "price_step": null,
                "price_steps": [],
                "minimum_stake": null,
                "minimum_amount": null,
                "minimum_cost": null,
                "vector": {
                    "path": file_name,
                    "sha256": vector_sha,
                    "rows": ROWS_PER_PAIR,
                    "format": "feather-ipc"
                },
                "feature_columns": ["score"],
                "can_short": false,
                "include_funding": false,
                "use_exit_signal": true,
                "include_previous_close": true
            }));
        }
        let manifest_path = temporary.path().join("manifest.json");
        std::fs::write(
            &manifest_path,
            serde_json::to_vec(&json!({
                "schema_version": crate::VECTOR_MANIFEST_SCHEMA_VERSION,
                "config": config,
                "pairs": manifest_pairs
            }))
            .expect("diagnostic manifest serializes"),
        )
        .expect("diagnostic manifest writes");

        let (feather, feather_profile) =
            load_vector_manifest_profiled(&manifest_path).expect("diagnostic Feather replay");
        let tasks = names
            .into_iter()
            .map(|name| (name.to_owned(), frame.clone()))
            .collect();
        let (direct, direct_profile) = execute_in_memory_pair_dag_profiled(
            serde_json::from_value(config).expect("portfolio config"),
            tasks,
            |(name, frame)| {
                let mut pair = fixture_pair(frame);
                pair.pair = name;
                Ok(pair)
            },
        )
        .expect("diagnostic direct transport");
        assert_eq!(simulate(&direct).unwrap(), simulate(&feather).unwrap());

        let direct_ns = direct_profile
            .vector_execute_ns
            .saturating_add(direct_profile.pair_prepare_ns);
        let feather_ns = feather_profile
            .manifest_ns
            .saturating_add(feather_profile.vector_hash_ns)
            .saturating_add(feather_profile.feather_decode_ns);
        println!(
            "M21_LARGE_TRANSPORT_PROFILE={}",
            serde_json::to_string(&json!({
                "schema_version": "1.0.0",
                "scope": "four-pair 100000-row local transport diagnostic; not release-grade",
                "direct": direct_profile,
                "direct_transport_ns": direct_ns,
                "feather": feather_profile,
                "feather_transport_ns": feather_ns,
                "observed_transport_speedup": duration_ratio(feather_ns, direct_ns),
                "trade_surface_exact": true
            }))
            .expect("diagnostic profile serializes")
        );
    }

    #[test]
    fn invalid_type_duplicate_pair_and_numeric_truthiness_fail_closed() {
        let config = serde_json::from_value(config_document()).expect("portfolio config");
        let mut invalid = fixture_pair(fixture_frame());
        let mut invalid_columns = invalid.frame.columns().clone();
        invalid_columns.insert(
            "date".to_owned(),
            OwnedColumn::text(vec![Some("not a timestamp".to_owned()); 4]),
        );
        invalid.frame = MutationFrame::new(invalid_columns).expect("consistent invalid frame");
        assert!(matches!(
            assemble_in_memory_vectors(
                serde_json::from_value(config_document()).expect("portfolio config"),
                vec![invalid]
            ),
            Err(VectorInputError::InMemoryColumnType { .. })
        ));

        let duplicate = vec![fixture_pair(fixture_frame()), fixture_pair(fixture_frame())];
        assert!(matches!(
            assemble_in_memory_vectors(config, duplicate),
            Err(VectorInputError::InvalidPair(_))
        ));

        let original = fixture_frame();
        let mut columns = original.columns().clone();
        columns.insert(
            "nfi_exec_enter_long".to_owned(),
            OwnedColumn::i64(vec![Some(0), Some(2), Some(-1), Some(0)]),
        );
        let frame = MutationFrame::new(columns).expect("replacement signal");
        let direct = assemble_in_memory_vectors(
            serde_json::from_value(config_document()).expect("portfolio config"),
            vec![fixture_pair(frame)],
        )
        .expect("numeric signal frame");
        assert!(direct.pairs[0]
            .candles
            .iter()
            .all(|candle| candle.enter_long.is_none()));
    }

    fn fixture_pair(frame: MutationFrame) -> InMemoryVectorPair {
        InMemoryVectorPair {
            pair: "AAA/USDT".to_owned(),
            execution_start_index: 1,
            amount_step: None,
            price_step: None,
            price_steps: Vec::new(),
            minimum_stake: None,
            minimum_amount: None,
            minimum_cost: None,
            feature_columns: vec!["score".to_owned()],
            options: VectorPairOptions::default()
                .with_exit_signal(true)
                .with_previous_close(true),
            frame,
        }
    }

    fn fixture_frame() -> MutationFrame {
        MutationFrame::new(BTreeMap::from([
            (
                "date".to_owned(),
                OwnedColumn::timestamp_ms(vec![
                    Some(60_000),
                    Some(120_000),
                    Some(180_000),
                    Some(240_000),
                ]),
            ),
            (
                "open".to_owned(),
                OwnedColumn::f64(vec![Some(100.0), Some(101.0), Some(103.0), Some(104.0)]),
            ),
            (
                "high".to_owned(),
                OwnedColumn::f64(vec![Some(101.0), Some(102.0), Some(104.0), Some(105.0)]),
            ),
            (
                "low".to_owned(),
                OwnedColumn::f64(vec![Some(99.0), Some(100.0), Some(102.0), Some(103.0)]),
            ),
            (
                "close".to_owned(),
                OwnedColumn::f64(vec![Some(100.0), Some(101.0), Some(103.0), Some(104.0)]),
            ),
            (
                "volume".to_owned(),
                OwnedColumn::f64(vec![Some(10.0), Some(11.0), Some(12.0), Some(13.0)]),
            ),
            (
                "score".to_owned(),
                OwnedColumn::f64(vec![None, Some(-0.0), Some(f64::NAN), Some(3.0)]),
            ),
            (
                "nfi_exec_enter_long".to_owned(),
                OwnedColumn::i64(vec![Some(0), Some(1), Some(0), Some(0)]),
            ),
            (
                "nfi_exec_exit_long".to_owned(),
                OwnedColumn::i64(vec![Some(0), Some(0), Some(1), Some(0)]),
            ),
            (
                "nfi_exec_enter_tag".to_owned(),
                OwnedColumn::text(vec![None, Some("101  ".to_owned()), None, None]),
            ),
            (
                "nfi_exec_exit_tag".to_owned(),
                OwnedColumn::text(vec![None, None, Some("profit signal ".to_owned()), None]),
            ),
        ]))
        .expect("fixture frame")
    }

    fn tag_source_frame() -> MutationFrame {
        MutationFrame::new(BTreeMap::from([
            (
                "score".to_owned(),
                OwnedColumn::f64(vec![
                    Some(-2.0),
                    Some(-0.5),
                    Some(0.0),
                    Some(0.5),
                    Some(1.5),
                    Some(2.0),
                    Some(2.5),
                    Some(f64::NAN),
                ]),
            ),
            (
                "exit_mask".to_owned(),
                OwnedColumn::boolean(vec![
                    Some(false),
                    Some(true),
                    Some(false),
                    Some(true),
                    Some(false),
                    Some(true),
                    None,
                    Some(false),
                ]),
            ),
            (
                "enter_tag".to_owned(),
                OwnedColumn::text(vec![Some("stale-entry".to_owned()); 8]),
            ),
            (
                "exit_tag".to_owned(),
                OwnedColumn::text(vec![Some("stale-exit".to_owned()); 8]),
            ),
        ]))
        .expect("Tag source frame")
    }

    fn diagnostic_frame(rows: usize) -> MutationFrame {
        let indexes = 0..rows;
        MutationFrame::new(BTreeMap::from([
            (
                "date".to_owned(),
                OwnedColumn::timestamp_ms(
                    indexes
                        .clone()
                        .map(|row| {
                            Some(60_000_i64 * (i64::try_from(row).expect("diagnostic row") + 1))
                        })
                        .collect(),
                ),
            ),
            ("open".to_owned(), OwnedColumn::f64(vec![Some(100.0); rows])),
            ("high".to_owned(), OwnedColumn::f64(vec![Some(101.0); rows])),
            ("low".to_owned(), OwnedColumn::f64(vec![Some(99.0); rows])),
            (
                "close".to_owned(),
                OwnedColumn::f64(vec![Some(100.0); rows]),
            ),
            (
                "volume".to_owned(),
                OwnedColumn::f64(vec![Some(10.0); rows]),
            ),
            (
                "score".to_owned(),
                OwnedColumn::f64(
                    indexes
                        .map(|row| {
                            Some(f64::from(u32::try_from(row % 101).expect("diagnostic row")))
                        })
                        .collect(),
                ),
            ),
            (
                "nfi_exec_enter_long".to_owned(),
                OwnedColumn::i64(vec![Some(0); rows]),
            ),
            (
                "nfi_exec_exit_long".to_owned(),
                OwnedColumn::i64(vec![Some(0); rows]),
            ),
            (
                "nfi_exec_enter_tag".to_owned(),
                OwnedColumn::text(vec![None; rows]),
            ),
            (
                "nfi_exec_exit_tag".to_owned(),
                OwnedColumn::text(vec![None; rows]),
            ),
        ]))
        .expect("diagnostic frame")
    }

    fn market_columns(rows: usize) -> BTreeMap<String, OwnedColumn> {
        let timestamps = (0..rows)
            .map(|row| Some(60_000_i64 * (i64::try_from(row).expect("small row") + 1)))
            .collect();
        let prices = (0..rows)
            .map(|row| Some(100.0 + f64::from(u32::try_from(row).expect("small row"))))
            .collect::<Vec<_>>();
        BTreeMap::from([
            ("date".to_owned(), OwnedColumn::timestamp_ms(timestamps)),
            ("open".to_owned(), OwnedColumn::f64(prices.clone())),
            (
                "high".to_owned(),
                OwnedColumn::f64(
                    prices
                        .iter()
                        .map(|value| value.map(|item| item + 1.0))
                        .collect(),
                ),
            ),
            (
                "low".to_owned(),
                OwnedColumn::f64(
                    prices
                        .iter()
                        .map(|value| value.map(|item| item - 1.0))
                        .collect(),
                ),
            ),
            ("close".to_owned(), OwnedColumn::f64(prices)),
            (
                "volume".to_owned(),
                OwnedColumn::f64(vec![Some(10.0); rows]),
            ),
        ])
    }

    fn config_document() -> Value {
        json!({
            "starting_balance": 1_000.0,
            "max_open_trades": 1,
            "stake_amount": 100.0,
            "fee_rate": 0.0,
            "stoploss_ratio": -0.5,
            "amount_step": 0.00001,
            "price_step": 0.01
        })
    }

    fn write_feather(path: &std::path::Path, frame: &MutationFrame) {
        let fields = frame
            .columns()
            .iter()
            .map(|(name, column)| Field::new(name, data_type(column), true))
            .collect::<Vec<_>>();
        let arrays = frame
            .columns()
            .values()
            .map(|column| match column {
                OwnedColumn::F64(values) => Box::new(values.clone()) as Box<dyn Array>,
                OwnedColumn::I64(values) => Box::new(values.clone()) as Box<dyn Array>,
                OwnedColumn::Bool(values) => Box::new(values.clone()) as Box<dyn Array>,
                OwnedColumn::Text(values) => Box::new(values.clone()) as Box<dyn Array>,
                OwnedColumn::TimestampMs(values) => Box::new(values.clone()) as Box<dyn Array>,
            })
            .collect();
        let schema = Schema::from(fields);
        let mut writer = FileWriter::try_new(
            File::create(path).expect("create fixture vector"),
            schema,
            None,
            WriteOptions { compression: None },
        )
        .expect("create Feather writer");
        writer
            .write(&Chunk::new(arrays), None)
            .expect("write Feather batch");
        writer.finish().expect("finish Feather vector");
    }

    fn data_type(column: &OwnedColumn) -> DataType {
        match column.as_view().value_type() {
            ValueType::F64 => DataType::Float64,
            ValueType::I64 => DataType::Int64,
            ValueType::Bool => DataType::Boolean,
            ValueType::Text => DataType::Utf8,
            ValueType::TimestampMs => DataType::Timestamp(TimeUnit::Millisecond, None),
        }
    }

    fn simulate_with_events(
        input: &SimulationInput,
    ) -> (nfi_sim_core::SimulationResult, Vec<SimulationEvent>) {
        let mut events = Vec::new();
        let result = simulate_with_observer(input, |event| events.push(event.clone()))
            .expect("simulation succeeds");
        (result, events)
    }

    #[allow(clippy::cast_precision_loss)]
    fn duration_ratio(numerator: u64, denominator: u64) -> f64 {
        numerator as f64 / denominator as f64
    }
}
