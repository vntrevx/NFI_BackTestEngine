//! Exact entry, fill, adjustment, order replay, and close execution.

use std::collections::BTreeMap;

use num_rational::BigRational;
use num_traits::{ToPrimitive, Zero};
use serde_json::Value;

use super::calculations::{
    available_stake_amount, ceil_step, entry_order_side, entry_sizing, exact_rational,
    exit_order_side, fee_close, fee_open, floor_step, ft_precise_division, precise_product,
    precise_product_quotient, precise_sum, round_eight, round_step,
};
use super::domain::{
    AdjustmentSignal, Candle, ClosedTrade, ConfirmProgram, CustomDataWrite, EntrySignal,
    FilledOrder, PairSeries, PortfolioConfig, SimError, StakeExpression, StakeProgram,
    StakeStatement,
};
use super::portfolio::{wallet_free, OpenTrade, TradeSide};
use super::protections::ProtectionState;
use super::{
    callback_feature_index, entry_leverage, evaluate_custom_exit_bundle, evaluate_nfi_exit,
    integer_value, nfi_entry_signal_is_supported, nfi_profit_snapshot, number_value,
    preserve_partial_exit_funding_refresh, reapply_inclusive_funding_after_entry_fill,
    recalculate_order_funding_total, take_running_funding, update_isolated_liquidation_price,
    valid_vm_value, CustomExitDecision, ProfitTarget,
};

/// Mutable portfolio state shared by every candle entry attempt.
///
/// Freqtrade can call its pair loop twice for one futures candle when an open
/// position closes beside an opposite-direction signal. Both the ordinary
/// entry and that reversal must cross exactly the same slot, lock, wallet,
/// precision, confirmation, and ID boundaries. Keeping those mutations in one
/// executor prevents the two paths from drifting as the entry contract grows.
pub(super) struct EntryExecution<'input, 'state> {
    pub(super) config: &'input PortfolioConfig,
    pub(super) protection_state: &'input ProtectionState,
    pub(super) closed_trades: &'input [ClosedTrade],
    pub(super) open_trades: &'state mut Vec<OpenTrade>,
    pub(super) available_balance: &'state mut f64,
    pub(super) rejected_signals: &'state mut u64,
    pub(super) next_trade_id: &'state mut u64,
    pub(super) next_order_id: &'state mut u64,
    pub(super) maximum_concurrent_trades: &'state mut usize,
}

impl EntryExecution<'_, '_> {
    pub(super) fn try_open(
        &mut self,
        pair_index: usize,
        pair: &PairSeries,
        candle: &Candle,
        side: TradeSide,
        signal: &EntrySignal,
    ) -> Result<bool, SimError> {
        if self
            .protection_state
            .is_pair_locked(&pair.pair, candle.timestamp_ms, side)
        {
            return Ok(false);
        }
        if self.open_trades.len() >= self.config.max_open_trades {
            *self.rejected_signals += 1;
            return Ok(false);
        }
        if self
            .config
            .nfi_x7_trade_manager
            .as_ref()
            .is_some_and(|manager| !nfi_entry_signal_is_supported(manager, side, signal))
        {
            return Err(SimError::UnsupportedNfiEntryTag {
                pair: pair.pair.clone(),
                entry_tag: signal.tag.clone().unwrap_or_else(|| "<none>".to_owned()),
            });
        }

        let tied_up_stake = self
            .open_trades
            .iter()
            .map(|trade| trade.stake_amount)
            .sum::<f64>();
        let stake_available = available_stake_amount(
            *self.available_balance,
            tied_up_stake,
            self.config.tradable_balance_ratio,
        );
        let proposed_stake = if self.config.unlimited_stake {
            let slot_divisor = f64::from(
                u32::try_from(self.config.max_open_trades)
                    .expect("validated max_open_trades fits u32"),
            );
            ((stake_available + tied_up_stake) / slot_divisor).min(stake_available)
        } else {
            self.config.stake_amount.min(stake_available)
        };
        let attempt = attempt_entry(
            EntryRequest {
                pair_index,
                pair,
                candle,
                side,
                signal,
                stake: EntryStake {
                    proposed: proposed_stake,
                    maximum: stake_available,
                },
                open_trades: self.open_trades,
                id: *self.next_trade_id,
                order_id: *self.next_order_id,
            },
            self.config,
        )?;
        if attempt.order_id_consumed {
            // Freqtrade allocates the order ID before amount precision and
            // confirm_trade_entry. A rejection therefore leaves a deliberate
            // gap which NFI can later expose inside grind exit tags.
            *self.next_order_id += 1;
        }
        let Some(trade) = attempt.trade else {
            return Ok(false);
        };

        *self.next_trade_id += 1;
        self.open_trades.push(trade);
        *self.maximum_concurrent_trades =
            (*self.maximum_concurrent_trades).max(self.open_trades.len());
        *self.available_balance = wallet_free(
            self.config.starting_balance,
            self.open_trades,
            self.closed_trades,
        );
        Ok(true)
    }
}

/// Apply Freqtrade's `check_for_trade_entry()` signal arbitration.
///
/// A same-side exit suppresses its entry. In futures mode, simultaneous long
/// and short entries suppress both instead of assigning priority to either
/// side. Spot ignores short columns before performing the long-side check.
#[cfg(test)]
pub(super) fn enter_trade(
    request: EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<Option<OpenTrade>, SimError> {
    attempt_entry(request, config).map(|attempt| attempt.trade)
}

pub(super) struct EntryAttempt {
    pub(super) trade: Option<OpenTrade>,
    pub(super) order_id_consumed: bool,
}

pub(super) fn attempt_entry(
    request: EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<EntryAttempt, SimError> {
    let leverage = entry_leverage(
        request.signal,
        config,
        request.pair,
        request.candle,
        request.stake.proposed,
    )?;
    let requested = requested_entry_stake(&request, config, leverage)?;
    let EntryRequest {
        pair_index,
        pair,
        candle,
        side,
        signal,
        stake: _,
        open_trades: _,
        id,
        order_id,
    } = request;
    let Some((amount, stake, precise_cost, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        pair.amount_step.unwrap_or(config.amount_step),
        leverage,
    ) else {
        return Ok(EntryAttempt {
            trade: None,
            order_id_consumed: true,
        });
    };
    if !entry_is_confirmed(&request, config, amount)? {
        return Ok(EntryAttempt {
            trade: None,
            order_id_consumed: true,
        });
    }
    let tag = signal.tag.clone();
    let order = FilledOrder {
        id: order_id,
        funding_fee: 0.0,
        sequence: 0,
        side: entry_order_side(side),
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: tag.clone(),
    };
    let amount_step = pair.amount_step.unwrap_or(config.amount_step);
    let price_step = pair_price_step(pair, candle, config.price_step);
    let stop_loss = initial_stop_loss(
        side,
        candle.open,
        config.stoploss_ratio,
        leverage,
        price_step,
    );
    let mut trade = OpenTrade {
        id,
        pair_index,
        pair: pair.pair.clone(),
        side,
        leverage,
        amount_step,
        price_step,
        open_timestamp_ms: candle.timestamp_ms,
        open_rate: candle.open,
        amount,
        stake_amount: stake,
        max_stake_amount: stake,
        entry_cost_with_fees: precise_cost,
        first_entry_cost_with_fees: precise_cost,
        adjustment_count: 0,
        entry_tag: tag,
        funding_fees: 0.0,
        funding_fees_total: 0.0,
        funding_sum_high: 0.0,
        funding_sum_low: 0.0,
        funding_rebase_seed: None,
        realized_partial_profit: 0.0,
        liquidation_price: signal.liquidation_price,
        liquidation_price_is_explicit: signal.liquidation_price.is_some(),
        initial_stop_loss: stop_loss,
        stop_loss,
        minimum_rate: candle.low,
        maximum_rate: candle.high,
        orders: vec![order],
        custom_data: BTreeMap::new(),
        nfi_adjustment_state: None,
    };
    reapply_inclusive_funding_after_entry_fill(&mut trade, candle, config.funding_fee_interval_ms);
    apply_order_filled(&mut trade, signal.tag.as_deref(), config);
    update_isolated_liquidation_price(&mut trade, config, candle.timestamp_ms)?;
    Ok(EntryAttempt {
        trade: Some(trade),
        order_id_consumed: true,
    })
}

pub(super) fn requested_entry_stake(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    leverage: f64,
) -> Result<f64, SimError> {
    let Some(program) = &config.stake_program else {
        return Ok(request.stake.proposed);
    };
    evaluate_stake_program(
        program,
        &StakeInputs {
            proposed_stake: request.stake.proposed,
            minimum_stake: minimum_pair_stake(
                request.pair,
                request.candle.open,
                config.stoploss_ratio,
                leverage,
                config.amount_reserve_percent,
            ),
            maximum_stake: request.stake.maximum,
            current_rate: request.candle.open,
            leverage,
            entry_tag: request.signal.tag.as_deref(),
            side: request.side,
        },
    )
    .ok_or_else(|| SimError::InvalidStakeProgram {
        pair: request.pair.pair.clone(),
        timestamp_ms: request.candle.timestamp_ms,
    })
    .map(|stake| stake.min(request.stake.maximum))
}

pub(super) fn initial_stop_loss(
    side: TradeSide,
    open_rate: f64,
    stoploss_ratio: f64,
    leverage: f64,
    price_step: f64,
) -> f64 {
    let leveraged_stoploss = stoploss_ratio / leverage;
    match side {
        TradeSide::Long => ceil_step(open_rate * (1.0 + leveraged_stoploss), price_step),
        TradeSide::Short => floor_step(open_rate * (1.0 - leveraged_stoploss), price_step),
    }
}

pub(super) fn pair_price_step(pair: &PairSeries, candle: &Candle, default: f64) -> f64 {
    let changes_before_or_at_candle = pair
        .price_steps
        .partition_point(|change| change.timestamp_ms <= candle.timestamp_ms);
    changes_before_or_at_candle
        .checked_sub(1)
        .and_then(|index| pair.price_steps.get(index))
        .map_or_else(|| pair.price_step.unwrap_or(default), |change| change.step)
}

pub(super) fn entry_is_confirmed(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    amount: f64,
) -> Result<bool, SimError> {
    let Some(program) = &config.entry_confirmation_program else {
        return Ok(true);
    };
    evaluate_confirm_program(
        program,
        ConfirmInputs {
            pair: &request.pair.pair,
            timestamp_ms: request.candle.timestamp_ms,
            amount,
            rate: request.candle.open,
            entry_tag: request.signal.tag.as_deref(),
            side: request.side,
            previous_close: request.candle.previous_close,
            open_trades: request.open_trades,
            max_open_trades: config.max_open_trades,
            is_futures: config.is_futures,
        },
    )
    .ok_or_else(|| SimError::InvalidEntryConfirmation {
        pair: request.pair.pair.clone(),
        timestamp_ms: request.candle.timestamp_ms,
    })
}

pub(super) fn minimum_pair_stake(
    pair: &PairSeries,
    rate: f64,
    stoploss_ratio: f64,
    leverage: f64,
    reserve_percent: f64,
) -> f64 {
    if let Some(stake) = pair.minimum_stake {
        return stake;
    }
    let margin_reserve = 1.0 + reserve_percent;
    let denominator = 1.0 - stoploss_ratio.abs();
    let stoploss_reserve = if denominator > 0.0 {
        (margin_reserve / denominator).clamp(1.0, 1.5)
    } else {
        1.5
    };
    let cost_stake = pair
        .minimum_cost
        .map_or(0.0, |cost| cost * stoploss_reserve);
    let amount_stake = pair
        .minimum_amount
        .map_or(0.0, |amount| amount * rate * margin_reserve);
    cost_stake.max(amount_stake) / leverage
}

/// Return the minimum stake exposed to `adjust_trade_position`.
///
/// Freqtrade's backtester asks the exchange for this value with a fixed
/// `-10%` stop-loss reserve and does not pass the trade leverage. Entry-order
/// validation is different: it passes leverage explicitly. Keeping this
/// distinction in one helper prevents the generic callback path and the
/// optimized NFI managers from drifting apart.
pub(super) fn adjustment_minimum_pair_stake(
    pair: &PairSeries,
    rate: f64,
    reserve_percent: f64,
) -> f64 {
    minimum_pair_stake(pair, rate, -0.1, 1.0, reserve_percent)
}

pub(super) fn apply_order_filled(
    trade: &mut OpenTrade,
    order_tag: Option<&str>,
    config: &PortfolioConfig,
) {
    let Some(program) = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
    else {
        return;
    };
    let successful_entries = trade.orders.iter().filter(|order| order.is_entry).count();
    if successful_entries == 1 {
        apply_custom_writes(
            &mut trade.custom_data,
            &program.initial_successful_entry_writes,
        );
    }
    let Some(mode) = order_tag.and_then(|tag| tag.split(' ').next()) else {
        return;
    };
    if let Some(writes) = program.order_tag_actions.get(mode) {
        apply_custom_writes(&mut trade.custom_data, writes);
    }
}

pub(super) fn apply_custom_writes(
    custom_data: &mut BTreeMap<String, Value>,
    writes: &[CustomDataWrite],
) {
    for write in writes {
        custom_data.insert(write.key.clone(), write.value.clone());
    }
}

pub(super) struct StakeInputs<'a> {
    pub(super) proposed_stake: f64,
    pub(super) minimum_stake: f64,
    pub(super) maximum_stake: f64,
    pub(super) current_rate: f64,
    pub(super) leverage: f64,
    pub(super) entry_tag: Option<&'a str>,
    pub(super) side: TradeSide,
}

#[derive(Clone, Copy)]
pub(super) struct EntryStake {
    pub(super) proposed: f64,
    pub(super) maximum: f64,
}

#[derive(Clone, Copy)]
pub(super) struct EntryRequest<'a> {
    pub(super) pair_index: usize,
    pub(super) pair: &'a PairSeries,
    pub(super) candle: &'a Candle,
    pub(super) side: TradeSide,
    pub(super) signal: &'a EntrySignal,
    pub(super) stake: EntryStake,
    pub(super) open_trades: &'a [OpenTrade],
    pub(super) id: u64,
    pub(super) order_id: u64,
}

pub(super) enum StakeControl {
    Continue,
    Return(Value),
}

pub(super) fn evaluate_stake_program(
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

pub(super) fn evaluate_stake_statements(
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

pub(super) fn evaluate_stake_expression(
    expression: &StakeExpression,
    variables: &BTreeMap<String, Value>,
) -> Option<Value> {
    match expression {
        StakeExpression::Literal { value } => valid_vm_value(value).then(|| value.clone()),
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

#[derive(Clone, Copy)]
pub(super) struct ConfirmInputs<'a> {
    pub(super) pair: &'a str,
    pub(super) timestamp_ms: i64,
    pub(super) amount: f64,
    pub(super) rate: f64,
    pub(super) entry_tag: Option<&'a str>,
    pub(super) side: TradeSide,
    pub(super) previous_close: Option<f64>,
    pub(super) open_trades: &'a [OpenTrade],
    pub(super) max_open_trades: usize,
    pub(super) is_futures: bool,
}

pub(super) enum ConfirmControl {
    Continue,
    Return(Value),
}

pub(super) fn evaluate_confirm_program(
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
        ("order_type".to_owned(), Value::String("limit".to_owned())),
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

pub(super) fn evaluate_exit_confirm_program(
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
        ("order_type".to_owned(), Value::String("limit".to_owned())),
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

pub(super) fn evaluate_confirm_statements(
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
pub(super) fn evaluate_confirm_expression(
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

pub(super) fn update_extrema(trade: &mut OpenTrade, candle: &Candle) {
    trade.minimum_rate = trade.minimum_rate.min(candle.low);
    trade.maximum_rate = trade.maximum_rate.max(candle.high);
}

pub(super) fn apply_adjustment(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    available_balance: f64,
    order_id: u64,
) -> Result<(), SimError> {
    if !adjustment.stake_amount.is_finite() || adjustment.stake_amount == 0.0 {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    if adjustment.stake_amount < 0.0 {
        return apply_partial_exit(trade, candle, adjustment, config, order_id);
    }
    let requested = adjustment.stake_amount.min(available_balance);
    let Some((amount, _, _, order_cost)) = entry_sizing(
        requested,
        candle.open,
        fee_open(config),
        trade.amount_step,
        trade.leverage,
    ) else {
        return Ok(());
    };
    let funding_fee = take_running_funding(trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: entry_order_side(trade.side),
        is_entry: true,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: candle.open,
        cost: order_cost,
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    // Freqtrade does not update these fields incrementally. Its
    // `LocalTrade.recalc_trade_from_orders()` replays every filled order after
    // each adjustment. Replaying here preserves weighted-basis exits and the
    // all-time entry stake even after a cluster has been sold.
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    reapply_inclusive_funding_after_entry_fill(trade, candle, config.funding_fee_interval_ms);
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config);
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    Ok(())
}

pub(super) fn apply_partial_exit(
    trade: &mut OpenTrade,
    candle: &Candle,
    adjustment: &AdjustmentSignal,
    config: &PortfolioConfig,
    order_id: u64,
) -> Result<(), SimError> {
    let amount_before_fill = trade.amount;
    let requested_stake = -adjustment.stake_amount;
    if requested_stake >= trade.stake_amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    // Freqtrade performs this multiplication with `FtPrecise` before amount
    // precision is applied. A mathematically exact 0.46 can therefore become
    // 0.459999... and correctly truncate to 0.45 on a 0.01 market step.
    let raw_amount = precise_product_quotient(requested_stake, trade.amount, trade.stake_amount)
        .ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?;
    let amount = floor_step(raw_amount, trade.amount_step);
    if amount <= 0.0 || amount >= trade.amount {
        return Err(SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        });
    }
    // Freqtrade freezes price precision on the trade when it opens and runs
    // every later exit through price_to_precision. This remains observable
    // after an exchange changes the pair tick size during a long-lived NFI
    // position: callback arithmetic uses the raw candle open, while the
    // resulting partial-exit order is filled at the frozen rounded price.
    let exit_rate = round_step(candle.open, trade.price_step);
    let funding_fee = take_running_funding(trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: candle.timestamp_ms,
        amount,
        price: exit_rate,
        cost: amount * exit_rate * (1.0 + fee_close(config)),
        tag: Some(adjustment.tag.clone()),
    });
    recalculate_order_funding_total(trade);
    // Pinned Freqtrade refreshes isolated liquidation inside
    // `_try_close_open_order()`, before `_process_exit_order()` replays the
    // partial exit into LocalTrade. The resulting one-adjustment lag is
    // observable when a second derisk changes the Binance maintenance tier.
    update_isolated_liquidation_price(trade, config, candle.timestamp_ms)?;
    recalculate_open_trade_from_orders(trade, config).ok_or_else(|| {
        SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        }
    })?;
    preserve_partial_exit_funding_refresh(trade, candle, amount_before_fill);
    trade.realized_partial_profit = if is_unleveraged_spot(trade, config) {
        replay_spot_profit(trade, config)
            .map(|replay| replay.profit_abs)
            .ok_or_else(|| SimError::InvalidAdjustment {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?
    } else {
        replay_leveraged_profit(trade, config).ok_or_else(|| SimError::InvalidAdjustment {
            pair: trade.pair.clone(),
            timestamp_ms: candle.timestamp_ms,
        })?
    };
    trade.adjustment_count += 1;
    apply_order_filled(trade, Some(&adjustment.tag), config);
    Ok(())
}

/// Rebuild Freqtrade's order-derived open-position fields.
///
/// Exit orders remove stake at the weighted entry price, not at their fill
/// price. `max_stake_amount` is the sum of every successful entry and never
/// shrinks after partial exits. Decimal replay also prevents accumulated
/// binary-float drift across the hundreds of X7 grind orders.
pub(super) fn recalculate_open_trade_from_orders(
    trade: &mut OpenTrade,
    config: &PortfolioConfig,
) -> Option<()> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut maximum_stake = BigRational::zero();
    let mut average_price = BigRational::zero();

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &price * &amount;
            maximum_stake += &price * &amount;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
        } else {
            current_amount -= &amount;
            current_stake -= &average_price * &amount;
        }
    }
    if current_amount <= BigRational::zero() || current_stake <= BigRational::zero() {
        return None;
    }

    let raw_amount = current_amount.to_f64()?;
    let raw_stake = current_stake.to_f64()?;
    trade.amount = floor_step(raw_amount, trade.amount_step);
    trade.stake_amount = raw_stake / trade.leverage;
    trade.max_stake_amount = maximum_stake.to_f64()? / trade.leverage;
    trade.open_rate = round_step(
        (&current_stake / &current_amount).to_f64()?,
        trade.price_step,
    );
    let leveraged_stoploss = config.stoploss_ratio / trade.leverage;
    let adjusted_stop = match trade.side {
        TradeSide::Long => ceil_step(
            trade.open_rate * (1.0 + leveraged_stoploss),
            trade.price_step,
        ),
        TradeSide::Short => floor_step(
            trade.open_rate * (1.0 - leveraged_stoploss),
            trade.price_step,
        ),
    };
    trade.stop_loss = match trade.side {
        // `adjust_stop_loss()` is monotonic: position adjustment may protect
        // more profit, but it must never loosen an already established stop.
        TradeSide::Long => trade.stop_loss.max(adjusted_stop),
        TradeSide::Short => trade.stop_loss.min(adjusted_stop),
    };

    let notional = precise_product(&[trade.amount, trade.open_rate])?;
    trade.entry_cost_with_fees = if (trade.leverage - 1.0).abs() < f64::EPSILON {
        precise_product(&[trade.amount, trade.open_rate, 1.0 + fee_open(config)])?
    } else {
        let entry_fee = precise_product(&[notional, fee_open(config)])?;
        precise_sum(&[trade.stake_amount, entry_fee])?
    };
    Some(())
}

pub(super) struct ProfitReplay {
    pub(super) profit_abs: f64,
    pub(super) total_entry_value: f64,
}

pub(super) fn is_unleveraged_spot(trade: &OpenTrade, config: &PortfolioConfig) -> bool {
    !config.is_futures && (trade.leverage - 1.0).abs() < f64::EPSILON
}

/// Replay Freqtrade's spot `recalc_trade_from_orders()` profit path.
///
/// Each partial exit is valued against the weighted entry price at that point
/// and rounded to eight decimals before it is added to cumulative profit.
/// The denominator includes entry fees for every buy, matching
/// `LocalTrade.close_profit` rather than the fee-free `max_stake_amount`.
pub(super) fn replay_spot_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<ProfitReplay> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut total_entry_value = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            total_entry_value +=
                precise_product(&[order.amount, order.price, 1.0 + fee_open(config)])?;
            continue;
        }

        if amount > current_amount {
            return None;
        }
        let open_value = precise_product(&[
            order.amount,
            average_price.to_f64()?,
            1.0 + fee_open(config),
        ])?;
        let close_value = precise_product(&[order.amount, order.price, 1.0 - fee_close(config)])?;
        let exit_profit = if trade.side == TradeSide::Long {
            close_value - open_value
        } else {
            open_value - close_value
        };
        profit_abs += round_eight(exit_profit);
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }

    Some(ProfitReplay {
        profit_abs,
        total_entry_value,
    })
}

/// Replay Freqtrade's leveraged/futures realized-profit calculation.
///
/// Freqtrade stores the full running funding amount on the next filled order.
/// During order replay it accumulates those values until an exit, includes the
/// accumulated funding in that exit's profit, rounds the exit profit to eight
/// decimals, and then resets the funding accumulator. This differs materially
/// from prorating funding by the partial-exit amount.
pub(super) fn replay_leveraged_profit(trade: &OpenTrade, config: &PortfolioConfig) -> Option<f64> {
    let mut current_amount = BigRational::zero();
    let mut current_stake = BigRational::zero();
    let mut average_price = BigRational::zero();
    let mut current_funding = 0.0;
    let mut profit_abs = 0.0;

    for order in &trade.orders {
        current_funding += order.funding_fee;
        let amount = exact_rational(order.amount)?;
        let price = exact_rational(order.price)?;
        if amount <= BigRational::zero() || price <= BigRational::zero() {
            return None;
        }
        if order.is_entry {
            current_amount += &amount;
            current_stake += &amount * &price;
            average_price = ft_precise_division(&current_stake, &current_amount)?;
            continue;
        }
        if amount > current_amount {
            return None;
        }

        let average = average_price.to_f64()?;
        let open_multiplier = if trade.side == TradeSide::Short {
            1.0 - fee_open(config)
        } else {
            1.0 + fee_open(config)
        };
        let close_multiplier = if trade.side == TradeSide::Short {
            1.0 + fee_close(config)
        } else {
            1.0 - fee_close(config)
        };
        let open_value = precise_product(&[order.amount, average, open_multiplier])?;
        let close_value = precise_product(&[order.amount, order.price, close_multiplier])?;
        let exit_profit = if trade.side == TradeSide::Short {
            open_value - close_value + current_funding
        } else {
            close_value - open_value + current_funding
        };
        profit_abs += round_eight(exit_profit);
        current_funding = 0.0;
        current_amount -= &amount;
        current_stake -= &average_price * &amount;
    }
    Some(profit_abs)
}

pub(super) fn freqtrade_total_entry_value(
    trade: &OpenTrade,
    config: &PortfolioConfig,
) -> Option<f64> {
    let open_multiplier = if trade.side == TradeSide::Short {
        1.0 - fee_open(config)
    } else {
        1.0 + fee_open(config)
    };
    trade
        .orders
        .iter()
        .filter(|order| order.is_entry)
        .try_fold(0.0, |total, order| {
            precise_product(&[order.amount, order.price, open_multiplier])
                .map(|entry_value| total + entry_value)
        })
}

pub(super) fn rule_adjustment(
    trade: &OpenTrade,
    candle: &Candle,
    config: &PortfolioConfig,
) -> Option<AdjustmentSignal> {
    let rule = config.adjustment_rule.as_ref()?;
    if trade.adjustment_count >= rule.max_adjustments {
        return None;
    }
    let current_profit = current_profit_ratio(trade, candle.open, fee_close(config));
    (current_profit < rule.profit_below).then(|| AdjustmentSignal {
        stake_amount: trade.first_entry_cost_with_fees * rule.stake_ratio,
        tag: rule.tag.clone(),
    })
}

pub(super) struct ExitDecision {
    pub(super) rate: f64,
    pub(super) reason: String,
    pub(super) requires_confirmation: bool,
}

pub(super) fn exit_decision(
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Result<Option<ExitDecision>, SimError> {
    // This order mirrors Freqtrade 2026.5.1 `IStrategy.should_exit`.
    // Strategy exits precede liquidation and stop-loss candidates, so a
    // same-candle collision keeps the strategy reason and candle-open rate.
    let signal = match trade.side {
        TradeSide::Long => &candle.exit_long,
        TradeSide::Short => &candle.exit_short,
    };
    if let Some(signal) = signal {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: signal.reason.clone(),
            requires_confirmation: true,
        }));
    }
    if let Some(manager) = &config.nfi_x7_trade_manager {
        let feature_index =
            callback_feature_index(candle_index).ok_or(SimError::InvalidNfiTradeManager)?;
        let decision = evaluate_nfi_exit(
            manager,
            trade,
            pair,
            feature_index,
            candle,
            config,
            profit_targets,
        )
        .ok_or(SimError::InvalidNfiTradeManager)?;
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
        }
    }
    if let Some(bundle) = &config.custom_exit_program {
        let feature_index =
            callback_feature_index(candle_index).ok_or_else(|| SimError::InvalidCustomExit {
                pair: trade.pair.clone(),
                timestamp_ms: candle.timestamp_ms,
            })?;
        let decision =
            evaluate_custom_exit_bundle(bundle, trade, pair, feature_index, candle, config)
                .ok_or_else(|| SimError::InvalidCustomExit {
                    pair: trade.pair.clone(),
                    timestamp_ms: candle.timestamp_ms,
                })?;
        if let CustomExitDecision::Exit(reason) = decision {
            return Ok(Some(ExitDecision {
                rate: candle.open,
                reason,
                requires_confirmation: true,
            }));
        }
    }
    if config
        .custom_exit_after_ms
        .is_some_and(|duration| candle.timestamp_ms - trade.open_timestamp_ms >= duration)
    {
        return Ok(Some(ExitDecision {
            rate: candle.open,
            reason: "contract_timed_exit".to_owned(),
            requires_confirmation: true,
        }));
    }
    // Freqtrade calculates stop-loss and liquidation collisions inside
    // `IStrategy.ft_stoploss_reached()`. A regular stop-loss wins that
    // collision and is the only candidate returned to the backtester. This is
    // observable when `confirm_trade_exit()` rejects the stop-loss: Freqtrade
    // does not then fall through to a same-candle liquidation candidate.
    let stopped = match trade.side {
        TradeSide::Long => candle.low <= trade.stop_loss,
        TradeSide::Short => candle.high >= trade.stop_loss,
    };
    if stopped {
        let trailing = match trade.side {
            TradeSide::Long => trade.stop_loss > trade.initial_stop_loss,
            TradeSide::Short => trade.stop_loss < trade.initial_stop_loss,
        };
        return Ok(Some(ExitDecision {
            rate: stop_or_liquidation_exit_rate(trade, candle, trade.stop_loss),
            reason: if trailing {
                "trailing_stop_loss".to_owned()
            } else {
                "stop_loss".to_owned()
            },
            requires_confirmation: true,
        }));
    }
    if let Some(liquidation_price) = trade.liquidation_price {
        let liquidated = match trade.side {
            TradeSide::Long => candle.low <= liquidation_price,
            TradeSide::Short => candle.high >= liquidation_price,
        };
        if liquidated {
            return Ok(Some(ExitDecision {
                rate: stop_or_liquidation_exit_rate(trade, candle, liquidation_price),
                reason: "liquidation".to_owned(),
                requires_confirmation: false,
            }));
        }
    }
    Ok(None)
}

pub(super) fn stop_or_liquidation_exit_rate(
    trade: &OpenTrade,
    candle: &Candle,
    threshold: f64,
) -> f64 {
    // Freqtrade exits at the candle open when a previously retained stop or
    // liquidation threshold lies beyond the complete candle range. This is
    // observable after confirm_trade_exit rejected earlier stop candidates.
    let crossed_before_open = match trade.side {
        TradeSide::Long => threshold > candle.high,
        TradeSide::Short => threshold < candle.low,
    };
    if crossed_before_open {
        candle.open
    } else {
        threshold
    }
}

pub(super) fn current_profit_ratio(trade: &OpenTrade, rate: f64, close_fee_rate: f64) -> f64 {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let hypothetical_proceeds = trade.amount * rate * (1.0 - close_fee_rate);
        return (hypothetical_proceeds - trade.entry_cost_with_fees + trade.funding_fees_total)
            / trade.entry_cost_with_fees;
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit = gross_profit - entry_fees - close_fees + trade.funding_fees_total;
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - (entry_fees / (trade.amount * trade.open_rate))
    } else {
        1.0 + (entry_fees / (trade.amount * trade.open_rate))
    };
    profit / (trade.stake_amount * open_fee_multiplier)
}

pub(super) fn close_trade(
    mut trade: OpenTrade,
    timestamp_ms: i64,
    rate: f64,
    reason: String,
    config: &PortfolioConfig,
    sequence: usize,
    order_id: u64,
) -> (ClosedTrade, f64) {
    // Backtest exit orders use the price precision captured when the trade
    // opened, not a later market-snapshot precision.
    let rate = round_step(rate, trade.price_step);
    let gross_proceeds = trade.amount * rate;
    let open_fee_rate = fee_open(config);
    let close_fee_rate = fee_close(config);
    let (fallback_remaining_profit, fallback_remaining_profit_ratio) =
        fallback_close_profit(&trade, rate, open_fee_rate, close_fee_rate, gross_proceeds);
    let funding_fee = take_running_funding(&mut trade);
    trade.orders.push(FilledOrder {
        id: order_id,
        funding_fee,
        sequence: trade.orders.len(),
        side: exit_order_side(trade.side),
        is_entry: false,
        filled_timestamp_ms: timestamp_ms,
        amount: trade.amount,
        price: rate,
        cost: gross_proceeds * (1.0 + close_fee_rate),
        tag: Some(reason.clone()),
    });
    recalculate_order_funding_total(&mut trade);
    let (profit_abs, fallback_profit_ratio) = replay_closed_profit(
        &trade,
        config,
        open_fee_rate,
        fallback_remaining_profit,
        fallback_remaining_profit_ratio,
    );
    let profit_ratio =
        freqtrade_total_entry_value(&trade, config).map_or(fallback_profit_ratio, |total_stake| {
            if total_stake == 0.0 {
                0.0
            } else {
                (profit_abs / total_stake) * trade.leverage
            }
        });
    let wallet_proceeds = trade.stake_amount + profit_abs;
    (
        ClosedTrade {
            sequence,
            id: trade.id,
            pair: trade.pair,
            is_short: trade.side == TradeSide::Short,
            leverage: trade.leverage,
            open_timestamp_ms: trade.open_timestamp_ms,
            close_timestamp_ms: timestamp_ms,
            open_rate: trade.open_rate,
            close_rate: rate,
            amount: trade.amount,
            stake_amount: trade.stake_amount,
            max_stake_amount: trade.max_stake_amount,
            entry_tag: trade.entry_tag,
            exit_reason: reason,
            fee_open: open_fee_rate,
            fee_close: close_fee_rate,
            funding_fees: trade.funding_fees_total,
            liquidation_price: trade.liquidation_price,
            profit_abs,
            profit_ratio,
            initial_stop_loss: trade.initial_stop_loss,
            stop_loss: trade.stop_loss,
            minimum_rate: trade.minimum_rate,
            maximum_rate: trade.maximum_rate,
            orders: trade.orders,
        },
        wallet_proceeds,
    )
}

pub(super) fn fallback_close_profit(
    trade: &OpenTrade,
    rate: f64,
    open_fee_rate: f64,
    close_fee_rate: f64,
    gross_proceeds: f64,
) -> (f64, f64) {
    if trade.side == TradeSide::Long && (trade.leverage - 1.0).abs() < f64::EPSILON {
        let proceeds =
            precise_product(&[trade.amount, rate, 1.0 - close_fee_rate]).unwrap_or(gross_proceeds);
        let profit_abs = round_eight(proceeds - trade.entry_cost_with_fees + trade.funding_fees);
        return (profit_abs, profit_abs / trade.entry_cost_with_fees);
    }
    let direction = if trade.side == TradeSide::Long {
        1.0
    } else {
        -1.0
    };
    let gross_profit = trade.amount * (rate - trade.open_rate) * direction;
    let entry_fees = trade.entry_cost_with_fees - trade.stake_amount;
    let close_fees = trade.amount * rate * close_fee_rate;
    let profit_abs = round_eight(gross_profit - entry_fees - close_fees + trade.funding_fees);
    let open_fee_multiplier = if trade.side == TradeSide::Short {
        1.0 - open_fee_rate
    } else {
        1.0 + open_fee_rate
    };
    (
        profit_abs,
        profit_abs / (trade.stake_amount * open_fee_multiplier),
    )
}

pub(super) fn replay_closed_profit(
    trade: &OpenTrade,
    config: &PortfolioConfig,
    open_fee_rate: f64,
    fallback_remaining_profit: f64,
    fallback_remaining_profit_ratio: f64,
) -> (f64, f64) {
    if is_unleveraged_spot(trade, config) {
        return replay_spot_profit(trade, config).map_or_else(
            || {
                let profit_abs =
                    round_eight(trade.realized_partial_profit + fallback_remaining_profit);
                (profit_abs, fallback_remaining_profit_ratio)
            },
            |replay| {
                let ratio = if replay.total_entry_value == 0.0 {
                    0.0
                } else {
                    replay.profit_abs / replay.total_entry_value
                };
                (replay.profit_abs, ratio)
            },
        );
    }
    if config.is_futures || trade.adjustment_count > 0 {
        // Futures uses LocalTrade.calculate_profit even for a single-entry
        // position. That path converts FtPrecise open/close values back to
        // floats before applying Python's eight-decimal formatting. The
        // algebraically equivalent gross-profit shortcut can miss a half-way
        // decimal by one unit at variable leverage.
        let profit_abs = replay_leveraged_profit(trade, config).unwrap_or_else(|| {
            round_eight(trade.realized_partial_profit + fallback_remaining_profit)
        });
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        return (
            profit_abs,
            profit_abs / (trade.max_stake_amount * open_fee_multiplier),
        );
    }
    let profit_abs = round_eight(trade.realized_partial_profit + fallback_remaining_profit);
    let profit_ratio = if trade.realized_partial_profit == 0.0 {
        fallback_remaining_profit_ratio
    } else {
        let open_fee_multiplier = if trade.side == TradeSide::Short {
            1.0 - open_fee_rate
        } else {
            1.0 + open_fee_rate
        };
        profit_abs / (trade.max_stake_amount * open_fee_multiplier)
    };
    (profit_abs, profit_ratio)
}
