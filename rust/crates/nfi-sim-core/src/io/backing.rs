//! Buffered reader for the verified fixed-width pair row store.

use std::cell::RefCell;
use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::rc::Rc;
use std::sync::OnceLock;

use super::{
    CALLBACK_FEATURE_LOOKBACK_ROWS, FILE_BACKED_FEATURE_BYTES, FILE_BACKED_READ_BUFFER_BYTES,
    FILE_BACKED_ROW_HEADER_BYTES,
};
use crate::domain::{Candle, EntrySignal, ExitSignal};

struct FileBackedState {
    file: File,
    window_start: usize,
    window_row_count: usize,
    window: Vec<u8>,
}

/// Shared safe file reader for one normalized pair.
///
/// The file is created privately by the verified Arrow boundary and remains
/// open for this object's lifetime. `RefCell` is deliberate: the simulator's
/// chronological event loop is single-threaded, while pair preparation is
/// parallelized before this boundary.
pub struct FileBackedRows {
    state: RefCell<FileBackedState>,
    row_count: usize,
    row_stride: usize,
    feature_count: usize,
    tags: Vec<String>,
    entry_indices: OnceLock<Vec<usize>>,
}

impl fmt::Debug for FileBackedRows {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("FileBackedRows")
            .field("row_count", &self.row_count)
            .field("row_stride", &self.row_stride)
            .field("feature_count", &self.feature_count)
            .field("tag_count", &self.tags.len())
            .field("entry_index_count", &self.entry_indices.get().map(Vec::len))
            .finish_non_exhaustive()
    }
}

impl FileBackedRows {
    /// Open a verified fixed-width pair spool.
    ///
    /// # Errors
    ///
    /// Returns an I/O error if the spool length does not match the declared
    /// row and feature counts or if its length cannot be represented safely.
    pub fn new(
        mut file: File,
        row_count: usize,
        feature_count: usize,
        tags: Vec<String>,
    ) -> Result<Rc<Self>, std::io::Error> {
        let feature_bytes = feature_count
            .checked_mul(FILE_BACKED_FEATURE_BYTES)
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "feature row is too wide")
            })?;
        let row_stride = FILE_BACKED_ROW_HEADER_BYTES
            .checked_add(feature_bytes)
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "pair row is too wide")
            })?;
        let expected_bytes = row_count.checked_mul(row_stride).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "pair spool is too large")
        })?;
        let actual_bytes = file.seek(SeekFrom::End(0))?;
        if actual_bytes != u64::try_from(expected_bytes).unwrap_or(u64::MAX) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "pair spool length mismatch: expected {expected_bytes}, got {actual_bytes}"
                ),
            ));
        }
        file.seek(SeekFrom::Start(0))?;
        let rows_per_window = (FILE_BACKED_READ_BUFFER_BYTES / row_stride).max(1);
        let window_bytes = rows_per_window.checked_mul(row_stride).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "read window is too large")
        })?;
        Ok(Rc::new(Self {
            state: RefCell::new(FileBackedState {
                file,
                window_start: 0,
                window_row_count: 0,
                window: vec![0; window_bytes],
            }),
            row_count,
            row_stride,
            feature_count,
            tags,
            entry_indices: OnceLock::new(),
        }))
    }

    #[must_use]
    pub const fn len(&self) -> usize {
        self.row_count
    }

    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.row_count == 0
    }

    fn with_row<T>(&self, index: usize, read: impl FnOnce(&[u8]) -> T) -> Option<T> {
        if index >= self.row_count {
            return None;
        }
        let mut state = self.state.borrow_mut();
        let window_end = state
            .window_start
            .checked_add(state.window_row_count)
            .expect("validated read window remains representable");
        if index < state.window_start || index >= window_end {
            let rows_per_window = state.window.len() / self.row_stride;
            // Include the callback-visible lookback in the same window as the
            // current row. Without this overlap, a candle near a window
            // boundary alternates between two disk reads while constructing
            // `last_candle` and `previous_candle_1..5`.
            let lookback_rows =
                CALLBACK_FEATURE_LOOKBACK_ROWS.min(rows_per_window.saturating_sub(1));
            let window_start = index.saturating_sub(lookback_rows);
            let window_row_count = rows_per_window.min(self.row_count - window_start);
            let file_offset = window_start
                .checked_mul(self.row_stride)
                .and_then(|value| u64::try_from(value).ok())
                .expect("validated pair spool offset remains representable");
            let window_bytes = window_row_count
                .checked_mul(self.row_stride)
                .expect("validated pair read window remains representable");
            {
                let FileBackedState { file, window, .. } = &mut *state;
                file.seek(SeekFrom::Start(file_offset))
                    .and_then(|_| file.read_exact(&mut window[..window_bytes]))
                    .expect("private verified pair spool remains readable");
            }
            state.window_start = window_start;
            state.window_row_count = window_row_count;
        }
        let row_offset = (index - state.window_start)
            .checked_mul(self.row_stride)
            .expect("validated pair row offset remains representable");
        Some(read(
            &state.window[row_offset..row_offset + self.row_stride],
        ))
    }

    pub(super) fn candle(&self, index: usize) -> Option<Candle> {
        self.with_row(index, |row| {
            let flags = row[72];
            let entry_tag = self.tag(read_u32(row, 73));
            let exit_tag = self.tag(read_u32(row, 77));
            let exit_reason = || exit_tag.clone().unwrap_or_else(|| "exit_signal".to_owned());
            Candle {
                timestamp_ms: read_i64(row, 0),
                open: read_f64(row, 8),
                high: read_f64(row, 16),
                low: read_f64(row, 24),
                close: read_f64(row, 32),
                volume: read_f64(row, 40),
                previous_close: flag(flags, 0).then(|| read_f64(row, 48)),
                enter_long: flag(flags, 3).then(|| EntrySignal {
                    tag: entry_tag.clone(),
                    leverage: None,
                    liquidation_price: None,
                }),
                enter_short: flag(flags, 4).then_some(EntrySignal {
                    tag: entry_tag,
                    leverage: None,
                    liquidation_price: None,
                }),
                exit_long: flag(flags, 5).then(|| ExitSignal {
                    reason: exit_reason(),
                }),
                exit_short: flag(flags, 6).then(|| ExitSignal {
                    reason: exit_reason(),
                }),
                funding_rate: flag(flags, 1).then(|| read_f64(row, 56)),
                funding_mark_price: flag(flags, 2).then(|| read_f64(row, 64)),
                adjustment: None,
            }
        })
    }

    pub(crate) fn timestamp_ms(&self, index: usize) -> Option<i64> {
        self.with_row(index, |row| read_i64(row, 0))
    }

    pub(super) fn has_entry_signal(&self, index: usize) -> Option<bool> {
        self.with_row(index, |row| {
            let flags = row[72];
            flag(flags, 3) || flag(flags, 4)
        })
    }

    pub(crate) fn next_entry_index(&self, start: usize) -> Option<usize> {
        if let Some(indices) = self.entry_indices.get() {
            let offset = indices.partition_point(|index| *index < start);
            return indices.get(offset).copied();
        }
        (start..self.row_count).find(|index| self.has_entry_signal(*index) == Some(true))
    }

    pub(crate) fn install_entry_indices(&self, indices: Vec<usize>) {
        // The row spool is immutable, so a second successful validation would
        // produce the same index. Keep the first completed index and avoid
        // replacing storage that may already be used by the scheduler.
        let _ = self.entry_indices.set(indices);
    }

    pub(crate) fn feature_number(&self, row_index: usize, feature_index: usize) -> Option<f64> {
        if feature_index >= self.feature_count {
            return None;
        }
        self.with_row(row_index, |row| {
            read_f64(
                row,
                FILE_BACKED_ROW_HEADER_BYTES + feature_index * FILE_BACKED_FEATURE_BYTES,
            )
        })
    }

    pub(super) fn feature_boolean(&self, row_index: usize, feature_index: usize) -> Option<bool> {
        self.feature_number(row_index, feature_index)
            .map(|value| value != 0.0)
    }

    fn tag(&self, encoded: u32) -> Option<String> {
        encoded
            .checked_sub(1)
            .and_then(|index| usize::try_from(index).ok())
            .and_then(|index| self.tags.get(index))
            .cloned()
    }

    #[cfg(test)]
    pub(crate) fn buffered_window_start(&self) -> usize {
        self.state.borrow().window_start
    }

    #[cfg(test)]
    pub(crate) fn installed_entry_indices(&self) -> Option<&[usize]> {
        self.entry_indices.get().map(Vec::as_slice)
    }
}

const fn flag(flags: u8, bit: u8) -> bool {
    flags & (1 << bit) != 0
}

fn read_i64(row: &[u8], offset: usize) -> i64 {
    i64::from_le_bytes(
        row[offset..offset + 8]
            .try_into()
            .expect("validated row scalar width"),
    )
}

fn read_u32(row: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(
        row[offset..offset + 4]
            .try_into()
            .expect("validated row scalar width"),
    )
}

fn read_f64(row: &[u8], offset: usize) -> f64 {
    f64::from_bits(u64::from_le_bytes(
        row[offset..offset + 8]
            .try_into()
            .expect("validated row scalar width"),
    ))
}
