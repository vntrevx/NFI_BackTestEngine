use std::collections::{BTreeMap, BTreeSet};

use nfi_sim_core::PriceStepChange;
use nfi_vector_core::alignment::{FrameCatalog, NumericFrame, SourceLocation};
use nfi_vector_core::column::{OwnedColumn, ValueType};
use nfi_vector_core::engine::{FullFrameOutput, FullIndicatorEngine};
use nfi_vector_core::mutation::{
    materialize_execution_signals, MutationEngine, MutationFrame, MutationProgram,
};
use nfi_vector_core::program::IndicatorProgram;
use nfi_vector_core::VectorCoreError;

use crate::full_manifest::{FuturesFrameSet, PairContract, RunContract, TradingMode};
use crate::{
    prepare_execution_ohlcv, prepare_funding_events, InMemoryVectorPair, VectorInputError,
    VectorPairOptions,
};

const SIMULATOR_COLUMNS: [&str; 5] = ["open", "high", "low", "close", "volume"];
const SIGNAL_COLUMNS: [&str; 4] = ["enter_long", "enter_short", "exit_long", "exit_short"];
const TAG_COLUMNS: [&str; 2] = ["enter_tag", "exit_tag"];

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_pair(
    indicator_program: &IndicatorProgram,
    signal_program: &MutationProgram,
    tag_program: &MutationProgram,
    catalog: &FrameCatalog,
    run: &RunContract,
    retained_features: &[String],
    futures: &[FuturesFrameSet],
    pair: PairContract,
) -> Result<InMemoryVectorPair, VectorInputError> {
    validate_retained_features(retained_features)?;
    let source = SourceLocation::new("full-native-base-frame", "native/full_pipeline", 0, 0);
    let base = catalog.lookup_non_empty(&pair.identity, &source)?;
    let requested = indicator_execution_outputs(retained_features, signal_program, tag_program);
    let indicator = FullIndicatorEngine::new(indicator_program)?.execute(
        base,
        catalog,
        &pair.metadata,
        &requested,
    )?;
    validate_indicator_identity(base, &indicator)?;
    let mutation_source = MutationFrame::new(indicator.columns().clone())?;
    let signal = MutationEngine::new(signal_program)?
        .execute_with_metadata(mutation_source.clone(), &pair.metadata)?;
    let tag =
        MutationEngine::new(tag_program)?.execute_with_metadata(mutation_source, &pair.metadata)?;
    compare_signal_surfaces(&signal, &tag)?;
    let combined = adopt_tag_columns(&signal, &tag)?;
    let execution = materialize_execution_signals(&combined, run.source_row_shift, 0)?;
    let full_transport =
        assemble_transport_columns(&indicator, &combined, &execution.frame, retained_features)?;
    let timerange = format!("{}-{}", run.timerange_start_ms, run.timerange_stop_ms);
    let prepared = prepare_execution_ohlcv(base, &timerange, run.startup_candles)?;
    let mut sliced = slice_for_execution(base, &prepared.frame, &full_transport)?;
    if run.trading_mode == TradingMode::Futures || pair.options.include_funding {
        let events = prepare_funding_events(
            run.trading_mode,
            &pair.identity.pair,
            &prepared.frame.timestamps_ms,
            futures,
        )
        .map_err(|error| invalid(error.to_string()))?;
        for (name, column) in events.into_owned_columns() {
            if sliced.insert(name.clone(), column).is_some() {
                return Err(invalid(format!("funding column {name} collides")));
            }
        }
    }
    let frame = MutationFrame::new(sliced)?;
    if frame.is_empty() || prepared.execution_start_index >= frame.len() {
        return Err(invalid(format!(
            "prepared execution range is empty for {}",
            pair.identity.pair
        )));
    }
    Ok(InMemoryVectorPair {
        pair: pair.identity.pair,
        execution_start_index: prepared.execution_start_index,
        amount_step: pair.precision.amount_step,
        price_step: pair.precision.price_step,
        price_steps: pair
            .price_steps
            .into_iter()
            .map(|step| PriceStepChange {
                timestamp_ms: step.timestamp_ms,
                step: step.step,
            })
            .collect(),
        minimum_stake: pair.limits.minimum_stake,
        minimum_amount: pair.limits.minimum_amount,
        minimum_cost: pair.limits.minimum_cost,
        feature_columns: retained_features.to_vec(),
        options: VectorPairOptions::default()
            .with_can_short(pair.options.can_short)
            .with_funding(pair.options.include_funding)
            .with_exit_signal(pair.options.use_exit_signal)
            .with_previous_close(pair.options.include_previous_close),
        frame,
    })
}

fn indicator_execution_outputs(
    retained: &[String],
    signal: &MutationProgram,
    tag: &MutationProgram,
) -> Vec<String> {
    let mut outputs = Vec::new();
    let mut seen = BTreeSet::new();
    for name in SIMULATOR_COLUMNS
        .into_iter()
        .chain(
            retained
                .iter()
                .map(String::as_str)
                .filter(|name| !SIGNAL_COLUMNS.contains(name) && !TAG_COLUMNS.contains(name)),
        )
        .chain(signal.required_input_columns.iter().map(String::as_str))
        .chain(tag.required_input_columns.iter().map(String::as_str))
    {
        if seen.insert(name) {
            outputs.push(name.to_owned());
        }
    }
    outputs
}

fn validate_retained_features(features: &[String]) -> Result<(), VectorInputError> {
    let mut seen = BTreeSet::new();
    for feature in features {
        if feature.is_empty()
            || !seen.insert(feature.as_str())
            || feature == "date"
            || SIMULATOR_COLUMNS.contains(&feature.as_str())
            || TAG_COLUMNS.contains(&feature.as_str())
            || feature.starts_with("nfi_exec_")
        {
            return Err(invalid(format!(
                "retained feature {feature:?} is empty, duplicate, or reserved"
            )));
        }
    }
    Ok(())
}

fn validate_indicator_identity(
    base: &NumericFrame,
    indicator: &FullFrameOutput,
) -> Result<(), VectorInputError> {
    if indicator.identity() != &base.identity || indicator.timestamps_ms() != base.timestamps_ms {
        return Err(VectorCoreError::InvalidOutput(
            "complete indicator output changed the base identity or row index".to_owned(),
        )
        .into());
    }
    Ok(())
}

fn compare_signal_surfaces(
    signal: &MutationFrame,
    tag: &MutationFrame,
) -> Result<(), VectorInputError> {
    for name in SIGNAL_COLUMNS {
        let left = signal.column(name).ok_or_else(|| {
            VectorCoreError::InvalidOutput(format!("Signal program did not produce {name}"))
        })?;
        let right = tag.column(name).ok_or_else(|| {
            VectorCoreError::InvalidOutput(format!("Tag program did not produce {name}"))
        })?;
        if !columns_are_exact(left, right) {
            return Err(VectorCoreError::InvalidOutput(format!(
                "Signal and Tag programs disagree on {name}"
            ))
            .into());
        }
    }
    Ok(())
}

fn columns_are_exact(left: &OwnedColumn, right: &OwnedColumn) -> bool {
    let left_view = left.as_view();
    let right_view = right.as_view();
    if left_view.value_type() != right_view.value_type() || left.len() != right.len() {
        return false;
    }
    (0..left.len()).all(|row| match left_view.value_type() {
        ValueType::F64 => match (left_view.f64_at(row), right_view.f64_at(row)) {
            (None, None) => true,
            (Some(left), Some(right)) => left.to_bits() == right.to_bits(),
            _ => false,
        },
        ValueType::I64 => left_view.i64_at(row) == right_view.i64_at(row),
        ValueType::Bool => left_view.bool_at(row) == right_view.bool_at(row),
        ValueType::Text => left_view.text_at(row) == right_view.text_at(row),
        ValueType::TimestampMs => left_view.timestamp_ms_at(row) == right_view.timestamp_ms_at(row),
    })
}

fn adopt_tag_columns(
    signal: &MutationFrame,
    tag: &MutationFrame,
) -> Result<MutationFrame, VectorInputError> {
    let mut combined = signal.columns().clone();
    for name in TAG_COLUMNS {
        let synthesized;
        let column = if let Some(column) = tag.column(name) {
            column
        } else {
            // Freqtrade's entry/exit wrappers initialize tag columns to the
            // empty string even when the strategy never mutates them. The Tag
            // contract represents that exact case with final_mutation=null.
            synthesized = OwnedColumn::text(vec![Some(String::new()); tag.len()]);
            &synthesized
        };
        if column.as_view().value_type() != ValueType::Text {
            return Err(VectorCoreError::InvalidOutput(format!(
                "Tag program produced non-text {name}"
            ))
            .into());
        }
        combined.insert(name.to_owned(), column.clone());
    }
    MutationFrame::new(combined).map_err(Into::into)
}

fn assemble_transport_columns(
    indicator: &FullFrameOutput,
    decision: &MutationFrame,
    execution: &MutationFrame,
    retained: &[String],
) -> Result<MutationFrame, VectorInputError> {
    let mut columns = BTreeMap::from([(
        "date".to_owned(),
        OwnedColumn::timestamp_ms(
            indicator
                .timestamps_ms()
                .iter()
                .copied()
                .map(Some)
                .collect(),
        ),
    )]);
    for name in SIMULATOR_COLUMNS {
        let column = indicator
            .columns()
            .get(name)
            .ok_or_else(|| VectorCoreError::MissingOutput(format!("transport column {name}")))?;
        if columns.insert(name.to_owned(), column.clone()).is_some() {
            return Err(invalid(format!("duplicate transport column {name}")));
        }
    }
    for name in retained {
        let column = if SIGNAL_COLUMNS.contains(&name.as_str()) {
            decision.column(name)
        } else {
            indicator.columns().get(name)
        }
        .ok_or_else(|| VectorCoreError::MissingOutput(format!("transport column {name}")))?;
        if columns.insert(name.clone(), column.clone()).is_some() {
            return Err(invalid(format!("duplicate transport column {name}")));
        }
    }
    for (name, column) in execution.columns() {
        if columns.insert(name.clone(), column.clone()).is_some() {
            return Err(invalid(format!(
                "execution column {name} collides with a transport column"
            )));
        }
    }
    MutationFrame::new(columns).map_err(Into::into)
}

fn slice_for_execution(
    base: &NumericFrame,
    selected: &NumericFrame,
    frame: &MutationFrame,
) -> Result<BTreeMap<String, OwnedColumn>, VectorInputError> {
    let Some(first) = selected.timestamps_ms.first() else {
        return Err(invalid("prepared execution slice is empty"));
    };
    let start = base
        .timestamps_ms
        .binary_search(first)
        .map_err(|_| invalid("prepared execution slice does not start in the base frame"))?;
    let end = start
        .checked_add(selected.timestamps_ms.len())
        .ok_or_else(|| invalid("prepared execution slice is out of range"))?;
    if end > base.timestamps_ms.len()
        || base.timestamps_ms[start..end] != selected.timestamps_ms
        || frame.len() != base.timestamps_ms.len()
    {
        return Err(invalid(
            "prepared execution slice differs from the complete vector row index",
        ));
    }
    frame
        .columns()
        .iter()
        .map(|(name, column)| Ok((name.clone(), slice_column(column, start, end))))
        .collect()
}

fn slice_column(column: &OwnedColumn, start: usize, end: usize) -> OwnedColumn {
    let view = column.as_view();
    match view.value_type() {
        ValueType::F64 => OwnedColumn::f64((start..end).map(|row| view.f64_at(row)).collect()),
        ValueType::I64 => OwnedColumn::i64((start..end).map(|row| view.i64_at(row)).collect()),
        ValueType::Bool => {
            OwnedColumn::boolean((start..end).map(|row| view.bool_at(row)).collect())
        }
        ValueType::Text => OwnedColumn::text(
            (start..end)
                .map(|row| view.text_at(row).map(str::to_owned))
                .collect(),
        ),
        ValueType::TimestampMs => {
            OwnedColumn::timestamp_ms((start..end).map(|row| view.timestamp_ms_at(row)).collect())
        }
    }
}

fn invalid(message: impl Into<String>) -> VectorInputError {
    VectorCoreError::InvalidProgram(message.into()).into()
}
