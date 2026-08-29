//! Entry and exit confirmation program evaluation.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::{fee_close, fee_open};
use crate::domain::{ConfirmProgram, OrderType, PortfolioConfig};
use crate::nfi::nfi_profit_snapshot;
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{integer_value, number_value};

use super::exit::current_profit_ratio;

#[derive(Clone, Copy)]
pub(crate) struct ConfirmInputs<'a> {
    pub(crate) pair: &'a str,
    pub(crate) timestamp_ms: i64,
    pub(crate) amount: f64,
    pub(crate) rate: f64,
    pub(crate) entry_tag: Option<&'a str>,
    pub(crate) side: TradeSide,
    pub(crate) previous_close: Option<f64>,
    pub(crate) open_trades: &'a [OpenTrade],
    pub(crate) max_open_trades: usize,
    pub(crate) is_futures: bool,
    pub(crate) order_type: OrderType,
}

pub(crate) enum ConfirmControl {
    Continue,
    Return(Value),
}

pub(crate) fn evaluate_confirm_program(
    program: &ConfirmProgram,
    inputs: ConfirmInputs<'_>,
) -> Option<bool> {
    let side = match inputs.side {
        TradeSide::Long => "long",
        TradeSide::Short => "short",
    };
    let open_trades = Value::Array(
        inputs
            .open_trades
            .iter()
            .map(|trade| {
                Value::Object(serde_json::Map::from_iter([
                    (
                        "trade_direction".to_owned(),
                        Value::String(
                            match trade.side {
                                TradeSide::Long => "long",
                                TradeSide::Short => "short",
                            }
                            .to_owned(),
                        ),
                    ),
                    (
                        "enter_tag".to_owned(),
                        trade
                            .entry_tag
                            .as_ref()
                            .map_or(Value::Null, |tag| Value::String(tag.clone())),
                    ),
                ]))
            })
            .collect(),
    );
    let analyzed_frame = Value::Array(
        inputs
            .previous_close
            .and_then(number_value)
            .map(|close| Value::Object(serde_json::Map::from_iter([("close".to_owned(), close)])))
            .into_iter()
            .collect(),
    );
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(inputs.pair.to_owned())),
        (
            "order_type".to_owned(),
            Value::String(inputs.order_type.as_str().to_owned()),
        ),
        ("amount".to_owned(), number_value(inputs.amount)?),
        ("rate".to_owned(), number_value(inputs.rate)?),
        ("time_in_force".to_owned(), Value::String("gtc".to_owned())),
        (
            "current_time".to_owned(),
            Value::Number(inputs.timestamp_ms.into()),
        ),
        (
            "entry_tag".to_owned(),
            Value::String(inputs.entry_tag?.to_owned()),
        ),
        ("side".to_owned(), Value::String(side.to_owned())),
        ("open_trades".to_owned(), open_trades),
        ("analyzed_frame".to_owned(), analyzed_frame),
        (
            "config.max_open_trades".to_owned(),
            Value::Number(u64::try_from(inputs.max_open_trades).ok()?.into()),
        ),
        (
            "config.is_futures".to_owned(),
            Value::Bool(inputs.is_futures),
        ),
    ]);
    let ConfirmControl::Return(value) =
        evaluate_confirm_statements(&program.statements, &mut variables, program, 0)?
    else {
        return None;
    };
    value.as_bool()
}

pub(crate) fn evaluate_exit_confirm_program(
    program: &ConfirmProgram,
    trade: &OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    exit_reason: &str,
    config: &PortfolioConfig,
) -> Option<(bool, bool)> {
    let liquidation_price = trade
        .liquidation_price
        .and_then(number_value)
        .unwrap_or(Value::Null);
    let profit_snapshot = nfi_profit_snapshot(
        trade,
        rate,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    );
    let snapshot_value = |value: Option<f64>| value.and_then(number_value).unwrap_or(Value::Null);
    let trade_value = Value::Object(serde_json::Map::from_iter([
        (
            "realized_profit".to_owned(),
            number_value(trade.realized_partial_profit)?,
        ),
        ("stake_amount".to_owned(), number_value(trade.stake_amount)?),
        (
            "is_short".to_owned(),
            Value::Bool(trade.side == TradeSide::Short),
        ),
        ("liquidation_price".to_owned(), liquidation_price),
        ("open_rate".to_owned(), number_value(trade.open_rate)?),
        ("fee_close".to_owned(), number_value(fee_close(config))?),
        (
            "nfi_profit_stake".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.stake)),
        ),
        (
            "nfi_profit_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.ratio)),
        ),
        (
            "nfi_profit_current_stake_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.current_stake_ratio)),
        ),
        (
            "nfi_profit_initial_stake_ratio".to_owned(),
            snapshot_value(profit_snapshot.map(|snapshot| snapshot.initial_stake_ratio)),
        ),
    ]));
    let mut variables = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        ("trade".to_owned(), trade_value),
        (
            "order_type".to_owned(),
            Value::String(config.exit_order_type.as_str().to_owned()),
        ),
        ("amount".to_owned(), number_value(trade.amount)?),
        ("rate".to_owned(), number_value(rate)?),
        ("time_in_force".to_owned(), Value::String("gtc".to_owned())),
        (
            "exit_reason".to_owned(),
            Value::String(exit_reason.to_owned()),
        ),
        (
            "current_time".to_owned(),
            Value::Number(timestamp_ms.into()),
        ),
        (
            "trade_profit_ratio".to_owned(),
            number_value(current_profit_ratio(trade, rate, fee_close(config)))?,
        ),
        ("clear_profit_target".to_owned(), Value::Bool(false)),
        (
            "config.is_futures".to_owned(),
            Value::Bool(config.is_futures),
        ),
    ]);
    let ConfirmControl::Return(value) =
        evaluate_confirm_statements(&program.statements, &mut variables, program, 0)?
    else {
        return None;
    };
    Some((
        value.as_bool()?,
        variables.get("clear_profit_target")?.as_bool()?,
    ))
}

pub(crate) fn evaluate_confirm_statements(
    statements: &[Value],
    variables: &mut BTreeMap<String, Value>,
    program: &ConfirmProgram,
    depth: usize,
) -> Option<ConfirmControl> {
    if depth > 128 {
        return None;
    }
    for statement in statements {
        let object = statement.as_object()?;
        match object.get("op")?.as_str()? {
            "let" => {
                let name = object.get("name")?.as_str()?;
                let value = evaluate_confirm_expression(
                    object.get("value")?,
                    variables,
                    program,
                    depth + 1,
                )?;
                variables.insert(name.to_owned(), value);
            }
            "if" => {
                let condition = evaluate_confirm_expression(
                    object.get("condition")?,
                    variables,
                    program,
                    depth + 1,
                )?
                .as_bool()?;
                let branch = if condition {
                    object.get("then")?
                } else {
                    object.get("otherwise")?
                };
                if let control @ ConfirmControl::Return(_) =
                    evaluate_confirm_statements(branch.as_array()?, variables, program, depth + 1)?
                {
                    return Some(control);
                }
            }
            "return" => {
                return Some(ConfirmControl::Return(evaluate_confirm_expression(
                    object.get("value")?,
                    variables,
                    program,
                    depth + 1,
                )?));
            }
            "log_noop" => {}
            "clear_profit_target" => {
                let pair = evaluate_confirm_expression(
                    object.get("pair")?,
                    variables,
                    program,
                    depth + 1,
                )?;
                pair.as_str()?;
                variables.insert("clear_profit_target".to_owned(), Value::Bool(true));
            }
            _ => return None,
        }
    }
    Some(ConfirmControl::Continue)
}

#[allow(clippy::too_many_lines)]
pub(crate) fn evaluate_confirm_expression(
    expression: &Value,
    variables: &mut BTreeMap<String, Value>,
    program: &ConfirmProgram,
    depth: usize,
) -> Option<Value> {
    if depth > 128 {
        return None;
    }
    let object = expression.as_object()?;
    let op = object.get("op")?.as_str()?;
    match op {
        "literal" => object.get("value").cloned(),
        "variable" => variables.get(object.get("name")?.as_str()?).cloned(),
        "field" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            value
                .as_object()?
                .get(object.get("name")?.as_str()?)
                .cloned()
        }
        "config_value" => variables
            .get(&format!("config.{}", object.get("name")?.as_str()?))
            .cloned(),
        "index" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let index =
                evaluate_confirm_expression(object.get("index")?, variables, program, depth + 1)?;
            if let Some(values) = value.as_array() {
                let raw_index = integer_value(&index)?;
                let length = i64::try_from(values.len()).ok()?;
                let resolved = if raw_index < 0 {
                    length.checked_add(raw_index)?
                } else {
                    raw_index
                };
                values.get(usize::try_from(resolved).ok()?).cloned()
            } else {
                value.as_object()?.get(index.as_str()?).cloned()
            }
        }
        "negative" => number_value(
            -evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?
                .as_f64()?,
        ),
        "not" => Some(Value::Bool(
            !evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?
                .as_bool()?,
        )),
        "add" | "subtract" | "multiply" | "divide" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?
                    .as_f64()?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?
                    .as_f64()?;
            number_value(match op {
                "add" => left + right,
                "subtract" => left - right,
                "multiply" => left * right,
                "divide" if right != 0.0 => left / right,
                _ => return None,
            })
        }
        "and" | "or" => {
            let values = object.get("values")?.as_array()?;
            if op == "and" {
                for value in values {
                    if !evaluate_confirm_expression(value, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        return Some(Value::Bool(false));
                    }
                }
                Some(Value::Bool(true))
            } else {
                for value in values {
                    if evaluate_confirm_expression(value, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        return Some(Value::Bool(true));
                    }
                }
                Some(Value::Bool(false))
            }
        }
        "equal" | "not_equal" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?;
            Some(Value::Bool(if op == "equal" {
                left == right
            } else {
                left != right
            }))
        }
        "greater" | "greater_equal" | "less" | "less_equal" => {
            let left =
                evaluate_confirm_expression(object.get("left")?, variables, program, depth + 1)?
                    .as_f64()?;
            let right =
                evaluate_confirm_expression(object.get("right")?, variables, program, depth + 1)?
                    .as_f64()?;
            Some(Value::Bool(match op {
                "greater" => left > right,
                "greater_equal" => left >= right,
                "less" => left < right,
                "less_equal" => left <= right,
                _ => return None,
            }))
        }
        "contains" => {
            let container = evaluate_confirm_expression(
                object.get("container")?,
                variables,
                program,
                depth + 1,
            )?;
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            Some(Value::Bool(
                container
                    .as_array()
                    .is_some_and(|values| values.contains(&value))
                    || container
                        .as_str()
                        .zip(value.as_str())
                        .is_some_and(|(text, needle)| text.contains(needle)),
            ))
        }
        "all_in" | "any_in" => {
            let items =
                evaluate_confirm_expression(object.get("items")?, variables, program, depth + 1)?;
            let container = evaluate_confirm_expression(
                object.get("container")?,
                variables,
                program,
                depth + 1,
            )?;
            let items = items.as_array()?;
            let container = container.as_array()?;
            Some(Value::Bool(if op == "all_in" {
                items.iter().all(|item| container.contains(item))
            } else {
                items.iter().any(|item| container.contains(item))
            }))
        }
        "length" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let length = value
                .as_array()
                .map(Vec::len)
                .or_else(|| value.as_str().map(str::len))?;
            Some(Value::Number(u64::try_from(length).ok()?.into()))
        }
        "open_trades" => variables.get("open_trades").cloned(),
        "open_trade_count" => {
            let count = variables.get("open_trades")?.as_array()?.len();
            Some(Value::Number(u64::try_from(count).ok()?.into()))
        }
        "analyzed_frame" => variables.get("analyzed_frame").cloned(),
        "trade_profit_ratio" => variables.get("trade_profit_ratio").cloned(),
        "split_words" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            Some(Value::Array(
                value
                    .as_str()?
                    .split_whitespace()
                    .map(|word| Value::String(word.to_owned()))
                    .collect(),
            ))
        }
        "partition" => {
            let value =
                evaluate_confirm_expression(object.get("value")?, variables, program, depth + 1)?;
            let text = value.as_str()?;
            let separator = object.get("separator")?.as_str()?;
            let (before, found, after) = text.find(separator).map_or((text, "", ""), |index| {
                (&text[..index], separator, &text[index + separator.len()..])
            });
            Some(Value::Array(
                [before, found, after]
                    .into_iter()
                    .map(|item| Value::String(item.to_owned()))
                    .collect(),
            ))
        }
        "count" => {
            let iterable = evaluate_confirm_expression(
                object.get("iterable")?,
                variables,
                program,
                depth + 1,
            )?;
            let values = iterable.as_array()?.clone();
            let name = object.get("name")?.as_str()?;
            let filters = object.get("filters")?.as_array()?;
            let previous = variables.get(name).cloned();
            let mut count = 0_u64;
            for value in values {
                variables.insert(name.to_owned(), value);
                let mut accepted = true;
                for filter in filters {
                    if !evaluate_confirm_expression(filter, variables, program, depth + 1)?
                        .as_bool()?
                    {
                        accepted = false;
                        break;
                    }
                }
                count += u64::from(accepted);
            }
            if let Some(previous) = previous {
                variables.insert(name.to_owned(), previous);
            } else {
                variables.remove(name);
            }
            Some(Value::Number(count.into()))
        }
        "call" => {
            let function = program.functions.get(object.get("name")?.as_str()?)?;
            let argument_nodes = object.get("arguments")?.as_array()?;
            if argument_nodes.len() != function.parameters.len() {
                return None;
            }
            let arguments = argument_nodes
                .iter()
                .map(|argument| {
                    evaluate_confirm_expression(argument, variables, program, depth + 1)
                })
                .collect::<Option<Vec<_>>>()?;
            let mut local = variables.clone();
            for (parameter, argument) in function.parameters.iter().zip(arguments) {
                local.insert(parameter.clone(), argument);
            }
            let ConfirmControl::Return(value) =
                evaluate_confirm_statements(&function.statements, &mut local, program, depth + 1)?
            else {
                return None;
            };
            Some(value)
        }
        _ => None,
    }
}
