//! Callback input assembly and bounded callback evaluation.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::fee_close;
use crate::domain::{AdjustmentSignal, Candle, PairSeries, PortfolioConfig, ScalarProgramBundle};
use crate::execution::{adjustment_minimum_pair_stake, current_profit_ratio};
use crate::nfi::CustomExitDecision;
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{
    evaluate_scalar_program_bundle, number_value, scalar_number, scalar_truthy,
};

use super::projection::insert_feature_window;

pub(crate) fn feature_number_at(pair: &PairSeries, index: usize, name: &str) -> Option<f64> {
    let candle = pair.candles.get(index)?;
    match name {
        "open" => Some(candle.open),
        "high" => Some(candle.high),
        "low" => Some(candle.low),
        "close" => Some(candle.close),
        "volume" => Some(candle.volume),
        _ => pair.feature_columns.get(name)?.number(index),
    }
}

pub(crate) fn feature_bool_at(pair: &PairSeries, index: usize, name: &str) -> Option<bool> {
    pair.feature_columns.get(name)?.boolean(index)
}

pub(crate) fn evaluate_custom_exit_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<CustomExitDecision> {
    let trade_value = scalar_trade_value(trade)?;
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        ("trade".to_owned(), trade_value),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        ("current_rate".to_owned(), number_value(candle.open)?),
        (
            "current_profit".to_owned(),
            number_value(current_profit_ratio(trade, candle.open, fee_close(config)))?,
        ),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index)?;
    let value = evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, &variables)?;
    if !scalar_truthy(&value) {
        return Some(CustomExitDecision::NoExit);
    }
    // Freqtrade preserves a truthy string as the custom reason. Any other
    // truthy Python value exits with ExitType.CUSTOM_EXIT's default reason.
    let reason = value.as_str().map_or_else(
        || "custom_exit".to_owned(),
        |value| value.chars().take(255).collect(),
    );
    Some(CustomExitDecision::Exit(reason))
}

pub(crate) fn scalar_trade_value(trade: &OpenTrade) -> Option<Value> {
    let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
    let exit_count = trade.orders.iter().filter(|order| !order.is_entry).count();
    Some(Value::Object(serde_json::Map::from_iter([
        ("id".to_owned(), Value::Number(trade.id.into())),
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        (
            "is_short".to_owned(),
            Value::Bool(trade.side == TradeSide::Short),
        ),
        ("amount".to_owned(), number_value(trade.amount)?),
        ("stake_amount".to_owned(), number_value(trade.stake_amount)?),
        ("open_rate".to_owned(), number_value(trade.open_rate)?),
        ("leverage".to_owned(), number_value(trade.leverage)?),
        (
            "open_date_utc".to_owned(),
            Value::Number(trade.open_timestamp_ms.into()),
        ),
        (
            "enter_tag".to_owned(),
            trade
                .entry_tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
        (
            "nr_of_successful_entries".to_owned(),
            Value::Number(u64::try_from(entry_count).ok()?.into()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            Value::Number(u64::try_from(exit_count).ok()?.into()),
        ),
        (
            "custom_data".to_owned(),
            Value::Object(trade.custom_data.clone().into_iter().collect()),
        ),
    ])))
}

pub(crate) fn evaluate_adjustment_bundle(
    bundle: &ScalarProgramBundle,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<Option<AdjustmentSignal>, ()> {
    let has_minimum = pair.minimum_stake.is_some()
        || pair.minimum_amount.is_some()
        || pair.minimum_cost.is_some();
    // Adjustment callbacks use Freqtrade's unleveraged minimum-stake
    // boundary, not the leverage-aware entry-order boundary.
    let minimum_stake = if has_minimum {
        number_value(adjustment_minimum_pair_stake(
            pair,
            candle.open,
            config.amount_reserve_percent,
        ))
        .ok_or(())?
    } else {
        Value::Null
    };
    let current_profit =
        number_value(current_profit_ratio(trade, candle.open, fee_close(config))).ok_or(())?;
    let mut variables = BTreeMap::from([
        ("trade".to_owned(), scalar_trade_value(trade).ok_or(())?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "current_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_profit".to_owned(), current_profit.clone()),
        ("min_stake".to_owned(), minimum_stake),
        (
            "max_stake".to_owned(),
            number_value(available_balance).ok_or(())?,
        ),
        (
            "current_entry_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        (
            "current_exit_rate".to_owned(),
            number_value(candle.open).ok_or(())?,
        ),
        ("current_entry_profit".to_owned(), current_profit.clone()),
        ("current_exit_profit".to_owned(), current_profit),
        ("kwargs".to_owned(), Value::Object(serde_json::Map::new())),
    ]);
    insert_feature_window(&mut variables, pair, candle_index).ok_or(())?;
    let value =
        evaluate_scalar_program_bundle(&bundle.programs, &bundle.entry, &variables).ok_or(())?;
    let (stake_amount, tag) = match value {
        Value::Null => return Ok(None),
        Value::Array(values) => {
            let stake = scalar_adjustment_number(values.first().ok_or(())?).ok_or(())?;
            let tag = match values.get(1) {
                None | Some(Value::Null | Value::Bool(false)) => String::new(),
                Some(Value::String(tag)) => tag.clone(),
                _ => return Err(()),
            };
            (stake, tag)
        }
        value => (scalar_adjustment_number(&value).ok_or(())?, String::new()),
    };
    if !stake_amount.is_finite() || stake_amount == 0.0 {
        return Ok(None);
    }
    if stake_amount > 0.0 && config.max_entry_position_adjustment >= 0 {
        let entry_count = trade.orders.iter().filter(|order| order.is_entry).count();
        if i64::try_from(entry_count).map_err(|_| ())? > config.max_entry_position_adjustment {
            return Ok(None);
        }
    }
    Ok(Some(AdjustmentSignal { stake_amount, tag }))
}

/// Map an execution candle to the last analyzed candle visible to callbacks.
///
/// Freqtrade shifts entry/exit signals onto the next candle before simulation,
/// but its data provider still ends at the last fully analyzed candle. At an
/// execution time of 15:30, callbacks therefore see the 15:25 row. Keeping
/// this translation at the callback boundary prevents order prices/timestamps
/// from being shifted along with indicator data.
fn scalar_adjustment_number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        value => scalar_number(value),
    }
}
