//! Entry arbitration, sizing, order creation, and order-filled effects.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::{
    available_stake_amount, ceil_step, entry_order_side, entry_sizing, fee_open, floor_step,
};
use crate::domain::{
    Candle, ClosedTrade, CustomDataWrite, EntrySignal, FilledOrder, PairSeries, PortfolioConfig,
    SimError, StateMachineActionKind, StateMachineReadSource,
};
use crate::futures::{
    entry_leverage, reapply_inclusive_funding_after_entry_fill, update_isolated_liquidation_price,
};
use crate::portfolio::{wallet_free, OpenTrade, TradeSide};
use crate::protections::ProtectionState;
use crate::validation::nfi_entry_signal_is_supported;
use crate::{evaluate_state_machine, StateMachineContext};

use super::confirmation::{evaluate_confirm_program, ConfirmInputs};
use super::stake::{evaluate_stake_program, EntryRequest, EntryStake, StakeInputs};
use super::state_machine::{order_value as state_machine_order_value, trade_value};

pub(crate) struct EntryExecution<'input, 'state> {
    pub(crate) config: &'input PortfolioConfig,
    pub(crate) protection_state: &'input ProtectionState,
    pub(crate) closed_trades: &'input [ClosedTrade],
    pub(crate) open_trades: &'state mut Vec<OpenTrade>,
    pub(crate) available_balance: &'state mut f64,
    pub(crate) rejected_signals: &'state mut u64,
    pub(crate) next_trade_id: &'state mut u64,
    pub(crate) next_order_id: &'state mut u64,
    pub(crate) maximum_concurrent_trades: &'state mut usize,
}

impl EntryExecution<'_, '_> {
    pub(crate) fn try_open(
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
            &EntryRequest {
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
#[allow(clippy::needless_pass_by_value)] // Retains the historical owned test-helper contract.
pub(crate) fn enter_trade(
    request: EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<Option<OpenTrade>, SimError> {
    attempt_entry(&request, config).map(|attempt| attempt.trade)
}

pub(crate) struct EntryAttempt {
    pub(crate) trade: Option<OpenTrade>,
    pub(crate) order_id_consumed: bool,
}

pub(crate) fn attempt_entry(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<EntryAttempt, SimError> {
    let leverage = entry_leverage(
        request.signal,
        config,
        request.pair,
        request.candle,
        request.stake.proposed,
    )?;
    let requested = requested_entry_stake(request, config, leverage)?;
    let &EntryRequest {
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
    if !entry_is_confirmed(request, config, amount)? {
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
        filled_order_aggregates: std::sync::OnceLock::new(),
        custom_data: BTreeMap::new(),
        nfi_adjustment_state: None,
    };
    reapply_inclusive_funding_after_entry_fill(&mut trade, candle, config.funding_fee_interval_ms);
    apply_order_filled(&mut trade, signal.tag.as_deref(), config)?;
    update_isolated_liquidation_price(&mut trade, config, candle.timestamp_ms)?;
    Ok(EntryAttempt {
        trade: Some(trade),
        order_id_consumed: true,
    })
}

pub(crate) fn requested_entry_stake(
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

pub(crate) fn initial_stop_loss(
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

pub(crate) fn pair_price_step(pair: &PairSeries, candle: &Candle, default: f64) -> f64 {
    let changes_before_or_at_candle = pair
        .price_steps
        .partition_point(|change| change.timestamp_ms <= candle.timestamp_ms);
    changes_before_or_at_candle
        .checked_sub(1)
        .and_then(|index| pair.price_steps.get(index))
        .map_or_else(|| pair.price_step.unwrap_or(default), |change| change.step)
}

pub(crate) fn entry_is_confirmed(
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

pub(crate) fn minimum_pair_stake(
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
pub(crate) fn adjustment_minimum_pair_stake(
    pair: &PairSeries,
    rate: f64,
    reserve_percent: f64,
) -> f64 {
    minimum_pair_stake(pair, rate, -0.1, 1.0, reserve_percent)
}

pub(crate) fn apply_order_filled(
    trade: &mut OpenTrade,
    order_tag: Option<&str>,
    config: &PortfolioConfig,
) -> Result<(), SimError> {
    if let Some(program) = &config.state_machine_program {
        if program.entrypoints.contains_key("order_filled") {
            let successful_entries = trade.orders.iter().filter(|order| order.is_entry).count();
            let mut context = StateMachineContext {
                trade: BTreeMap::new(),
                orders: BTreeMap::from([
                    (
                        "successful_entries".to_owned(),
                        Value::Number(
                            u64::try_from(successful_entries)
                                .map_err(|_| SimError::InvalidStateMachineProgram)?
                                .into(),
                        ),
                    ),
                    (
                        "count".to_owned(),
                        Value::Number(
                            u64::try_from(trade.orders.len())
                                .map_err(|_| SimError::InvalidStateMachineProgram)?
                                .into(),
                        ),
                    ),
                ]),
                custom_state: trade.custom_data.clone(),
                ..StateMachineContext::default()
            };
            for read in &program.required_reads {
                let value = match read.source {
                    StateMachineReadSource::Trade => trade_value(&read.key, trade),
                    StateMachineReadSource::Orders => state_machine_order_value(&read.key, trade),
                    StateMachineReadSource::Input if read.key == "pair" => {
                        Some(Value::String(trade.pair.clone()))
                    }
                    StateMachineReadSource::Input if read.key == "order_tag" => {
                        Some(order_tag.map_or(Value::Null, |tag| Value::String(tag.to_owned())))
                    }
                    StateMachineReadSource::Input if read.key == "current_time" => trade
                        .orders
                        .last()
                        .map(|order| Value::Number(order.filled_timestamp_ms.into())),
                    StateMachineReadSource::Candle
                    | StateMachineReadSource::Wallet
                    | StateMachineReadSource::CustomState
                    | StateMachineReadSource::Input
                    | StateMachineReadSource::Local => None,
                };
                if let Some(value) = value {
                    let destination = match read.source {
                        StateMachineReadSource::Trade => &mut context.trade,
                        StateMachineReadSource::Orders => &mut context.orders,
                        StateMachineReadSource::Input => &mut context.input,
                        StateMachineReadSource::Candle
                        | StateMachineReadSource::Wallet
                        | StateMachineReadSource::CustomState
                        | StateMachineReadSource::Local => continue,
                    };
                    destination.insert(read.key.clone(), value);
                }
            }
            if let Some(tag) = order_tag {
                context
                    .input
                    .insert("order_tag".to_owned(), Value::String(tag.to_owned()));
            }
            let action = evaluate_state_machine(program, "order_filled", &mut context)
                .map_err(|_| SimError::InvalidStateMachineProgram)?;
            if action.is_some_and(|action| action.kind != StateMachineActionKind::NoOp) {
                return Err(SimError::InvalidStateMachineProgram);
            }
            trade.custom_data = context.custom_state;
        }
        return Ok(());
    }
    let Some(program) = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
    else {
        return Ok(());
    };
    let successful_entries = trade.orders.iter().filter(|order| order.is_entry).count();
    if successful_entries == 1 {
        apply_custom_writes(
            &mut trade.custom_data,
            &program.initial_successful_entry_writes,
        );
    }
    let Some(mode) = order_tag.and_then(|tag| tag.split(' ').next()) else {
        return Ok(());
    };
    if let Some(writes) = program.order_tag_actions.get(mode) {
        apply_custom_writes(&mut trade.custom_data, writes);
    }
    Ok(())
}

pub(crate) fn apply_custom_writes(
    custom_data: &mut BTreeMap<String, Value>,
    writes: &[CustomDataWrite],
) {
    for write in writes {
        custom_data.insert(write.key.clone(), write.value.clone());
    }
}
