use std::collections::BTreeMap;

use crate::VectorCoreError;

/// Identifies the source location that declared an alignment operation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceLocation {
    pub node: String,
    pub path: String,
    pub line: u64,
    pub column: u64,
}

impl SourceLocation {
    #[must_use]
    pub fn new(node: impl Into<String>, path: impl Into<String>, line: u64, column: u64) -> Self {
        Self {
            node: node.into(),
            path: path.into(),
            line,
            column,
        }
    }
}

/// A canonical Freqtrade timeframe token.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Timeframe(String, i64);

impl Timeframe {
    /// Parses a positive `<count><unit>` Freqtrade timeframe.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error for an unsupported, zero, or
    /// unrepresentable timeframe.
    pub fn parse(value: impl Into<String>) -> Result<Self, VectorCoreError> {
        let value = value.into();
        let Some(unit) = value.chars().last() else {
            return Err(VectorCoreError::InvalidProgram(
                "empty timeframe".to_owned(),
            ));
        };
        if !matches!(unit, 's' | 'm' | 'h' | 'd' | 'w' | 'M' | 'y') {
            return Err(VectorCoreError::InvalidProgram(format!(
                "unsupported timeframe {value:?}"
            )));
        }
        let count = value[..value.len() - unit.len_utf8()]
            .parse::<u64>()
            .ok()
            .filter(|count| *count > 0)
            .ok_or_else(|| {
                VectorCoreError::InvalidProgram(format!("invalid timeframe {value:?}"))
            })?;
        let multiplier = match unit {
            's' => 1_000_u64,
            'm' => 60_000_u64,
            'h' => 3_600_000,
            'd' => 86_400_000,
            'w' => 7 * 86_400_000,
            // Freqtrade's timeframe-to-minutes contract uses 30 days for
            // ordering. `1M` gets its calendar MonthBegin merge rule below.
            'M' => 30 * 86_400_000,
            'y' => 365 * 86_400_000,
            _ => unreachable!("validated timeframe unit"),
        };
        let duration_ms = count.checked_mul(multiplier).ok_or_else(|| {
            VectorCoreError::InvalidProgram(format!("timeframe is too large: {value}"))
        })?;
        let duration_ms = i64::try_from(duration_ms).map_err(|_| {
            VectorCoreError::InvalidProgram(format!("timeframe is too large: {value}"))
        })?;
        Ok(Self(value, duration_ms))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Duration used for resampling and Arrow timestamp arithmetic.
    #[must_use]
    pub fn resample_duration_ms(&self) -> i64 {
        self.1
    }

    /// Freqtrade merge duration: `ccxt.parse_timeframe(timeframe) // 60`.
    #[must_use]
    pub fn merge_minutes(&self) -> i64 {
        self.resample_duration_ms() / 60_000
    }
}

/// The concrete pair and timeframe that own one source frame.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct FrameIdentity {
    pub pair: String,
    pub timeframe: Timeframe,
}

impl FrameIdentity {
    /// Creates a non-empty pair/timeframe identity.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program error when `pair` is empty.
    pub fn new(pair: impl Into<String>, timeframe: Timeframe) -> Result<Self, VectorCoreError> {
        let pair = pair.into();
        if pair.is_empty() {
            return Err(VectorCoreError::InvalidProgram(
                "frame pair is empty".to_owned(),
            ));
        }
        Ok(Self { pair, timeframe })
    }
}

/// One timestamp-indexed numeric frame. A null is distinct from a present NaN.
#[derive(Clone, Debug, PartialEq)]
pub struct NumericFrame {
    pub identity: FrameIdentity,
    pub timestamps_ms: Vec<i64>,
    pub columns: BTreeMap<String, Vec<Option<f64>>>,
}

impl NumericFrame {
    /// Validates column names and row shape.
    ///
    /// # Errors
    ///
    /// Returns an invalid-program or column-length error for an invalid frame.
    pub fn validate(&self) -> Result<(), VectorCoreError> {
        for (name, values) in &self.columns {
            if name.is_empty() {
                return Err(VectorCoreError::InvalidProgram(
                    "frame column is empty".to_owned(),
                ));
            }
            if values.len() != self.timestamps_ms.len() {
                return Err(VectorCoreError::ColumnLength {
                    column: name.clone(),
                    actual: values.len(),
                    expected: self.timestamps_ms.len(),
                });
            }
        }
        Ok(())
    }
}

/// Exact options for one `merge_informative_pair` operation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MergeSpec {
    pub base: FrameIdentity,
    pub informative: FrameIdentity,
    pub ffill: bool,
    pub append_timeframe: bool,
    pub suffix: Option<String>,
    /// Informative source timestamp name. The base join key is always `date`.
    pub date_column: String,
    pub source: SourceLocation,
}

impl MergeSpec {
    /// Validates pair identities and the mutually-exclusive suffix modes.
    ///
    /// # Errors
    ///
    /// Returns a source-located error for incompatible merge options.
    pub fn validate(&self) -> Result<(), VectorCoreError> {
        if self.append_timeframe
            && self
                .suffix
                .as_ref()
                .is_some_and(|suffix| !suffix.is_empty())
        {
            return Err(self.error("suffix cannot be combined with append_timeframe"));
        }
        if self.date_column.is_empty() {
            return Err(self.error("informative date_column cannot be empty"));
        }
        if self.informative.timeframe.merge_minutes() < self.base.timeframe.merge_minutes() {
            return Err(self.error("informative timeframe cannot be faster than base timeframe"));
        }
        Ok(())
    }

    pub(super) fn error(&self, message: impl Into<String>) -> VectorCoreError {
        VectorCoreError::Execution {
            node: self.source.node.clone(),
            message: format!(
                "{}:{}:{}: {}",
                self.source.path,
                self.source.line,
                self.source.column,
                message.into()
            ),
        }
    }

    pub(super) fn output_name(&self, name: &str) -> Result<String, VectorCoreError> {
        let suffix = if self.append_timeframe {
            Some(self.informative.timeframe.as_str())
        } else {
            self.suffix.as_deref().filter(|suffix| !suffix.is_empty())
        };
        suffix.map_or_else(
            || {
                Err(self.error(format!(
                    "informative column {name:?} would collide without a suffix"
                )))
            },
            |suffix| Ok(format!("{name}_{suffix}")),
        )
    }

    pub(super) fn effective_timestamp(&self, open_ms: i64) -> Result<i64, VectorCoreError> {
        let base_minutes = self.base.timeframe.merge_minutes();
        let informative_minutes = self.informative.timeframe.merge_minutes();
        if base_minutes == informative_minutes {
            return Ok(open_ms);
        }
        let base_offset_ms = base_minutes
            .checked_mul(60_000)
            .ok_or_else(|| self.error("base merge duration is out of range"))?;
        if self.informative.timeframe.0 == "1M" {
            return month_begin_next(open_ms)
                .and_then(|next| next.checked_sub(base_offset_ms))
                .ok_or_else(|| self.error("monthly informative merge timestamp is out of range"));
        }
        let informative_offset_ms = informative_minutes
            .checked_mul(60_000)
            .ok_or_else(|| self.error("informative merge duration is out of range"))?;
        open_ms
            .checked_add(informative_offset_ms)
            .and_then(|value| value.checked_sub(base_offset_ms))
            .ok_or_else(|| self.error("informative merge timestamp is out of range"))
    }
}

/// A merged base frame, including the informative source dates returned by Freqtrade.
///
/// Missing informative numeric join values are canonical `f64::NAN`; an input
/// nullable numeric value remains `None` after an exact match or forward fill.
#[derive(Clone, Debug, PartialEq)]
pub struct MergedFrame {
    pub identity: FrameIdentity,
    pub timestamps_ms: Vec<i64>,
    pub columns: BTreeMap<String, Vec<Option<f64>>>,
    pub informative_dates_ms: BTreeMap<String, Vec<Option<i64>>>,
}

fn month_begin_next(timestamp_ms: i64) -> Option<i64> {
    const DAY_MS: i64 = 86_400_000;
    let days = timestamp_ms.div_euclid(DAY_MS);
    let (year, month, day) = civil_from_days(days);
    let (next_year, next_month) = if day == 1 && timestamp_ms.rem_euclid(DAY_MS) == 0 {
        if month == 12 {
            (year + 1, 1)
        } else {
            (year, month + 1)
        }
    } else if month == 12 {
        (year + 1, 1)
    } else {
        (year, month + 1)
    };
    days_from_civil(next_year, next_month, 1)?.checked_mul(DAY_MS)
}

fn civil_from_days(days: i64) -> (i64, u8, u8) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_in_era = z - era * 146_097;
    let year_in_era =
        (day_in_era - day_in_era / 1_460 + day_in_era / 36_524 - day_in_era / 146_096) / 365;
    let year = year_in_era + era * 400;
    let ordinal = day_in_era - (365 * year_in_era + year_in_era / 4 - year_in_era / 100);
    let month_prime = (5 * ordinal + 2) / 153;
    let day = ordinal - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    (
        year + i64::from(month <= 2),
        u8::try_from(month).expect("civil month is in 1..=12"),
        u8::try_from(day).expect("civil day is in 1..=31"),
    )
}

pub(super) fn days_from_civil(year: i64, month: u8, day_of_month: u8) -> Option<i64> {
    let year = year - i64::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let month = i64::from(month);
    let day_of_month = i64::from(day_of_month);
    let ordinal = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day_of_month - 1;
    let day_in_era = yoe * 365 + yoe / 4 - yoe / 100 + ordinal;
    era.checked_mul(146_097)?
        .checked_add(day_in_era)?
        .checked_sub(719_468)
}
