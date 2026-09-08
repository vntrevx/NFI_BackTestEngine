//! Source-compiled common stops and cached-profit-target decisions.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::callbacks::{
    insert_projected_feature_window, scalar_program_feature_projection, scalar_trade_value,
};
use crate::domain::{ManagedSystemExitPrograms, PairSeries, ScalarDecisionProgram};
use crate::portfolio::OpenTrade;
use crate::scalar_vm::{evaluate_scalar_decision_program, number_value};

use super::exit::{managed_exit_matcher_matches, NfiTargetDecision};
use super::state::{NfiProfitSnapshot, ProfitTarget};

fn variables(
    system: &ManagedSystemExitPrograms,
    mode: &str,
    trade: &OpenTrade,
    snapshot: NfiProfitSnapshot,
    is_futures: bool,
) -> Option<BTreeMap<String, Value>> {
    if trade
        .custom_data
        .get("system_version")
        .and_then(Value::as_str)
        != Some(system.system_version.as_str())
    {
        return None;
    }
    let first = trade.orders.iter().find(|order| order.is_entry)?;
    Some(BTreeMap::from([
        ("mode_name".to_owned(), Value::String(mode.to_owned())),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        ("profit_stake".to_owned(), number_value(snapshot.stake)?),
        ("profit_ratio".to_owned(), number_value(snapshot.ratio)?),
        (
            "profit_init_ratio".to_owned(),
            number_value(snapshot.initial_stake_ratio)?,
        ),
        (
            "profit_current_stake_ratio".to_owned(),
            number_value(snapshot.current_stake_ratio)?,
        ),
        (
            "entry_cost".to_owned(),
            number_value(first.amount * first.price)?,
        ),
        ("is_futures_mode".to_owned(), Value::Bool(is_futures)),
    ]))
}

#[allow(clippy::too_many_arguments)]
pub(super) fn stop(
    system: &ManagedSystemExitPrograms,
    helper: &str,
    mode: &str,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    is_futures: bool,
) -> Option<(bool, Option<String>)> {
    let program = match helper {
        "long_exit_stoploss" => &system.long_stop,
        "short_exit_stoploss" => &system.short_stop,
        _ => return None,
    };
    let mut values = variables(system, mode, trade, snapshot, is_futures)?;
    let value = run(program, &mut values, pair, candle_index)?;
    let fields = value.as_array()?;
    if fields.len() != 2 {
        return None;
    }
    Some((fields[0].as_bool()?, reason(&fields[1])?))
}

#[allow(clippy::too_many_arguments)]
pub(super) fn target(
    system: &ManagedSystemExitPrograms,
    mode: &str,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    previous: &ProfitTarget,
    is_futures: bool,
) -> Option<NfiTargetDecision> {
    let target = &system.profit_target;
    let first = trade.orders.iter().find(|order| order.is_entry)?;
    let is_derisk = derisked(
        system,
        first.amount,
        trade.amount,
        trade
            .orders
            .iter()
            .filter(|order| !order.is_entry)
            .filter_map(|order| order.tag.as_deref()),
    );
    let mut values = variables(system, mode, trade, snapshot, is_futures)?;
    values.extend([
        ("previous_profit".to_owned(), number_value(previous.profit)?),
        (
            "previous_sell_reason".to_owned(),
            Value::String(previous.sell_reason.clone()),
        ),
        ("is_derisk".to_owned(), Value::Bool(is_derisk)),
    ]);
    let tags = trade.entry_tag_words();
    values.extend(target.predicates.iter().map(|(name, matcher)| {
        (
            name.clone(),
            Value::Bool(managed_exit_matcher_matches(matcher, tags, trade.side)),
        )
    }));
    let value = run(&target.program, &mut values, pair, candle_index)?;
    let fields = value.as_array()?;
    if fields.len() != 3 {
        return None;
    }
    let sell = fields[0].as_bool()?;
    let selected_reason = reason(&fields[1])?;
    Some(NfiTargetDecision {
        exit_reason: if sell { selected_reason } else { None },
        remove: fields[2].as_bool()?,
    })
}

pub(super) fn derisked<'a>(
    system: &ManagedSystemExitPrograms,
    first_amount: f64,
    remaining_amount: f64,
    mut exit_tags: impl Iterator<Item = &'a str>,
) -> bool {
    let policy = &system.profit_target.derisk;
    exit_tags.any(|tag| {
        // Source uses partition(" "), not whitespace splitting.
        let mode = tag.split_once(' ').map_or(tag, |(mode, _)| mode);
        policy.order_tags.iter().any(|candidate| candidate == mode)
    }) || remaining_amount < first_amount * policy.amount_ratio
}

#[allow(clippy::option_option)] // Invalid value differs from a valid null reason.
fn reason(value: &Value) -> Option<Option<String>> {
    match value {
        Value::Null => Some(None),
        Value::String(reason) => Some(Some(reason.clone())),
        _ => None,
    }
}

fn run(
    program: &ScalarDecisionProgram,
    variables: &mut BTreeMap<String, Value>,
    pair: &PairSeries,
    candle_index: usize,
) -> Option<Value> {
    let projection = scalar_program_feature_projection(program);
    insert_projected_feature_window(variables, pair, candle_index, &projection)?;
    evaluate_scalar_decision_program(program, variables)
}
