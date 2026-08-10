use std::collections::BTreeMap;

use crate::VectorCoreError;

use super::model::{MergeSpec, MergedFrame, NumericFrame};
use super::support::{
    informative_events, ordered_by_timestamp, output_names, validate_frame, validate_identity,
    InformativeEvent, OutputBuilder,
};

/// Merges one complete informative frame onto a complete base frame.
///
/// Duplicate informative keys intentionally produce the same Cartesian rows as
/// pandas `merge`/`merge_ordered`; base order is never sorted or rewritten.
/// When `ffill` is enabled, `merge_ordered` instead sorts base timestamps.
///
/// # Errors
///
/// Returns a source-located error for an invalid spec, mismatched frame,
/// collision, or unrepresentable timestamp. Informative input is stably
/// ordered by its effective timestamp, matching pandas merge behavior.
pub fn merge(
    base: &NumericFrame,
    informative: &NumericFrame,
    spec: &MergeSpec,
) -> Result<MergedFrame, VectorCoreError> {
    spec.validate()?;
    validate_identity(base, &spec.base, spec)?;
    validate_identity(informative, &spec.informative, spec)?;
    validate_frame(base, spec)?;
    validate_frame(informative, spec)?;
    let ordered_base;
    let base = if spec.ffill {
        ordered_base = ordered_by_timestamp(base);
        &ordered_base
    } else {
        base
    };
    let names = output_names(base, informative, spec)?;
    let events = informative_events(informative, spec)?;
    let mut exact = BTreeMap::<i64, Vec<usize>>::new();
    for event in &events {
        exact
            .entry(event.key_ms)
            .or_default()
            .push(event.source_row);
    }

    let mut output = OutputBuilder::new(base, informative, &names);
    if spec.ffill {
        merge_ffill(base, &events, &exact, &mut output);
    } else {
        for base_row in 0..base.timestamps_ms.len() {
            let matches = exact.get(&base.timestamps_ms[base_row]);
            output.extend(base, base_row, matches.map(Vec::as_slice));
        }
    }
    Ok(output.finish(base.identity.clone()))
}

fn merge_ffill(
    base: &NumericFrame,
    events: &[InformativeEvent],
    exact: &BTreeMap<i64, Vec<usize>>,
    output: &mut OutputBuilder<'_>,
) {
    let first_match = base
        .timestamps_ms
        .iter()
        .find(|timestamp| exact.contains_key(timestamp))
        .copied();
    let leading_history = first_match.and_then(|first| {
        events
            .iter()
            .rev()
            .find(|event| event.key_ms < first)
            .map(|event| event.source_row)
    });
    let mut event_cursor = 0;
    let mut last = None;
    for base_row in 0..base.timestamps_ms.len() {
        let timestamp = base.timestamps_ms[base_row];
        while event_cursor < events.len() && events[event_cursor].key_ms <= timestamp {
            last = Some(events[event_cursor].source_row);
            event_cursor += 1;
        }
        if let Some(matches) = exact.get(&timestamp) {
            output.extend(base, base_row, Some(matches));
        } else if first_match.is_some_and(|first| timestamp < first) {
            if let Some(row) = leading_history {
                output.extend(base, base_row, Some(&[row]));
            } else {
                output.extend(base, base_row, None);
            }
        } else if first_match.is_some() {
            if let Some(row) = last {
                output.extend(base, base_row, Some(&[row]));
            } else {
                output.extend(base, base_row, None);
            }
        } else {
            output.extend(base, base_row, None);
        }
    }
}
