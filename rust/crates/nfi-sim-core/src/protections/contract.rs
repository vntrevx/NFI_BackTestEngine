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
        calculation_mode: DrawdownMode,
    },
    LowProfitPairs {
        timing: ProtectionTiming,
        trade_limit: usize,
        only_per_side: bool,
        required_profit: f64,
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
                Self::MaxDrawdown {
                    maximum_allowed_drawdown,
                    ..
                } => *maximum_allowed_drawdown >= 0.0,
                _ => true,
            }
    }
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
