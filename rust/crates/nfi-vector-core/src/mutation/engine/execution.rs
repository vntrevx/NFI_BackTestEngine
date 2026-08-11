use std::collections::BTreeMap;

use super::value::type_error;
use super::MutationFrame;
use crate::column::{OwnedColumn, ValueType};
use crate::VectorCoreError;

const SIGNAL_COLUMNS: [&str; 4] = ["enter_long", "enter_short", "exit_long", "exit_short"];
const TAG_COLUMNS: [&str; 2] = ["enter_tag", "exit_tag"];

/// Shifted execution columns plus their exact numeric-one indexes.
#[derive(Clone, Debug)]
pub struct ExecutionSignals {
    pub frame: MutationFrame,
    pub enabled_indexes: BTreeMap<String, Vec<usize>>,
}

/// Apply Freqtrade's decision-row to next-open shift and exact numeric-one gate.
///
/// # Errors
///
/// Returns a type or range error without emitting a partial execution frame.
pub fn materialize_execution_signals(
    frame: &MutationFrame,
    source_row_shift: usize,
    execution_start_index: usize,
) -> Result<ExecutionSignals, VectorCoreError> {
    if source_row_shift == 0 || execution_start_index > frame.rows {
        return Err(VectorCoreError::InvalidProgram(
            "execution shift or start index is invalid".to_owned(),
        ));
    }
    let mut columns = BTreeMap::new();
    let mut enabled_indexes = BTreeMap::new();
    for name in SIGNAL_COLUMNS {
        let shifted = match frame.column(name) {
            Some(column) => shift_signal(column, source_row_shift)?,
            None => OwnedColumn::i64(vec![Some(0); frame.rows]),
        };
        enabled_indexes.insert(
            name.to_owned(),
            (execution_start_index..frame.rows)
                .filter(|row| numeric_one(&shifted, *row))
                .collect(),
        );
        columns.insert(format!("nfi_exec_{name}"), shifted);
    }
    for name in TAG_COLUMNS {
        if let Some(column) = frame.column(name) {
            columns.insert(
                format!("nfi_exec_{name}"),
                shift_tag(column, source_row_shift)?,
            );
        }
    }
    Ok(ExecutionSignals {
        frame: MutationFrame::new(columns)?,
        enabled_indexes,
    })
}

fn shift_signal(column: &OwnedColumn, periods: usize) -> Result<OwnedColumn, VectorCoreError> {
    let rows = column.len();
    let view = column.as_view();
    Ok(match view.value_type() {
        ValueType::I64 => OwnedColumn::i64(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.i64_at(index))
                        .or(Some(0))
                })
                .collect(),
        ),
        ValueType::F64 => OwnedColumn::f64(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.f64_at(index))
                        .or(Some(0.0))
                })
                .collect(),
        ),
        ValueType::Bool => OwnedColumn::boolean(
            (0..rows)
                .map(|row| {
                    row.checked_sub(periods)
                        .and_then(|index| view.bool_at(index))
                        .or(Some(false))
                })
                .collect(),
        ),
        ValueType::Text | ValueType::TimestampMs => {
            return Err(type_error("execution signal", "numeric or Boolean"));
        }
    })
}

fn shift_tag(column: &OwnedColumn, periods: usize) -> Result<OwnedColumn, VectorCoreError> {
    let view = column.as_view();
    if view.value_type() != ValueType::Text {
        return Err(type_error("execution tag", "text"));
    }
    Ok(OwnedColumn::text(
        (0..column.len())
            .map(|row| {
                row.checked_sub(periods)
                    .and_then(|index| view.text_at(index).map(str::to_owned))
            })
            .collect(),
    ))
}

fn numeric_one(column: &OwnedColumn, row: usize) -> bool {
    match column.as_view().value_type() {
        ValueType::I64 => column.as_view().i64_at(row) == Some(1),
        ValueType::F64 => column.as_view().f64_at(row) == Some(1.0),
        ValueType::Bool => column.as_view().bool_at(row) == Some(true),
        _ => false,
    }
}
