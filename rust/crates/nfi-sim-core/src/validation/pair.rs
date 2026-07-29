//! Pair-series, candle, and entry-signal validation.

use std::collections::BTreeSet;

use crate::domain::{Candle, EntrySignal, NfiX7TradeManager, PairSeries, SimError};
use crate::portfolio::TradeSide;

use super::routing::unsupported_nfi_pair_signal;

pub(crate) fn freqtrade_entry_signal(
    candle: &Candle,
    can_short: bool,
) -> Option<(TradeSide, &EntrySignal)> {
    let enter_long = candle.enter_long.as_ref();
    let enter_short = can_short.then_some(candle.enter_short.as_ref()).flatten();
    if candle.exit_long.is_none() && enter_short.is_none() {
        if let Some(signal) = enter_long {
            return Some((TradeSide::Long, signal));
        }
    }
    if candle.exit_short.is_none() && enter_long.is_none() {
        if let Some(signal) = enter_short {
            return Some((TradeSide::Short, signal));
        }
    }
    None
}

pub(crate) fn validate_pair_series(
    pair_index: usize,
    pair: &PairSeries,
    nfi_manager: Option<&NfiX7TradeManager>,
    can_short: bool,
    logical_timestamps: &mut BTreeSet<i64>,
) -> Result<(), SimError> {
    if pair.pair.is_empty() {
        return Err(SimError::EmptyPair(pair_index));
    }
    if pair.candles.is_empty() {
        return Err(SimError::EmptyCandles(pair.pair.clone()));
    }
    if pair.execution_start_index >= pair.candles.len() {
        return Err(SimError::InvalidExecutionStart {
            pair: pair.pair.clone(),
            index: pair.execution_start_index,
            rows: pair.candles.len(),
        });
    }
    for (name, value) in [
        ("pair.amount_step", pair.amount_step),
        ("pair.price_step", pair.price_step),
    ] {
        if value.is_some_and(|step| !step.is_finite() || step <= 0.0) {
            return Err(SimError::InvalidPositiveConfig(name));
        }
    }
    let mut previous_step_timestamp = None;
    for change in &pair.price_steps {
        if change.timestamp_ms < 0
            || !change.step.is_finite()
            || change.step <= 0.0
            || previous_step_timestamp.is_some_and(|previous| change.timestamp_ms <= previous)
        {
            return Err(SimError::InvalidPositiveConfig("pair.price_steps"));
        }
        previous_step_timestamp = Some(change.timestamp_ms);
    }
    for (column, values) in &pair.feature_columns {
        if column.is_empty() || values.is_empty() || values.len() != pair.candles.len() {
            return Err(SimError::InvalidFeatureColumn {
                pair: pair.pair.clone(),
                column: column.clone(),
            });
        }
    }
    let mut previous = None;
    let mut unsupported_nfi_signal = None;
    let mut entry_indices = Vec::new();
    for (index, candle) in pair.candles.iter().enumerate() {
        if previous.is_some_and(|value| candle.timestamp_ms <= value) {
            return Err(SimError::CandleOrder {
                pair: pair.pair.clone(),
                index,
            });
        }
        previous = Some(candle.timestamp_ms);
        validate_candle(pair, index, &candle)?;
        if index >= pair.execution_start_index {
            logical_timestamps.insert(candle.timestamp_ms);
        }
        if freqtrade_entry_signal(&candle, can_short).is_some() {
            entry_indices.push(index);
        }
        // The old validator made a second full pass over every pair solely for
        // this short-tag check. Retain the first unsupported signal while the
        // general validation pass continues, preserving the prior error
        // precedence without reading a multi-year spool twice.
        if unsupported_nfi_signal.is_none() {
            unsupported_nfi_signal = nfi_manager
                .and_then(|manager| unsupported_nfi_pair_signal(pair, &candle, manager, can_short));
        }
    }
    if let Some(error) = unsupported_nfi_signal {
        return Err(error);
    }
    // File-backed scheduling can now jump by binary search instead of reading
    // every idle row again. Owned fixtures remain in memory and need no index.
    pair.candles.install_entry_indices(entry_indices);
    Ok(())
}

fn validate_candle(pair: &PairSeries, index: usize, candle: &Candle) -> Result<(), SimError> {
    let values = [
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
    ];
    if candle.timestamp_ms < 0
        || values.iter().any(|value| !value.is_finite())
        || candle.open <= 0.0
        || candle.high < candle.low
        || candle.low <= 0.0
        || candle.volume < 0.0
        || candle.funding_rate.is_some_and(|rate| !rate.is_finite())
        || candle
            .funding_mark_price
            .is_some_and(|price| !price.is_finite() || price <= 0.0)
        || candle.funding_rate.is_some() != candle.funding_mark_price.is_some()
    {
        return Err(SimError::InvalidCandle {
            pair: pair.pair.clone(),
            index,
        });
    }
    for signal in [&candle.enter_long, &candle.enter_short]
        .into_iter()
        .flatten()
    {
        if signal
            .leverage
            .is_some_and(|leverage| !leverage.is_finite() || leverage <= 0.0)
        {
            return Err(SimError::InvalidLeverage {
                pair: pair.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            });
        }
        if signal
            .liquidation_price
            .is_some_and(|price| !price.is_finite() || price <= 0.0)
        {
            return Err(SimError::InvalidLiquidationPrice {
                pair: pair.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            });
        }
    }
    if pair
        .minimum_stake
        .is_some_and(|stake| !stake.is_finite() || stake < 0.0)
        || pair
            .minimum_amount
            .is_some_and(|amount| !amount.is_finite() || amount < 0.0)
        || pair
            .minimum_cost
            .is_some_and(|cost| !cost.is_finite() || cost < 0.0)
    {
        return Err(SimError::InvalidPositiveConfig("pair_stake_limits"));
    }
    Ok(())
}
