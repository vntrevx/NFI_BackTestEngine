//! Conversion of projected Arrow batches into the fixed-width row contract.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Write};

use arrow2::array::{Array, BooleanArray};
use arrow2::chunk::Chunk;
use nfi_sim_core::{
    FileBackedFeatureKind, FILE_BACKED_FEATURE_BYTES, FILE_BACKED_ROW_HEADER_BYTES,
};

use crate::loader::VectorPair;
use crate::schema::FeatureLayout;
use crate::values::{
    column, enabled, optional_column, optional_number, optional_text, required_number,
    required_timestamp_ms,
};
use crate::VectorInputError;

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
// Keeping one row constructor makes the signal timing and shared tag/reason
// order directly reviewable against the two legacy Python adapter loops.
pub(crate) fn append_batch_to_spool(
    batch: &Chunk<Box<dyn Array>>,
    positions: &BTreeMap<String, usize>,
    pair: &VectorPair,
    row_offset: usize,
    previous_close: &mut Option<f64>,
    feature_layouts: &[FeatureLayout],
    spool: &mut BufWriter<File>,
    row_buffer: &mut [u8],
    tag_ids: &mut BTreeMap<String, u32>,
    tags: &mut Vec<String>,
) -> Result<(), VectorInputError> {
    let date = column(batch, positions, "date");
    let open_column = column(batch, positions, "open");
    let high_column = column(batch, positions, "high");
    let low_column = column(batch, positions, "low");
    let close_column = column(batch, positions, "close");
    let volume_column = column(batch, positions, "volume");
    let entry_tag_column = optional_column(batch, positions, "nfi_exec_enter_tag");
    let exit_tag_column = optional_column(batch, positions, "nfi_exec_exit_tag");
    let enter_long_column = column(batch, positions, "nfi_exec_enter_long");
    let exit_long_column = pair
        .use_exit_signal
        .enabled()
        .then(|| column(batch, positions, "nfi_exec_exit_long"));
    let enter_short_column = pair
        .can_short
        .enabled()
        .then(|| column(batch, positions, "nfi_exec_enter_short"));
    let exit_short_column = (pair.can_short.enabled() && pair.use_exit_signal.enabled())
        .then(|| column(batch, positions, "nfi_exec_exit_short"));
    let funding_rate_column = pair
        .include_funding
        .enabled()
        .then(|| column(batch, positions, "nfi_exec_funding_rate"));
    let funding_mark_price_column = pair
        .include_funding
        .enabled()
        .then(|| column(batch, positions, "nfi_exec_funding_mark_price"));
    let feature_arrays = feature_layouts
        .iter()
        .map(|layout| batch.arrays()[layout.source_index].as_ref())
        .collect::<Vec<_>>();

    for row in 0..batch.len() {
        // Every header and feature byte is assigned below. Clearing the whole
        // fixed-width row first would duplicate tens of gigabytes of writes.
        let absolute_row = row_offset + row;
        let timestamp_ms = required_timestamp_ms(date, row, &pair.pair, "date", absolute_row)?;
        let open = required_number(open_column, row, &pair.pair, "open", absolute_row)?;
        let high = required_number(high_column, row, &pair.pair, "high", absolute_row)?;
        let low = required_number(low_column, row, &pair.pair, "low", absolute_row)?;
        let close = required_number(close_column, row, &pair.pair, "close", absolute_row)?;
        let volume = required_number(volume_column, row, &pair.pair, "volume", absolute_row)?;
        let entry_tag = entry_tag_column
            .map(|array| optional_text(array, row, &pair.pair, "nfi_exec_enter_tag"))
            .transpose()?
            .flatten();
        let exit_tag = exit_tag_column
            .map(|array| optional_text(array, row, &pair.pair, "nfi_exec_exit_tag"))
            .transpose()?
            .flatten();
        let enter_long = enabled(
            enter_long_column,
            row,
            &pair.pair,
            "nfi_exec_enter_long",
            absolute_row,
        )?;
        let exit_long = exit_long_column
            .map(|array| enabled(array, row, &pair.pair, "nfi_exec_exit_long", absolute_row))
            .transpose()?
            .unwrap_or(false);
        let enter_short = enter_short_column
            .map(|array| enabled(array, row, &pair.pair, "nfi_exec_enter_short", absolute_row))
            .transpose()?
            .unwrap_or(false);
        let exit_short = exit_short_column
            .map(|array| enabled(array, row, &pair.pair, "nfi_exec_exit_short", absolute_row))
            .transpose()?
            .unwrap_or(false);
        let funding_rate = funding_rate_column
            .map(|array| {
                optional_number(
                    array,
                    row,
                    &pair.pair,
                    "nfi_exec_funding_rate",
                    absolute_row,
                )
            })
            .transpose()?
            .flatten();
        let funding_mark_price = funding_mark_price_column
            .map(|array| {
                optional_number(
                    array,
                    row,
                    &pair.pair,
                    "nfi_exec_funding_mark_price",
                    absolute_row,
                )
            })
            .transpose()?
            .flatten();
        let prior_close = pair
            .include_previous_close
            .enabled()
            .then_some(*previous_close)
            .flatten();
        let mut flags = 0_u8;
        set_flag(&mut flags, 0, prior_close.is_some());
        set_flag(&mut flags, 1, funding_rate.is_some());
        set_flag(&mut flags, 2, funding_mark_price.is_some());
        set_flag(&mut flags, 3, enter_long);
        set_flag(&mut flags, 4, enter_short);
        set_flag(&mut flags, 5, exit_long);
        set_flag(&mut flags, 6, exit_short);
        put_i64(row_buffer, 0, timestamp_ms);
        put_f64(row_buffer, 8, open);
        put_f64(row_buffer, 16, high);
        put_f64(row_buffer, 24, low);
        put_f64(row_buffer, 32, close);
        put_f64(row_buffer, 40, volume);
        put_f64(row_buffer, 48, prior_close.unwrap_or_default());
        put_f64(row_buffer, 56, funding_rate.unwrap_or_default());
        put_f64(row_buffer, 64, funding_mark_price.unwrap_or_default());
        row_buffer[72] = flags;
        put_u32(
            row_buffer,
            73,
            dictionary_id(entry_tag.as_deref(), tag_ids, tags, &pair.pair)?,
        );
        put_u32(
            row_buffer,
            77,
            dictionary_id(exit_tag.as_deref(), tag_ids, tags, &pair.pair)?,
        );
        *previous_close = Some(close);

        for (feature_index, (layout, array)) in feature_layouts
            .iter()
            .zip(feature_arrays.iter().copied())
            .enumerate()
        {
            let value = match layout.kind {
                FileBackedFeatureKind::Number => {
                    // Pandas materializes nullable numeric Arrow values as
                    // NaN. Preserve that exact callback-visible warm-up value.
                    if array.is_null(row) {
                        f64::NAN
                    } else {
                        required_number(array, row, &pair.pair, &layout.name, absolute_row)?
                    }
                }
                FileBackedFeatureKind::Boolean => {
                    if array.is_null(row) {
                        return Err(VectorInputError::NullValue {
                            pair: pair.pair.clone(),
                            column: layout.name.clone(),
                            row: absolute_row,
                        });
                    }
                    let boolean = array
                        .as_any()
                        .downcast_ref::<BooleanArray>()
                        .expect("feature type was checked against the Arrow schema");
                    f64::from(u8::from(boolean.value(row)))
                }
            };
            put_f64(
                row_buffer,
                FILE_BACKED_ROW_HEADER_BYTES + feature_index * FILE_BACKED_FEATURE_BYTES,
                value,
            );
        }
        spool
            .write_all(row_buffer)
            .map_err(|source| VectorInputError::FileBacking {
                pair: pair.pair.clone(),
                source,
            })?;
    }
    Ok(())
}

fn dictionary_id(
    value: Option<&str>,
    ids: &mut BTreeMap<String, u32>,
    values: &mut Vec<String>,
    pair: &str,
) -> Result<u32, VectorInputError> {
    let Some(value) = value else {
        return Ok(0);
    };
    if let Some(existing) = ids.get(value) {
        return Ok(*existing);
    }
    let identifier =
        u32::try_from(values.len() + 1).map_err(|_| VectorInputError::FileBacking {
            pair: pair.to_owned(),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "pair tag dictionary exceeds its row schema",
            ),
        })?;
    ids.insert(value.to_owned(), identifier);
    values.push(value.to_owned());
    Ok(identifier)
}

const fn set_flag(flags: &mut u8, bit: u8, enabled: bool) {
    if enabled {
        *flags |= 1 << bit;
    }
}

fn put_i64(row: &mut [u8], offset: usize, value: i64) {
    row[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn put_u32(row: &mut [u8], offset: usize, value: u32) {
    row[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put_f64(row: &mut [u8], offset: usize, value: f64) {
    row[offset..offset + 8].copy_from_slice(&value.to_bits().to_le_bytes());
}
