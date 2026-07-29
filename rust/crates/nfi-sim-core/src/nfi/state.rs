use std::collections::BTreeMap;

use crate::portfolio::{OpenTrade, TradeSide};
use crate::{Candle, PairSeries, PortfolioConfig};

/// Immutable inputs shared by every NFI position-adjustment route.
///
/// Keeping the callback boundary in one value makes route dispatch readable
/// and prevents future callback fields from expanding every function
/// signature independently.
#[derive(Clone, Copy)]
pub(crate) struct PositionAdjustmentRequest<'a> {
    pub(crate) pair: &'a PairSeries,
    pub(crate) candle_index: usize,
    pub(crate) candle: &'a Candle,
    pub(crate) config: &'a PortfolioConfig,
    pub(crate) available_balance: f64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct NfiProfitSnapshot {
    pub(crate) stake: f64,
    pub(crate) ratio: f64,
    pub(crate) current_stake_ratio: f64,
    pub(crate) initial_stake_ratio: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ProfitTarget {
    pub(crate) rate: f64,
    pub(crate) profit: f64,
    pub(crate) sell_reason: String,
    pub(crate) time_profit_reached_ms: i64,
}

pub(crate) fn nfi_trade_is_derisked(trade: &OpenTrade) -> Option<bool> {
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let tagged_exit = trade
        .orders
        .iter()
        .filter(|order| !order.is_entry)
        .any(|order| {
            order
                .tag
                .as_deref()
                .and_then(|tag| tag.split_whitespace().next())
                .is_some_and(|tag| {
                    matches!(
                        tag,
                        "d" | "d1" | "derisk_level_1" | "derisk_level_2" | "derisk_level_3"
                    )
                })
        });
    Some(tagged_exit || trade.amount < first_entry.amount * 0.95)
}

pub(crate) fn set_profit_target(
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
    trade: &OpenTrade,
    candle: &Candle,
    sell_reason: String,
    profit: f64,
) {
    profit_targets.insert(
        trade.pair.clone(),
        ProfitTarget {
            rate: candle.open,
            profit,
            sell_reason,
            time_profit_reached_ms: candle.timestamp_ms,
        },
    );
}

pub(crate) fn nfi_profit_bucket(profit: f64) -> Option<u8> {
    if profit < 0.001 {
        return None;
    }
    if profit >= 0.12 {
        return Some(12);
    }
    let mut bucket = 0_u8;
    for candidate in 1_u8..=11 {
        if profit >= f64::from(candidate) / 100.0 {
            bucket = candidate;
        }
    }
    Some(bucket)
}

pub(crate) fn nfi_profit_snapshot(
    trade: &OpenTrade,
    exit_rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    is_futures: bool,
) -> Option<NfiProfitSnapshot> {
    if !exit_rate.is_finite()
        || !open_fee_rate.is_finite()
        || !close_fee_rate.is_finite()
        || trade.orders.is_empty()
    {
        return None;
    }
    let mut total_amount = 0.0;
    let mut total_stake = 0.0;
    let mut total_profit = 0.0;
    let (open_multiplier, close_multiplier) = if trade.side == TradeSide::Short {
        (1.0 - open_fee_rate, 1.0 + close_fee_rate)
    } else {
        (1.0 + open_fee_rate, 1.0 - close_fee_rate)
    };
    let mut first_entry_cost = None;
    for order in &trade.orders {
        let stake = order.amount * order.price;
        if order.is_entry {
            first_entry_cost.get_or_insert(stake);
            let entry_stake = stake * open_multiplier;
            total_amount += order.amount;
            total_stake += entry_stake;
            if trade.side == TradeSide::Short {
                total_profit += entry_stake;
            } else {
                total_profit -= entry_stake;
            }
        } else {
            let exit_stake = stake * close_multiplier;
            total_amount -= order.amount;
            if trade.side == TradeSide::Short {
                total_profit -= exit_stake;
            } else {
                total_profit += exit_stake;
            }
        }
    }
    let current_stake = total_amount * exit_rate * close_multiplier;
    if trade.side == TradeSide::Short {
        total_profit -= current_stake;
    } else {
        total_profit += current_stake;
    }
    if is_futures {
        // NFI reads `trade.funding_fees`, which Freqtrade keeps as the
        // cumulative fee across filled orders plus the current running
        // interval. A partial exit realizes part of the position but does not
        // reduce this callback-visible cumulative value.
        total_profit += trade.funding_fees_total;
    }
    let first_entry_cost = first_entry_cost?;
    if total_stake == 0.0 || current_stake == 0.0 || first_entry_cost == 0.0 {
        return None;
    }
    Some(NfiProfitSnapshot {
        stake: total_profit,
        ratio: total_profit / total_stake,
        current_stake_ratio: total_profit / current_stake,
        initial_stake_ratio: total_profit / first_entry_cost,
    })
}
