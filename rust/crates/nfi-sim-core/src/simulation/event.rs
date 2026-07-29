//! Observer-facing state projection for one chronological pair event.

use crate::portfolio::{OpenTrade, TradeSide};
use crate::protections::PairLockState;
use crate::{AssetBalance, ClosedTrade, SimulationEvent, SimulationState};

pub(super) fn simulation_event(
    timestamp_ms: i64,
    pair: &str,
    quote_free: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    rejected_signals: u64,
    locks: &[PairLockState],
) -> SimulationEvent {
    let mut base_balances: Vec<AssetBalance> = open_trades
        .iter()
        .map(|trade| AssetBalance {
            currency: trade
                .pair
                .split_once('/')
                .map_or_else(|| trade.pair.clone(), |(base, _)| base.to_owned()),
            free: if trade.side == TradeSide::Short {
                -trade.amount
            } else {
                trade.amount
            },
        })
        .collect();
    base_balances.sort_by(|left, right| left.currency.cmp(&right.currency));
    let realized_profit = closed_trades.iter().map(|trade| trade.profit_abs).sum();
    let trade_id_counter = open_trades
        .iter()
        .map(|trade| trade.id)
        .chain(closed_trades.iter().map(|trade| trade.id))
        .max()
        .unwrap_or_default();
    let order_id_counter = closed_trades
        .iter()
        .map(|trade| trade.orders.len())
        .sum::<usize>()
        + open_trades
            .iter()
            .map(|trade| trade.orders.len())
            .sum::<usize>();
    SimulationEvent {
        timestamp_ms,
        pair: pair.to_owned(),
        state: SimulationState {
            quote_free,
            base_balances,
            open_trade_count: open_trades.len(),
            realized_profit,
            closed_trade_count: closed_trades.len(),
            rejected_signals,
            trade_id_counter,
            order_id_counter,
            locks: locks.to_vec(),
        },
    }
}
