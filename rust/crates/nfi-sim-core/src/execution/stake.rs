//! Bounded custom-stake program evaluation.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::domain::{
    Candle, EntrySignal, PairSeries, StakeExpression, StakeProgram, StakeStatement,
};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{number_value, valid_vm_value};

pub(crate) struct StakeInputs<'a> {
    pub(crate) proposed_stake: f64,
    pub(crate) minimum_stake: f64,
    pub(crate) maximum_stake: f64,
    pub(crate) current_rate: f64,
    pub(crate) leverage: f64,
    pub(crate) entry_tag: Option<&'a str>,
    pub(crate) side: TradeSide,
}

pub(crate) struct EntryStake {
    pub(crate) proposed: f64,
    pub(crate) maximum: f64,
}
pub(crate) struct EntryRequest<'a> {
    pub(crate) pair_index: usize,
    pub(crate) pair: &'a PairSeries,
    pub(crate) candle: &'a Candle,
    pub(crate) side: TradeSide,
    pub(crate) signal: &'a EntrySignal,
    pub(crate) stake: EntryStake,
    pub(crate) open_trades: &'a [OpenTrade],
    pub(crate) id: u64,
    pub(crate) order_id: u64,
}

pub(crate) enum StakeControl {
    Continue,
    Return(Value),
}

pub(crate) fn evaluate_stake_program(
    program: &StakeProgram,
    inputs: &StakeInputs<'_>,
) -> Option<f64> {
    let mut variables = BTreeMap::from([
        (
            "proposed_stake".to_owned(),
            number_value(inputs.proposed_stake)?,
        ),
        ("min_stake".to_owned(), number_value(inputs.minimum_stake)?),
        ("max_stake".to_owned(), number_value(inputs.maximum_stake)?),
        (
            "current_rate".to_owned(),
            number_value(inputs.current_rate)?,
        ),
        ("leverage".to_owned(), number_value(inputs.leverage)?),
        (
            "entry_tag".to_owned(),
            Value::String(inputs.entry_tag?.to_owned()),
        ),
        (
            "side".to_owned(),
            Value::String(
                match inputs.side {
                    TradeSide::Long => "long",
                    TradeSide::Short => "short",
                }
                .to_owned(),
            ),
        ),
    ]);
    let StakeControl::Return(result) =
        evaluate_stake_statements(&program.statements, &mut variables)?
    else {
        return None;
    };
    let stake = result.as_f64()?;
    (stake.is_finite() && stake > 0.0).then_some(stake)
}

pub(crate) fn evaluate_stake_statements(
    statements: &[StakeStatement],
    variables: &mut BTreeMap<String, Value>,
) -> Option<StakeControl> {
    for statement in statements {
        match statement {
            StakeStatement::Let { name, value } => {
                let result = evaluate_stake_expression(value, variables)?;
                variables.insert(name.clone(), result);
            }
            StakeStatement::If {
                condition,
                then,
                otherwise,
            } => {
                let branch = if evaluate_stake_expression(condition, variables)?.as_bool()? {
                    then
                } else {
                    otherwise
                };
                if let control @ StakeControl::Return(_) =
                    evaluate_stake_statements(branch, variables)?
                {
                    return Some(control);
                }
            }
            StakeStatement::For {
                name,
                iterable,
                body,
            } => {
                let values = evaluate_stake_expression(iterable, variables)?
                    .as_array()?
                    .clone();
                for value in values {
                    variables.insert(name.clone(), value);
                    if let control @ StakeControl::Return(_) =
                        evaluate_stake_statements(body, variables)?
                    {
                        return Some(control);
                    }
                }
            }
            StakeStatement::Return { value } => {
                return Some(StakeControl::Return(evaluate_stake_expression(
                    value, variables,
                )?));
            }
        }
    }
    Some(StakeControl::Continue)
}

pub(crate) fn evaluate_stake_expression(
    expression: &StakeExpression,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    match expression {
        StakeExpression::Literal { value } => {
            (value.is_null() || valid_vm_value(value)).then(|| value.clone())
        }
        StakeExpression::Variable { name } => variables.get(name).cloned(),
        StakeExpression::Multiply { left, right } => number_value(
            evaluate_stake_expression(left, variables)?.as_f64()?
                * evaluate_stake_expression(right, variables)?.as_f64()?,
        ),
        StakeExpression::And { values } => {
            for value in values {
                if !evaluate_stake_expression(value, variables)?.as_bool()? {
                    return Some(Value::Bool(false));
                }
            }
            Some(Value::Bool(true))
        }
        StakeExpression::Or { values } => {
            for value in values {
                if evaluate_stake_expression(value, variables)?.as_bool()? {
                    return Some(Value::Bool(true));
                }
            }
            Some(Value::Bool(false))
        }
        StakeExpression::Equal { left, right } => Some(Value::Bool(
            evaluate_stake_expression(left, variables)?
                == evaluate_stake_expression(right, variables)?,
        )),
        StakeExpression::Greater { left, right } => Some(Value::Bool(
            evaluate_stake_expression(left, variables)?.as_f64()?
                > evaluate_stake_expression(right, variables)?.as_f64()?,
        )),
        StakeExpression::Choose {
            condition,
            then,
            otherwise,
        } => {
            if evaluate_stake_expression(condition, variables)?.as_bool()? {
                evaluate_stake_expression(then, variables)
            } else {
                evaluate_stake_expression(otherwise, variables)
            }
        }
        StakeExpression::Index { value, index } => {
            let values = evaluate_stake_expression(value, variables)?;
            let index = evaluate_stake_expression(index, variables)?.as_u64()?;
            values
                .as_array()?
                .get(usize::try_from(index).ok()?)
                .cloned()
        }
        StakeExpression::SplitWords { value } => {
            let value = evaluate_stake_expression(value, variables)?;
            Some(Value::Array(
                value
                    .as_str()?
                    .split_whitespace()
                    .map(|word| Value::String(word.to_owned()))
                    .collect(),
            ))
        }
        StakeExpression::StakeClampMin { multiplier } => {
            let stake = variables.get("proposed_stake")?.as_f64()?
                * evaluate_stake_expression(multiplier, variables)?.as_f64()?;
            let minimum = variables.get("min_stake")?.as_f64()?;
            number_value(if stake > minimum { stake } else { minimum })
        }
        StakeExpression::AllIn { items, container } => {
            let items = evaluate_stake_expression(items, variables)?;
            let container = evaluate_stake_expression(container, variables)?;
            Some(Value::Bool(items.as_array()?.iter().all(|item| {
                container
                    .as_array()
                    .is_some_and(|values| values.contains(item))
            })))
        }
        StakeExpression::AnyIn { items, container } => {
            let items = evaluate_stake_expression(items, variables)?;
            let container = evaluate_stake_expression(container, variables)?;
            Some(Value::Bool(items.as_array()?.iter().any(|item| {
                container
                    .as_array()
                    .is_some_and(|values| values.contains(item))
            })))
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::{json, Value};

    use crate::domain::StakeExpression;

    use super::evaluate_stake_expression;

    #[test]
    fn optional_minimum_identity_is_evaluated_exactly() {
        let expression: StakeExpression = serde_json::from_value(json!({
            "op": "equal",
            "left": {"op": "variable", "name": "min_stake"},
            "right": {"op": "literal", "value": null}
        }))
        .expect("valid optional minimum expression");
        let mut variables = BTreeMap::from([("min_stake".to_owned(), Value::Null)]);

        assert_eq!(
            evaluate_stake_expression(&expression, &variables),
            Some(Value::Bool(true))
        );
        variables.insert("min_stake".to_owned(), json!(10.0));
        assert_eq!(
            evaluate_stake_expression(&expression, &variables),
            Some(Value::Bool(false))
        );
    }
}
