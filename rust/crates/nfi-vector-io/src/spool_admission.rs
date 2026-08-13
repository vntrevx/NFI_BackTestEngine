//! Filesystem admission for bounded Full Native pair spools.
//!
//! The bound comes only from public run semantics and the fixed row schema.
//! Raw manifest row counts are deliberately not used: two sparse source rows
//! can expand into every timeframe bucket through Freqtrade gap filling.

use std::ffi::OsString;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};

use nfi_sim_core::{FILE_BACKED_FEATURE_BYTES, FILE_BACKED_ROW_HEADER_BYTES};

use crate::{RunContract, VectorInputError};

pub(crate) const SPOOL_DIRECTORY_ENVIRONMENT: &str = "NFI_BTE_SPOOL_DIRECTORY";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct SpoolAdmission {
    pub(crate) required_upper_bound_bytes: u64,
    pub(crate) available_bytes: u64,
    pub(crate) target_source: &'static str,
    pub(crate) cleanup_mode: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SpoolTarget {
    directory: PathBuf,
    source: &'static str,
}

pub(crate) fn admit(
    run: &RunContract,
    pair_count: usize,
    retained_feature_count: usize,
) -> Result<SpoolAdmission, VectorInputError> {
    let target = resolve_target();
    admit_with(run, pair_count, retained_feature_count, target, |path| {
        fs2::available_space(path)
    })
}

pub(crate) fn create_file() -> io::Result<File> {
    tempfile::tempfile_in(resolve_target().directory)
}

fn admit_with(
    run: &RunContract,
    pair_count: usize,
    retained_feature_count: usize,
    target: SpoolTarget,
    capacity: impl FnOnce(&Path) -> io::Result<u64>,
) -> Result<SpoolAdmission, VectorInputError> {
    let required_upper_bound_bytes = required_upper_bound(run, pair_count, retained_feature_count)?;
    let available_bytes =
        capacity(&target.directory).map_err(|source| VectorInputError::SpoolCapacityProbe {
            target: target.directory,
            source,
        })?;
    if available_bytes < required_upper_bound_bytes {
        return Err(VectorInputError::SpoolCapacity {
            target_source: target.source,
            required_bytes: required_upper_bound_bytes,
            available_bytes,
        });
    }
    Ok(SpoolAdmission {
        required_upper_bound_bytes,
        available_bytes,
        target_source: target.source,
        cleanup_mode: cleanup_mode(),
    })
}

fn required_upper_bound(
    run: &RunContract,
    pair_count: usize,
    retained_feature_count: usize,
) -> Result<u64, VectorInputError> {
    if pair_count == 0 {
        return Err(bound_error("pair count must be positive"));
    }
    if run.timerange_start_ms < 0 || run.timerange_stop_ms < run.timerange_start_ms {
        return Err(bound_error("run timerange is invalid"));
    }
    let timeframe_ms = run.base_timeframe.resample_duration_ms();
    if timeframe_ms <= 0 {
        return Err(bound_error("base timeframe duration must be positive"));
    }
    let startup_candles = i64::try_from(run.startup_candles)
        .map_err(|_| bound_error("startup candle count is outside timestamp range"))?;
    let startup_ms = timeframe_ms
        .checked_mul(startup_candles)
        .ok_or_else(|| bound_error("startup window is outside timestamp range"))?;
    let load_start_ms = run
        .timerange_start_ms
        .checked_sub(startup_ms)
        .ok_or_else(|| bound_error("startup window is outside timestamp range"))?;
    let first_bucket = load_start_ms.div_euclid(timeframe_ms);
    let last_bucket = run.timerange_stop_ms.div_euclid(timeframe_ms);
    let rows_per_pair = last_bucket
        .checked_sub(first_bucket)
        .and_then(|value| value.checked_add(1))
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| bound_error("resampled row bound is outside addressable range"))?;
    let row_stride = retained_feature_count
        .checked_mul(FILE_BACKED_FEATURE_BYTES)
        .and_then(|bytes| FILE_BACKED_ROW_HEADER_BYTES.checked_add(bytes))
        .and_then(|bytes| u64::try_from(bytes).ok())
        .ok_or_else(|| bound_error("pair spool row is too wide"))?;
    u64::try_from(pair_count)
        .ok()
        .and_then(|pairs| pairs.checked_mul(rows_per_pair))
        .and_then(|rows| rows.checked_mul(row_stride))
        .ok_or_else(|| bound_error("aggregate spool upper bound exceeds u64"))
}

fn resolve_target() -> SpoolTarget {
    resolve_target_from(
        std::env::var_os(SPOOL_DIRECTORY_ENVIRONMENT),
        tempfile::env::temp_dir(),
    )
}

fn resolve_target_from(explicit: Option<OsString>, default: PathBuf) -> SpoolTarget {
    explicit.map_or(
        SpoolTarget {
            directory: default,
            source: "os-temp",
        },
        |directory| SpoolTarget {
            directory: PathBuf::from(directory),
            source: "environment",
        },
    )
}

#[cfg(unix)]
const fn cleanup_mode() -> &'static str {
    "unlink-on-open"
}

#[cfg(windows)]
const fn cleanup_mode() -> &'static str {
    "delete-on-close"
}

#[cfg(not(any(unix, windows)))]
const fn cleanup_mode() -> &'static str {
    "os-delete-on-close"
}

fn bound_error(message: impl Into<String>) -> VectorInputError {
    VectorInputError::SpoolBound(message.into())
}

#[cfg(test)]
mod tests {
    use nfi_vector_core::alignment::Timeframe;

    use super::*;
    use crate::TradingMode;

    fn run(start_ms: i64, stop_ms: i64, startup_candles: usize, timeframe: &str) -> RunContract {
        RunContract {
            trading_mode: TradingMode::Spot,
            timerange_start_ms: start_ms,
            timerange_stop_ms: stop_ms,
            startup_candles,
            base_timeframe: Timeframe::parse(timeframe).expect("timeframe"),
            source_row_shift: 1,
        }
    }

    #[test]
    fn bound_covers_gap_fill_instead_of_using_raw_row_counts() {
        // load_start=-10m through stop=+10m contains five 5m buckets even if
        // the manifest has only two endpoint rows.
        let contract = run(0, 600_000, 2, "5m");

        let required = required_upper_bound(&contract, 3, 4).expect("bound");

        assert_eq!(required, 3 * 5 * (81 + 4 * 8));
    }

    #[test]
    fn equality_is_admitted_and_one_byte_short_fails_closed() {
        let contract = run(1_704_067_200_000, 1_704_067_800_000, 0, "5m");
        let target = SpoolTarget {
            directory: PathBuf::from("injected-spool"),
            source: "environment",
        };
        let required = required_upper_bound(&contract, 2, 1).expect("bound");

        let admitted =
            admit_with(&contract, 2, 1, target.clone(), |_| Ok(required)).expect("equal capacity");
        let rejected =
            admit_with(&contract, 2, 1, target, |_| Ok(required - 1)).expect_err("one byte short");

        assert_eq!(admitted.required_upper_bound_bytes, required);
        assert_eq!(admitted.available_bytes, required);
        assert!(matches!(
            rejected,
            VectorInputError::SpoolCapacity {
                required_bytes,
                available_bytes,
                ..
            } if required_bytes == required && available_bytes == required - 1
        ));
    }

    #[test]
    fn overflow_and_capacity_probe_errors_are_structured() {
        let overflow =
            required_upper_bound(&run(0, i64::MAX, usize::MAX, "1d"), usize::MAX, usize::MAX)
                .expect_err("overflow");
        assert!(matches!(overflow, VectorInputError::SpoolBound(_)));

        let failure = admit_with(
            &run(0, 0, 0, "5m"),
            1,
            0,
            SpoolTarget {
                directory: PathBuf::from("unreadable-spool"),
                source: "environment",
            },
            |_| Err(io::Error::new(io::ErrorKind::PermissionDenied, "denied")),
        )
        .expect_err("probe error");
        assert!(matches!(
            failure,
            VectorInputError::SpoolCapacityProbe { source, .. }
                if source.kind() == io::ErrorKind::PermissionDenied
        ));
    }

    #[test]
    fn target_selection_distinguishes_default_and_explicit_directories() {
        let default = resolve_target_from(None, PathBuf::from("system-temp"));
        let explicit = resolve_target_from(
            Some(OsString::from("profile-spool")),
            PathBuf::from("system-temp"),
        );

        assert_eq!(default.directory, PathBuf::from("system-temp"));
        assert_eq!(default.source, "os-temp");
        assert_eq!(explicit.directory, PathBuf::from("profile-spool"));
        assert_eq!(explicit.source, "environment");
    }
}
