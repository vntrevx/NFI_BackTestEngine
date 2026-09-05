//! Serialized protection configuration and validation.

use serde::{Deserialize, Serialize};

use crate::domain::ClosedTrade;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProtectionProgram {
    pub timeframe_ms: i64,
    pub handlers: Vec<ProtectionHandler>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "method")]
pub enum ProtectionHandler {
    CooldownPeriod {
        timing: ProtectionTiming,
    },
    StoplossGuard {
        timing: ProtectionTiming,
        trade_limit: usize,
        only_per_pair: bool,
        only_per_side: bool,
        required_profit: f64,
    },
    MaxDrawdown {
        timing: ProtectionTiming,
        trade_limit: usize,
        maximum_allowed_drawdown: f64,
        #[serde(default)]
        maximum_allowed_drawdown_repr: Option<String>,
        calculation_mode: DrawdownMode,
    },
    LowProfitPairs {
        timing: ProtectionTiming,
        trade_limit: usize,
        only_per_side: bool,
        required_profit: f64,
        #[serde(default)]
        required_profit_repr: Option<String>,
    },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProtectionTiming {
    pub lookback_ms: i64,
    pub lookback_text: String,
    pub duration_ms: Option<i64>,
    pub unlock_at_minute_utc: Option<u16>,
    pub lock_text: String,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DrawdownMode {
    Ratios,
    Equity,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PairLockState {
    pub pair: String,
    pub lock_timestamp_ms: i64,
    pub lock_end_timestamp_ms: i64,
    pub reason: String,
    pub side: String,
    pub active: bool,
}

impl ProtectionProgram {
    pub(crate) fn is_valid(&self) -> bool {
        self.timeframe_ms > 0 && self.handlers.iter().all(ProtectionHandler::is_valid)
    }
}

impl ProtectionHandler {
    fn is_valid(&self) -> bool {
        let (timing, trade_limit, numeric) = match self {
            Self::CooldownPeriod { timing } => (timing, None, None),
            Self::StoplossGuard {
                timing,
                trade_limit,
                required_profit,
                ..
            }
            | Self::LowProfitPairs {
                timing,
                trade_limit,
                required_profit,
                ..
            } => (timing, Some(*trade_limit), Some(*required_profit)),
            Self::MaxDrawdown {
                timing,
                trade_limit,
                maximum_allowed_drawdown,
                ..
            } => (timing, Some(*trade_limit), Some(*maximum_allowed_drawdown)),
        };
        timing.is_valid()
            && trade_limit.is_none_or(|value| value > 0)
            && numeric.is_none_or(f64::is_finite)
            && match self {
                Self::LowProfitPairs {
                    required_profit,
                    required_profit_repr,
                    ..
                } => numeric_repr_is_valid(*required_profit, required_profit_repr.as_deref()),
                Self::MaxDrawdown {
                    maximum_allowed_drawdown,
                    maximum_allowed_drawdown_repr,
                    ..
                } => numeric_repr_is_valid(
                    *maximum_allowed_drawdown,
                    maximum_allowed_drawdown_repr.as_deref(),
                ),
                _ => true,
            }
            && match self {
                Self::MaxDrawdown {
                    maximum_allowed_drawdown,
                    ..
                } => *maximum_allowed_drawdown >= 0.0,
                _ => true,
            }
    }
}

fn numeric_repr_is_valid(value: f64, numeric_repr: Option<&str>) -> bool {
    numeric_repr.is_none_or(|text| {
        canonical_integer_repr(text)
            && text.parse::<f64>().is_ok_and(|parsed| {
                parsed.to_bits() == value.to_bits() && format!("{parsed:.0}") == text
            })
    })
}

fn canonical_integer_repr(text: &str) -> bool {
    let digits = text.strip_prefix('-').unwrap_or(text);
    !digits.is_empty()
        && digits.bytes().all(|byte| byte.is_ascii_digit())
        && (digits == "0" || !digits.starts_with('0'))
        && text != "-0"
}

impl ProtectionTiming {
    fn is_valid(&self) -> bool {
        self.lookback_ms > 0
            && !self.lookback_text.is_empty()
            && !self.lock_text.is_empty()
            && match (self.duration_ms, self.unlock_at_minute_utc) {
                (Some(duration), None) => duration > 0,
                (None, Some(minute)) => minute < 24 * 60,
                _ => false,
            }
    }

    pub(super) fn lock_end(&self, trades: &[&ClosedTrade]) -> Option<i64> {
        let latest = trades.iter().map(|trade| trade.close_timestamp_ms).max()?;
        if let Some(duration) = self.duration_ms {
            return latest.checked_add(duration);
        }
        let minute = i64::from(self.unlock_at_minute_utc?);
        let day_ms = 86_400_000_i64;
        let day_start = latest - latest.rem_euclid(day_ms);
        let mut unlock = day_start + minute * 60_000;
        if unlock < latest {
            unlock += day_ms;
        }
        Some(unlock)
    }
}
