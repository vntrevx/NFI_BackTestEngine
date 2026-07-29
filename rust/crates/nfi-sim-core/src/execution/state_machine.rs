//! Trade-context adapter for the generic state-machine VM.

use serde_json::{json, Value};

use crate::calculations::fee_close;
use crate::callbacks::feature_number_at;
use crate::domain::{
    AdjustmentSignal, Candle, PairSeries, PortfolioConfig, SimError, StateMachineActionKind,
    StateMachineProgram, StateMachineReadSource,
};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::{evaluate_state_machine, StateMachineContext};

use super::{adjustment_minimum_pair_stake, current_profit_ratio};

pub(crate) fn evaluate_state_machine_adjustment(
    program: &StateMachineProgram,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<Option<AdjustmentSignal>, SimError> {
    let mut context = trade_context(
        program,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )?;
    let action = evaluate_state_machine(program, "adjust_trade_position", &mut context)
        .map_err(|_| SimError::InvalidStateMachineProgram)?;
    let signal = match action {
        None => None,
        Some(action) if action.kind == StateMachineActionKind::NoOp => None,
        Some(action)
            if matches!(
                action.kind,
                StateMachineActionKind::Stop | StateMachineActionKind::Exit
            ) =>
        {
            return Err(SimError::InvalidStateMachineProgram);
        }
        Some(action) => {
            let mut stake = action.stake.ok_or(SimError::InvalidStateMachineProgram)?;
            if matches!(
                action.kind,
                StateMachineActionKind::PartialExit | StateMachineActionKind::Derisk
            ) {
                stake = -stake.abs();
            } else {
                stake = stake.abs();
            }
            let tag = action
                .tag
                .filter(|tag| !tag.is_empty())
                .ok_or(SimError::InvalidStateMachineProgram)?;
            Some(AdjustmentSignal {
                stake_amount: stake,
                tag,
            })
        }
    };
    trade.custom_data = context.custom_state;
    Ok(signal)
}

pub(crate) fn evaluate_state_machine_exit(
    program: &StateMachineProgram,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Result<Option<String>, SimError> {
    let mut context = trade_context(program, trade, pair, candle_index, candle, config, 0.0)?;
    let action = evaluate_state_machine(program, "custom_exit", &mut context)
        .map_err(|_| SimError::InvalidStateMachineProgram)?;
    let tag = match action {
        None => None,
        Some(action) if action.kind == StateMachineActionKind::NoOp => None,
        Some(action)
            if matches!(
                action.kind,
                StateMachineActionKind::Stop | StateMachineActionKind::Exit
            ) =>
        {
            Some(
                action
                    .tag
                    .filter(|tag| !tag.is_empty())
                    .ok_or(SimError::InvalidStateMachineProgram)?,
            )
        }
        Some(_) => return Err(SimError::InvalidStateMachineProgram),
    };
    trade.custom_data = context.custom_state;
    Ok(tag)
}

#[allow(clippy::too_many_arguments)]
fn trade_context(
    program: &StateMachineProgram,
    trade: &mut OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Result<StateMachineContext, SimError> {
    let mut context = StateMachineContext {
        custom_state: trade.custom_data.clone(),
        ..StateMachineContext::default()
    };
    let current_profit = current_profit_ratio(trade, candle.open, fee_close(config));
    for read in &program.required_reads {
        let destination = match read.source {
            StateMachineReadSource::Candle => &mut context.candle,
            StateMachineReadSource::Wallet => &mut context.wallet,
            StateMachineReadSource::Trade => &mut context.trade,
            StateMachineReadSource::Orders => &mut context.orders,
            StateMachineReadSource::Input => &mut context.input,
            StateMachineReadSource::CustomState | StateMachineReadSource::Local => continue,
        };
        let value = match read.source {
            StateMachineReadSource::Candle => {
                candle_value(&read.key, pair, candle_index, candle, current_profit)
            }
            StateMachineReadSource::Wallet => {
                wallet_value(&read.key, pair, candle, config, available_balance)
            }
            StateMachineReadSource::Trade => trade_value(&read.key, trade),
            StateMachineReadSource::Orders => order_value(&read.key, trade),
            StateMachineReadSource::Input => input_value(&read.key, pair, candle),
            StateMachineReadSource::CustomState | StateMachineReadSource::Local => None,
        }
        .ok_or(SimError::InvalidStateMachineProgram)?;
        destination.insert(read.key.clone(), value);
    }
    Ok(context)
}

fn candle_value(
    key: &str,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    current_profit: f64,
) -> Option<Value> {
    let value = match key {
        "current_time" => return Some(Value::Number(candle.timestamp_ms.into())),
        "current_rate" | "current_entry_rate" | "current_exit_rate" => candle.open,
        "current_profit" | "current_entry_profit" | "current_exit_profit" => current_profit,
        name => feature_number_at(pair, candle_index, name)?,
    };
    serde_json::Number::from_f64(value).map(Value::Number)
}

fn wallet_value(
    key: &str,
    pair: &PairSeries,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Value> {
    let value = match key {
        "min_stake" => {
            adjustment_minimum_pair_stake(pair, candle.open, config.amount_reserve_percent)
        }
        "max_stake" => available_balance,
        _ => return None,
    };
    serde_json::Number::from_f64(value).map(Value::Number)
}

pub(super) fn trade_value(key: &str, trade: &OpenTrade) -> Option<Value> {
    match key {
        "pair" => Some(Value::String(trade.pair.clone())),
        "entry_tag" => Some(
            trade
                .entry_tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
        "is_short" => Some(Value::Bool(trade.side == TradeSide::Short)),
        "is_long" => Some(Value::Bool(trade.side == TradeSide::Long)),
        "leverage" => serde_json::Number::from_f64(trade.leverage).map(Value::Number),
        "stake_amount" => serde_json::Number::from_f64(trade.stake_amount).map(Value::Number),
        "amount" => serde_json::Number::from_f64(trade.amount).map(Value::Number),
        _ => None,
    }
}

pub(super) fn order_value(key: &str, trade: &OpenTrade) -> Option<Value> {
    match key {
        "filled_entries" => Some(Value::Array(
            trade
                .orders
                .iter()
                .filter(|order| order.is_entry)
                .map(order_json)
                .collect(),
        )),
        "filled_exits" => Some(Value::Array(
            trade
                .orders
                .iter()
                .filter(|order| !order.is_entry)
                .map(order_json)
                .collect(),
        )),
        "count" => Some(Value::Number(
            u64::try_from(trade.orders.len()).ok()?.into(),
        )),
        _ => None,
    }
}

fn order_json(order: &crate::domain::FilledOrder) -> Value {
    json!({
        "id": order.id,
        "is_entry": order.is_entry,
        "filled_timestamp_ms": order.filled_timestamp_ms,
        "amount": order.amount,
        "price": order.price,
        "cost": order.cost,
        "tag": order.tag,
    })
}

fn input_value(key: &str, pair: &PairSeries, candle: &Candle) -> Option<Value> {
    match key {
        "pair" => Some(Value::String(pair.pair.clone())),
        "current_time" => Some(Value::Number(candle.timestamp_ms.into())),
        _ => None,
    }
}
