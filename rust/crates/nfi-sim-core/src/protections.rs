use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::{python_float_sum, ClosedTrade, TradeSide};

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

struct LockRequest {
    until_ms: i64,
    reason: String,
    side: &'static str,
}

#[derive(Default)]
pub(super) struct ProtectionState {
    locks: Vec<PairLockState>,
    maximum_end_by_scope: BTreeMap<(String, String), i64>,
}

impl ProtectionProgram {
    pub(super) fn is_valid(&self) -> bool {
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

    fn lock_end(&self, trades: &[&ClosedTrade]) -> Option<i64> {
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

impl ProtectionState {
    pub(super) fn locks(&self) -> &[PairLockState] {
        &self.locks
    }

    pub(super) fn is_pair_locked(&self, pair: &str, timestamp_ms: i64, side: TradeSide) -> bool {
        let side = side_name(side);
        self.scope_is_locked(pair, timestamp_ms, side)
            || self.scope_is_locked("*", timestamp_ms, side)
    }

    pub(super) fn after_trade_close(
        &mut self,
        program: &ProtectionProgram,
        closed_trade: &ClosedTrade,
        closed_trades: &[ClosedTrade],
        starting_balance: f64,
    ) {
        let side = trade_side(closed_trade);
        for handler in &program.handlers {
            if let Some(request) = handler.local_lock(closed_trade, closed_trades, side) {
                self.add_local_lock(
                    &closed_trade.pair,
                    closed_trade.close_timestamp_ms,
                    request,
                    program.timeframe_ms,
                );
            }
        }
        for handler in &program.handlers {
            if let Some(request) =
                handler.global_lock(closed_trade, closed_trades, side, starting_balance)
            {
                self.add_global_lock(
                    closed_trade.close_timestamp_ms,
                    request,
                    program.timeframe_ms,
                );
            }
        }
    }

    fn add_local_lock(&mut self, pair: &str, now_ms: i64, request: LockRequest, timeframe_ms: i64) {
        if self.scope_is_locked(pair, request.until_ms, request.side)
            || self.scope_is_locked("*", request.until_ms, request.side)
        {
            return;
        }
        self.add_lock(pair, now_ms, request, timeframe_ms);
    }

    fn add_global_lock(&mut self, now_ms: i64, request: LockRequest, timeframe_ms: i64) {
        if self.scope_is_locked("*", request.until_ms, request.side) {
            return;
        }
        self.add_lock("*", now_ms, request, timeframe_ms);
    }

    fn add_lock(&mut self, pair: &str, now_ms: i64, request: LockRequest, timeframe_ms: i64) {
        // CCXT ROUND_UP always advances one complete timeframe, even when
        // the requested end already lies exactly on a candle boundary.
        let rounded_end =
            request.until_ms - request.until_ms.rem_euclid(timeframe_ms) + timeframe_ms;
        let scope = (pair.to_owned(), request.side.to_owned());
        self.maximum_end_by_scope
            .entry(scope)
            .and_modify(|end| *end = (*end).max(rounded_end))
            .or_insert(rounded_end);
        self.locks.push(PairLockState {
            pair: pair.to_owned(),
            lock_timestamp_ms: now_ms,
            lock_end_timestamp_ms: rounded_end,
            reason: request.reason,
            side: request.side.to_owned(),
            active: true,
        });
    }

    fn scope_is_locked(&self, pair: &str, timestamp_ms: i64, side: &str) -> bool {
        self.maximum_end(pair, "*") > timestamp_ms
            || (side != "*" && self.maximum_end(pair, side) > timestamp_ms)
    }

    fn maximum_end(&self, pair: &str, side: &str) -> i64 {
        self.maximum_end_by_scope
            .get(&(pair.to_owned(), side.to_owned()))
            .copied()
            .unwrap_or_default()
    }
}

impl ProtectionHandler {
    fn local_lock(
        &self,
        closed_trade: &ClosedTrade,
        closed_trades: &[ClosedTrade],
        side: TradeSide,
    ) -> Option<LockRequest> {
        match self {
            Self::CooldownPeriod { timing } => {
                let trades = recent_trades(
                    closed_trades,
                    closed_trade.close_timestamp_ms,
                    timing.lookback_ms,
                    Some(&closed_trade.pair),
                );
                timing.lock_end(&trades).map(|until_ms| LockRequest {
                    until_ms,
                    reason: format!("Cooldown period for {}.", timing.lock_text),
                    side: "*",
                })
            }
            Self::StoplossGuard {
                timing,
                trade_limit,
                only_per_side,
                required_profit,
                ..
            } => stoploss_lock(
                timing,
                *trade_limit,
                *only_per_side,
                *required_profit,
                closed_trade.close_timestamp_ms,
                Some(&closed_trade.pair),
                side,
                closed_trades,
            ),
            Self::LowProfitPairs {
                timing,
                trade_limit,
                only_per_side,
                required_profit,
            } => low_profit_lock(
                timing,
                *trade_limit,
                *only_per_side,
                *required_profit,
                closed_trade,
                side,
                closed_trades,
            ),
            Self::MaxDrawdown { .. } => None,
        }
    }

    fn global_lock(
        &self,
        closed_trade: &ClosedTrade,
        closed_trades: &[ClosedTrade],
        side: TradeSide,
        starting_balance: f64,
    ) -> Option<LockRequest> {
        match self {
            Self::StoplossGuard {
                timing,
                trade_limit,
                only_per_pair,
                only_per_side,
                required_profit,
            } if !only_per_pair => stoploss_lock(
                timing,
                *trade_limit,
                *only_per_side,
                *required_profit,
                closed_trade.close_timestamp_ms,
                None,
                side,
                closed_trades,
            ),
            Self::MaxDrawdown {
                timing,
                trade_limit,
                maximum_allowed_drawdown,
                calculation_mode,
            } => max_drawdown_lock(
                timing,
                *trade_limit,
                *maximum_allowed_drawdown,
                *calculation_mode,
                closed_trade.close_timestamp_ms,
                closed_trades,
                starting_balance,
            ),
            _ => None,
        }
    }
}

fn recent_trades<'a>(
    closed_trades: &'a [ClosedTrade],
    now_ms: i64,
    lookback_ms: i64,
    pair: Option<&str>,
) -> Vec<&'a ClosedTrade> {
    let cutoff = now_ms - lookback_ms;
    closed_trades
        .iter()
        .filter(|trade| {
            trade.close_timestamp_ms > cutoff
                && trade.close_timestamp_ms <= now_ms
                && pair.is_none_or(|value| trade.pair == value)
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn stoploss_lock(
    timing: &ProtectionTiming,
    trade_limit: usize,
    only_per_side: bool,
    required_profit: f64,
    now_ms: i64,
    pair: Option<&str>,
    side: TradeSide,
    closed_trades: &[ClosedTrade],
) -> Option<LockRequest> {
    let trades = recent_trades(closed_trades, now_ms, timing.lookback_ms, pair)
        .into_iter()
        .filter(|trade| {
            matches!(
                trade.exit_reason.as_str(),
                "trailing_stop_loss" | "stop_loss" | "stoploss_on_exchange" | "liquidation"
            ) && trade.profit_ratio != 0.0
                && trade.profit_ratio < required_profit
                && (!only_per_side || trade_side(trade) == side)
        })
        .collect::<Vec<_>>();
    if trades.len() < trade_limit {
        return None;
    }
    timing.lock_end(&trades).map(|until_ms| LockRequest {
        until_ms,
        reason: format!(
            "{trade_limit} stoplosses in {} min, locking {}.",
            timing.lookback_ms / 60_000,
            timing.lock_text
        ),
        side: lock_side(only_per_side, side),
    })
}

fn low_profit_lock(
    timing: &ProtectionTiming,
    trade_limit: usize,
    only_per_side: bool,
    required_profit: f64,
    closed_trade: &ClosedTrade,
    side: TradeSide,
    closed_trades: &[ClosedTrade],
) -> Option<LockRequest> {
    let trades = recent_trades(
        closed_trades,
        closed_trade.close_timestamp_ms,
        timing.lookback_ms,
        Some(&closed_trade.pair),
    );
    // Freqtrade checks the unfiltered pair count before applying only_per_side.
    if trades.len() < trade_limit {
        return None;
    }
    let profit = python_float_sum(
        trades
            .iter()
            .filter(|trade| !only_per_side || trade_side(trade) == side)
            .filter_map(|trade| (trade.profit_ratio != 0.0).then_some(trade.profit_ratio)),
    );
    if profit >= required_profit {
        return None;
    }
    timing.lock_end(&trades).map(|until_ms| LockRequest {
        until_ms,
        reason: format!(
            "{} < {} in {}, locking {}.",
            python_float_repr(profit),
            python_float_repr(required_profit),
            timing.lookback_text,
            timing.lock_text
        ),
        side: lock_side(only_per_side, side),
    })
}

fn max_drawdown_lock(
    timing: &ProtectionTiming,
    trade_limit: usize,
    maximum_allowed_drawdown: f64,
    calculation_mode: DrawdownMode,
    now_ms: i64,
    closed_trades: &[ClosedTrade],
    starting_balance: f64,
) -> Option<LockRequest> {
    let trades = recent_trades(closed_trades, now_ms, timing.lookback_ms, None);
    if trades.len() < trade_limit {
        return None;
    }
    let drawdown = match calculation_mode {
        DrawdownMode::Ratios => {
            maximum_drawdown(trades.iter().map(|trade| trade.profit_ratio), 0.0, false)
        }
        DrawdownMode::Equity => {
            let cutoff = now_ms - timing.lookback_ms;
            let profit_before_window = python_float_sum(
                closed_trades
                    .iter()
                    .filter(|trade| trade.close_timestamp_ms <= cutoff)
                    .map(|trade| trade.profit_abs),
            );
            maximum_drawdown(
                trades.iter().map(|trade| trade.profit_abs),
                starting_balance + profit_before_window,
                true,
            )
        }
    };
    if drawdown <= maximum_allowed_drawdown {
        return None;
    }
    timing.lock_end(&trades).map(|until_ms| LockRequest {
        until_ms,
        reason: format!(
            "{} passed {} in {}, locking {}.",
            python_float_repr(drawdown),
            python_float_repr(maximum_allowed_drawdown),
            timing.lookback_text,
            timing.lock_text
        ),
        side: "*",
    })
}

fn maximum_drawdown(
    values: impl IntoIterator<Item = f64>,
    starting_balance: f64,
    relative: bool,
) -> f64 {
    let mut cumulative = 0.0_f64;
    let mut high = 0.0_f64;
    let mut maximum = 0.0_f64;
    for value in values {
        cumulative += value;
        high = high.max(cumulative);
        let drawdown = if relative {
            let high_balance = starting_balance + high;
            (high_balance - (starting_balance + cumulative)) / high_balance
        } else {
            high - cumulative
        };
        maximum = maximum.max(drawdown);
    }
    maximum
}

fn trade_side(trade: &ClosedTrade) -> TradeSide {
    if trade.is_short {
        TradeSide::Short
    } else {
        TradeSide::Long
    }
}

const fn side_name(side: TradeSide) -> &'static str {
    match side {
        TradeSide::Long => "long",
        TradeSide::Short => "short",
    }
}

const fn lock_side(only_per_side: bool, side: TradeSide) -> &'static str {
    if only_per_side {
        side_name(side)
    } else {
        "*"
    }
}

fn python_float_repr(value: f64) -> String {
    let mut text = value.to_string();
    if !text.contains(['.', 'e', 'E']) {
        text.push_str(".0");
    }
    text
}
