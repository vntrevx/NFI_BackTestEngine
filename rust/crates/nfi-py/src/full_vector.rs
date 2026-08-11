//! In-memory Python bridge for the complete Rust vector stage.

use std::collections::{BTreeMap, BTreeSet};

use nfi_vector_core::alignment::{FrameCatalog, FrameIdentity, NumericFrame, Timeframe};
use nfi_vector_core::column::{OwnedColumn, ValueType};
use nfi_vector_core::engine::FullIndicatorEngine;
use nfi_vector_core::mutation::{
    materialize_execution_signals, ExecutionSignals, MutationEngine, MutationFrame, MutationProgram,
};
use nfi_vector_core::program::IndicatorProgram;
use nfi_vector_core::VectorCoreError;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

const SIGNAL_COLUMNS: [&str; 4] = ["enter_long", "enter_short", "exit_long", "exit_short"];
const TAG_COLUMNS: [&str; 2] = ["enter_tag", "exit_tag"];
const SOURCE_ROW_SHIFT: usize = 1;

type NumericColumns = BTreeMap<String, Vec<Option<f64>>>;
type InformativeFrameInput = (String, String, Vec<i64>, NumericColumns);

/// Complete typed output that can be transferred directly into an
/// `InMemoryVectorPair` without a Feather or strategy-Python round trip.
#[derive(Debug)]
struct FullVectorOutput {
    identity: FrameIdentity,
    timestamps_ms: Vec<i64>,
    execution_start_index: usize,
    columns: BTreeMap<String, OwnedColumn>,
    enabled_indexes: BTreeMap<String, Vec<usize>>,
}

#[allow(clippy::too_many_arguments)]
fn execute_stage(
    indicator_program: &IndicatorProgram,
    signal_program: &MutationProgram,
    tag_program: &MutationProgram,
    base: &NumericFrame,
    catalog: &FrameCatalog,
    metadata: &BTreeMap<String, String>,
    requested_indicator_columns: &[String],
    execution_start_index: usize,
) -> Result<FullVectorOutput, VectorCoreError> {
    validate_stage_contract(
        indicator_program,
        signal_program,
        tag_program,
        base,
        metadata,
        requested_indicator_columns,
        execution_start_index,
    )?;

    let execution_outputs =
        indicator_execution_outputs(requested_indicator_columns, signal_program, tag_program);
    let indicator = FullIndicatorEngine::new(indicator_program)?.execute(
        base,
        catalog,
        metadata,
        &execution_outputs,
    )?;
    if indicator.identity() != &base.identity || indicator.timestamps_ms() != base.timestamps_ms {
        return Err(VectorCoreError::InvalidOutput(
            "complete indicator output changed the base identity or row index".to_owned(),
        ));
    }

    let mutation_source = MutationFrame::new(indicator.columns().clone())?;
    let signal = MutationEngine::new(signal_program)?
        .execute_with_metadata(mutation_source.clone(), metadata)?;
    let tag = MutationEngine::new(tag_program)?.execute_with_metadata(mutation_source, metadata)?;
    compare_signal_surfaces(&signal, &tag)?;

    let mut combined = signal.columns().clone();
    for name in TAG_COLUMNS {
        let column = tag.column(name).ok_or_else(|| {
            VectorCoreError::InvalidOutput(format!("Tag program did not produce {name}"))
        })?;
        if column.as_view().value_type() != ValueType::Text {
            return Err(VectorCoreError::InvalidOutput(format!(
                "Tag program produced non-text {name}"
            )));
        }
        combined.insert(name.to_owned(), column.clone());
    }
    let execution = materialize_execution_signals(
        &MutationFrame::new(combined)?,
        SOURCE_ROW_SHIFT,
        execution_start_index,
    )?;
    assemble_output(
        &indicator,
        execution,
        requested_indicator_columns,
        execution_start_index,
    )
}

#[allow(clippy::too_many_arguments, clippy::needless_pass_by_value)]
#[pyfunction]
pub(super) fn execute_full_vector(
    py: Python<'_>,
    indicator_program: &str,
    signal_program: &str,
    tag_program: &str,
    base_pair: String,
    base_timeframe: String,
    base_timestamps_ms: Vec<i64>,
    base_columns: NumericColumns,
    informative_frames: Vec<InformativeFrameInput>,
    metadata: BTreeMap<String, String>,
    requested_indicator_columns: Vec<String>,
    execution_start_index: usize,
) -> PyResult<Py<PyAny>> {
    let indicator_program = IndicatorProgram::from_json(indicator_program)
        .map_err(|error| rejected("indicator program", &error))?;
    let signal_program = MutationProgram::from_json(signal_program)
        .map_err(|error| rejected("Signal program", &error))?;
    let tag_program =
        MutationProgram::from_json(tag_program).map_err(|error| rejected("Tag program", &error))?;
    let base = numeric_frame(base_pair, base_timeframe, base_timestamps_ms, base_columns)
        .map_err(|error| rejected("base frame", &error))?;
    let catalog = informative_catalog(informative_frames)
        .map_err(|error| rejected("informative catalog", &error))?;
    let output = execute_stage(
        &indicator_program,
        &signal_program,
        &tag_program,
        &base,
        &catalog,
        &metadata,
        &requested_indicator_columns,
        execution_start_index,
    )
    .map_err(|error| rejected("full vector stage", &error))?;
    output_to_python(py, output)
}

fn numeric_frame(
    pair: String,
    timeframe: String,
    timestamps_ms: Vec<i64>,
    columns: NumericColumns,
) -> Result<NumericFrame, VectorCoreError> {
    let identity = FrameIdentity::new(pair, Timeframe::parse(timeframe)?)?;
    let frame = NumericFrame {
        identity,
        timestamps_ms,
        columns,
    };
    frame.validate()?;
    Ok(frame)
}

fn informative_catalog(
    frames: Vec<InformativeFrameInput>,
) -> Result<FrameCatalog, VectorCoreError> {
    let frames = frames
        .into_iter()
        .map(|(pair, timeframe, timestamps, columns)| {
            let frame = numeric_frame(pair, timeframe, timestamps, columns)?;
            Ok((frame.identity.clone(), frame))
        })
        .collect::<Result<Vec<_>, VectorCoreError>>()?;
    FrameCatalog::new(frames)
}

#[allow(clippy::too_many_arguments)]
fn validate_stage_contract(
    indicator: &IndicatorProgram,
    signal: &MutationProgram,
    tag: &MutationProgram,
    base: &NumericFrame,
    metadata: &BTreeMap<String, String>,
    requested: &[String],
    execution_start_index: usize,
) -> Result<(), VectorCoreError> {
    if signal.is_tag_program() || !tag.is_tag_program() {
        return Err(invalid(
            "Signal and Tag programs are assigned to the wrong lanes",
        ));
    }
    if indicator.selected_class != signal.selected_class
        || signal.selected_class != tag.selected_class
        || indicator.source.sha256 != signal.source.sha256
        || signal.source.sha256 != tag.source.sha256
        || signal.compile_context != tag.compile_context
    {
        return Err(invalid(
            "Indicator, Signal, and Tag programs do not share one compiled strategy identity",
        ));
    }
    match metadata.get("pair") {
        Some(pair) if pair == &base.identity.pair => {}
        _ => {
            return Err(invalid(
                "metadata pair is absent or differs from the base frame pair",
            ));
        }
    }
    if base.timestamps_ms.is_empty() || execution_start_index >= base.timestamps_ms.len() {
        return Err(invalid(
            "execution_start_index must identify a row in a non-empty base frame",
        ));
    }
    let mut seen = BTreeSet::new();
    for name in requested {
        if name.is_empty()
            || !seen.insert(name.as_str())
            || name == "date"
            || name.starts_with("nfi_exec_")
            || SIGNAL_COLUMNS.contains(&name.as_str())
            || TAG_COLUMNS.contains(&name.as_str())
        {
            return Err(invalid(format!(
                "requested indicator column {name:?} is empty, duplicate, or reserved"
            )));
        }
    }
    Ok(())
}

fn indicator_execution_outputs(
    requested: &[String],
    signal: &MutationProgram,
    tag: &MutationProgram,
) -> Vec<String> {
    let mut outputs = Vec::new();
    let mut seen = BTreeSet::new();
    for name in requested
        .iter()
        .chain(&signal.required_input_columns)
        .chain(&tag.required_input_columns)
    {
        if seen.insert(name.as_str()) {
            outputs.push(name.clone());
        }
    }
    outputs
}

fn compare_signal_surfaces(
    signal: &MutationFrame,
    tag: &MutationFrame,
) -> Result<(), VectorCoreError> {
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
            )));
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

fn assemble_output(
    indicator: &nfi_vector_core::engine::FullFrameOutput,
    execution: ExecutionSignals,
    requested: &[String],
    execution_start_index: usize,
) -> Result<FullVectorOutput, VectorCoreError> {
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
    for name in requested {
        let column = indicator.columns().get(name).ok_or_else(|| {
            VectorCoreError::MissingOutput(format!("requested indicator column {name}"))
        })?;
        columns.insert(name.clone(), column.clone());
    }
    for (name, column) in execution.frame.columns() {
        if columns.insert(name.clone(), column.clone()).is_some() {
            return Err(VectorCoreError::InvalidOutput(format!(
                "execution column {name} collides with an indicator output"
            )));
        }
    }
    let frame = MutationFrame::new(columns)?;
    Ok(FullVectorOutput {
        identity: indicator.identity().clone(),
        timestamps_ms: indicator.timestamps_ms().to_vec(),
        execution_start_index,
        columns: frame.columns().clone(),
        enabled_indexes: execution.enabled_indexes,
    })
}

fn output_to_python(py: Python<'_>, output: FullVectorOutput) -> PyResult<Py<PyAny>> {
    let result = PyDict::new(py);
    result.set_item("pair", output.identity.pair)?;
    result.set_item("timeframe", output.identity.timeframe.as_str())?;
    result.set_item("timestamps_ms", output.timestamps_ms)?;
    result.set_item("execution_start_index", output.execution_start_index)?;
    let columns = PyDict::new(py);
    for (name, column) in output.columns {
        let encoded = PyDict::new(py);
        encoded.set_item("value_type", column.as_view().value_type().label())?;
        set_column_values(&encoded, &column)?;
        columns.set_item(name, encoded)?;
    }
    result.set_item("columns", columns)?;
    result.set_item("enabled_indexes", output.enabled_indexes)?;
    Ok(result.into_any().unbind())
}

fn set_column_values(output: &Bound<'_, PyDict>, column: &OwnedColumn) -> PyResult<()> {
    let view = column.as_view();
    match view.value_type() {
        ValueType::F64 => output.set_item(
            "values",
            (0..column.len())
                .map(|row| view.f64_at(row))
                .collect::<Vec<_>>(),
        ),
        ValueType::I64 => output.set_item(
            "values",
            (0..column.len())
                .map(|row| view.i64_at(row))
                .collect::<Vec<_>>(),
        ),
        ValueType::Bool => output.set_item(
            "values",
            (0..column.len())
                .map(|row| view.bool_at(row))
                .collect::<Vec<_>>(),
        ),
        ValueType::Text => output.set_item(
            "values",
            (0..column.len())
                .map(|row| view.text_at(row).map(str::to_owned))
                .collect::<Vec<_>>(),
        ),
        ValueType::TimestampMs => output.set_item(
            "values",
            (0..column.len())
                .map(|row| view.timestamp_ms_at(row))
                .collect::<Vec<_>>(),
        ),
    }
}

fn invalid(message: impl Into<String>) -> VectorCoreError {
    VectorCoreError::InvalidProgram(message.into())
}

fn rejected(context: &str, error: &VectorCoreError) -> PyErr {
    PyValueError::new_err(format!("{context} rejected: {error}"))
}
