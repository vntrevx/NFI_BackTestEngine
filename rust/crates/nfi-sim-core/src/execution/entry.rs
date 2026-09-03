//! Entry arbitration, sizing, order creation, and order-filled effects.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::{
    available_stake_amount, ceil_step, checked_float_sum, entry_order_side, entry_sizing, fee_open,
    floor_step, round_step,
};
use crate::domain::{
    CallbackInvocation, CallbackOutcome, CallbackPhase, CallbackReturnClass, CallbackTransaction,
    Candle, ClosedTrade, CustomDataWrite, EntrySignal, ExecutableCallbackError, FilledOrder,
    OrderType, PairSeries, PortfolioConfig, SimError, StateMachineActionKind,
    StateMachineReadSource,
};
use crate::futures::{
    entry_leverage, reapply_inclusive_funding_after_entry_fill, update_isolated_liquidation_price,
};
use crate::portfolio::{wallet_free, OpenTrade, TradeSide};
use crate::protections::ProtectionState;
use crate::scheduler_observer::{self, BoundaryContext, BoundaryDetail};
use crate::validation::nfi_entry_signal_is_supported;
use crate::{
    evaluate_state_machine, EntryRejectionReason, PortfolioBoundary, PortfolioBoundaryEvent,
    StateMachineContext,
};

use super::callback_trace::{
    record_current as trace_callback, record_trade as trace_trade_callback, ExecutableCallbacks,
};
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
    pub(crate) processing_order_index: usize,
    pub(crate) portfolio_events: &'state mut Vec<PortfolioBoundaryEvent>,
    pub(crate) portfolio_event_sequence: &'state mut u64,
}

impl EntryExecution<'_, '_> {
    pub(crate) fn try_open(
        &mut self,
        pair_index: usize,
        pair: &PairSeries,
        candle: &Candle,
        side: TradeSide,
        signal: &EntrySignal,
        mut executable_callbacks: Option<&mut ExecutableCallbacks<'_, '_, '_>>,
    ) -> Result<bool, SimError> {
        let state_before = self.boundary_state()?;
        if self.reject_at_gate(pair_index, pair, candle, side, state_before.clone())? {
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

        let (proposed_stake, stake_available, compounding_base) = self.entry_stake()?;
        let request = EntryRequest {
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
        };
        let attempt = attempt_entry_with_callbacks(
            &request,
            self.config,
            executable_callbacks.as_deref_mut(),
        )?;
        let allocated_order_id = attempt.order_id_consumed.then_some(*self.next_order_id);
        if attempt.order_id_consumed {
            // Freqtrade allocates the order after minimum-stake validation but
            // before amount precision and confirmation. Either later rejection
            // therefore leaves a deliberate allocator gap.
            *self.next_order_id += 1;
        }
        let Some(trade) = attempt.trade else {
            self.record_entry(
                pair_index,
                pair,
                candle,
                PortfolioBoundary::EntryRejected,
                state_before,
                entry_detail(
                    attempt.rejection_reason,
                    allocated_order_id,
                    Some(proposed_stake),
                    Some(compounding_base),
                ),
            )?;
            return Ok(false);
        };

        let allocated_trade_id = *self.next_trade_id;
        let allocated_order_id = allocated_order_id.ok_or(SimError::InvalidCallbackRuntime)?;
        *self.next_trade_id += 1;
        self.open_trades.push(trade);
        *self.maximum_concurrent_trades =
            (*self.maximum_concurrent_trades).max(self.open_trades.len());
        *self.available_balance = wallet_free(
            self.config.starting_balance,
            self.open_trades,
            self.closed_trades,
            self.config.is_futures,
        )?;
        let mut detail = entry_detail(
            None,
            Some(allocated_order_id),
            Some(proposed_stake),
            Some(compounding_base),
        );
        detail.allocated_trade_id = Some(allocated_trade_id);
        self.record_entry(
            pair_index,
            pair,
            candle,
            PortfolioBoundary::EntryAccepted,
            state_before,
            detail,
        )?;
        if let Some(trade) = self.open_trades.last_mut() {
            if let Some(callbacks) = executable_callbacks {
                executable_order_filled(callbacks, trade, *self.available_balance)?;
            } else {
                trace_trade_callback(
                    CallbackPhase::OrderFilled,
                    CallbackOutcome::Accepted,
                    CallbackTransaction::Committed,
                    *self.available_balance,
                    trade,
                    None,
                )?;
            }
        }
        Ok(true)
    }

    fn reject_at_gate(
        &mut self,
        pair_index: usize,
        pair: &PairSeries,
        candle: &Candle,
        side: TradeSide,
        state_before: crate::PortfolioBoundaryState,
    ) -> Result<bool, SimError> {
        let reason = if self
            .protection_state
            .is_pair_locked(&pair.pair, candle.timestamp_ms, side)
        {
            Some(EntryRejectionReason::PairLocked)
        } else if self.open_trades.len() >= self.config.max_open_trades {
            *self.rejected_signals += 1;
            Some(EntryRejectionReason::SlotLimit)
        } else {
            None
        };
        if let Some(reason) = reason {
            self.record_entry(
                pair_index,
                pair,
                candle,
                PortfolioBoundary::EntryRejected,
                state_before,
                entry_detail(Some(reason), None, None, None),
            )?;
        }
        Ok(reason.is_some())
    }

    fn entry_stake(&self) -> Result<(f64, f64, f64), SimError> {
        let tied_up_stake = checked_float_sum(
            &self
                .open_trades
                .iter()
                .map(|trade| trade.stake_amount)
                .collect::<Vec<_>>(),
            "entry-tied-up-stake",
        )?;
        let compounding_base = checked_float_sum(
            &[*self.available_balance, tied_up_stake],
            "entry-compounding-base",
        )?;
        let available = available_stake_amount(
            *self.available_balance,
            tied_up_stake,
            self.config.tradable_balance_ratio,
        )?;
        let proposed = if self.config.unlimited_stake {
            let divisor = f64::from(
                u32::try_from(self.config.max_open_trades)
                    .expect("validated max_open_trades fits u32"),
            );
            ((available + tied_up_stake) / divisor).min(available)
        } else {
            self.config.stake_amount.min(available)
        };
        Ok((proposed, available, compounding_base))
    }

    fn boundary_state(&self) -> Result<crate::PortfolioBoundaryState, SimError> {
        scheduler_observer::state(
            *self.available_balance,
            self.open_trades,
            self.closed_trades,
            self.config.max_open_trades,
            *self.next_trade_id,
            *self.next_order_id,
            *self.rejected_signals,
        )
    }

    fn record_entry(
        &mut self,
        pair_index: usize,
        pair: &PairSeries,
        candle: &Candle,
        boundary: PortfolioBoundary,
        state_before: crate::PortfolioBoundaryState,
        detail: BoundaryDetail,
    ) -> Result<(), SimError> {
        let state_after = self.boundary_state()?;
        self.portfolio_events.push(scheduler_observer::event(
            self.portfolio_event_sequence,
            &BoundaryContext {
                timestamp_ms: candle.timestamp_ms,
                pair: &pair.pair,
                configured_pair_index: pair_index,
                processing_order_index: self.processing_order_index,
            },
            boundary,
            state_before,
            state_after,
            detail,
        ));
        Ok(())
    }
}

fn entry_detail(
    rejection_reason: Option<EntryRejectionReason>,
    allocated_order_id: Option<u64>,
    proposed_stake: Option<f64>,
    compounding_base: Option<f64>,
) -> BoundaryDetail {
    let mut detail = BoundaryDetail::plain();
    detail.rejection_reason = rejection_reason;
    detail.allocated_order_id = allocated_order_id;
    detail.proposed_stake = proposed_stake;
    detail.compounding_base = compounding_base;
    detail
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
    pub(crate) rejection_reason: Option<EntryRejectionReason>,
}

#[cfg(test)]
pub(crate) fn attempt_entry(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<EntryAttempt, SimError> {
    attempt_entry_with_callbacks(request, config, None)
}

fn attempt_entry_with_callbacks(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    mut executable_callbacks: Option<&mut ExecutableCallbacks<'_, '_, '_>>,
) -> Result<EntryAttempt, SimError> {
    let rate = entry_rate(request, config)?;
    let executable_selection = executable_callbacks
        .as_mut()
        .map(|callbacks| executable_entry_selection(callbacks, request, config, rate))
        .transpose()?;
    let (requested, leverage) = if let Some(selection) = &executable_selection {
        (selection.requested_stake, selection.leverage)
    } else {
        entry_callback_values(request, config, rate)?
    };
    let minimum = minimum_pair_stake(
        request.pair,
        rate,
        config.stoploss_ratio,
        leverage,
        config.amount_reserve_percent,
    );
    let Some(validated_stake) = validate_stake_amount(requested, minimum, request.stake.maximum)
    else {
        return Ok(rejected_entry_attempt(
            EntryRejectionReason::MinimumStake,
            false,
        ));
    };
    let Some((amount, stake, precise_cost, order_cost)) = entry_sizing(
        validated_stake,
        rate,
        fee_open(config),
        request.pair.amount_step.unwrap_or(config.amount_step),
        leverage,
    )?
    else {
        return Ok(rejected_entry_attempt(
            EntryRejectionReason::StakePrecision,
            true,
        ));
    };
    let confirmed = if let (Some(callbacks), Some(selection)) =
        (executable_callbacks.as_mut(), executable_selection.as_ref())
    {
        executable_entry_confirmation(callbacks, request, selection, amount, config, rate)?
    } else {
        let confirmed = entry_is_confirmed(request, config, amount, rate)?;
        trace_entry_confirmation(confirmed)?;
        confirmed
    };
    if !confirmed {
        return Ok(rejected_entry_attempt(
            EntryRejectionReason::EntryConfirmation,
            true,
        ));
    }
    Ok(EntryAttempt {
        trade: Some(build_entry_trade(
            request,
            config,
            leverage,
            rate,
            amount,
            stake,
            precise_cost,
            order_cost,
        )?),
        order_id_consumed: true,
        rejection_reason: None,
    })
}

pub(crate) fn validate_stake_amount(requested: f64, minimum: f64, maximum: f64) -> Option<f64> {
    if !requested.is_finite()
        || !minimum.is_finite()
        || !maximum.is_finite()
        || requested <= 0.0
        || minimum > maximum
    {
        return None;
    }
    let validated = if requested < minimum {
        if requested * 1.3 < minimum {
            return None;
        }
        minimum
    } else {
        requested
    };
    Some(validated.min(maximum))
}

fn rejected_entry_attempt(
    rejection_reason: EntryRejectionReason,
    order_id_consumed: bool,
) -> EntryAttempt {
    EntryAttempt {
        trade: None,
        order_id_consumed,
        rejection_reason: Some(rejection_reason),
    }
}

#[allow(clippy::too_many_arguments)] // Keep each precision-stage fill value explicit.
fn build_entry_trade(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    leverage: f64,
    rate: f64,
    amount: f64,
    stake: f64,
    precise_cost: f64,
    order_cost: f64,
) -> Result<OpenTrade, SimError> {
    let order = FilledOrder {
        id: request.order_id,
        funding_fee: 0.0,
        sequence: 0,
        side: entry_order_side(request.side),
        is_entry: true,
        filled_timestamp_ms: request.candle.timestamp_ms,
        amount,
        price: rate,
        cost: order_cost,
        tag: request.signal.tag.clone(),
    };
    let price_step = pair_price_step(request.pair, request.candle, config.price_step);
    let stop_loss = initial_stop_loss(
        request.side,
        rate,
        config.stoploss_ratio,
        leverage,
        price_step,
    )?;
    let mut trade = OpenTrade {
        id: request.id,
        pair_index: request.pair_index,
        pair: request.pair.pair.clone(),
        side: request.side,
        leverage,
        amount_step: request.pair.amount_step.unwrap_or(config.amount_step),
        price_step,
        open_timestamp_ms: request.candle.timestamp_ms,
        open_rate: rate,
        amount,
        stake_amount: stake,
        max_stake_amount: stake,
        entry_cost_with_fees: precise_cost,
        first_entry_cost_with_fees: precise_cost,
        adjustment_count: 0,
        entry_tag: request.signal.tag.clone(),
        entry_tag_cache: std::sync::OnceLock::new(),
        funding_fees: 0.0,
        funding_fees_total: 0.0,
        funding_sum_high: 0.0,
        funding_sum_low: 0.0,
        funding_rebase_seed: None,
        realized_partial_profit: 0.0,
        liquidation_price: request.signal.liquidation_price,
        liquidation_price_is_explicit: request.signal.liquidation_price.is_some(),
        initial_stop_loss: stop_loss,
        stop_loss,
        custom_stop_loss_ratio: None,
        minimum_rate: request.candle.low,
        maximum_rate: request.candle.high,
        orders: vec![order],
        filled_order_aggregates: std::sync::OnceLock::new(),
        custom_data: BTreeMap::new(),
        nfi_adjustment_state: None,
    };
    reapply_inclusive_funding_after_entry_fill(
        &mut trade,
        request.candle,
        config.funding_fee_interval_ms,
    )?;
    apply_order_filled(&mut trade, request.signal.tag.as_deref(), config)?;
    update_isolated_liquidation_price(&mut trade, config, request.candle.timestamp_ms)?;
    Ok(trade)
}

fn trace_entry_confirmation(confirmed: bool) -> Result<(), SimError> {
    trace_callback(
        CallbackPhase::EntryConfirmation,
        if confirmed {
            CallbackOutcome::Accepted
        } else {
            CallbackOutcome::Rejected
        },
    )
}

pub(crate) struct ExecutableEntrySelection {
    pub(crate) requested_stake: f64,
    pub(crate) leverage: f64,
}

pub(crate) fn executable_entry_selection(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    rate: f64,
) -> Result<ExecutableEntrySelection, ExecutableCallbackError> {
    let proposed_leverage = request.signal.leverage.or(config.leverage).unwrap_or(1.0);
    let maximum_leverage = config
        .maximum_leverage_by_pair
        .get(&request.pair.pair)
        .copied()
        .unwrap_or(proposed_leverage.max(1.0));
    let side = match request.side {
        TradeSide::Long => "long",
        TradeSide::Short => "short",
    };
    let mut custom_state = BTreeMap::new();
    let leverage = if config.is_futures {
        let inputs = BTreeMap::from([
            ("pair".to_owned(), Value::String(request.pair.pair.clone())),
            (
                "current_time".to_owned(),
                Value::from(request.candle.timestamp_ms),
            ),
            ("current_rate".to_owned(), Value::from(rate)),
            (
                "proposed_leverage".to_owned(),
                Value::from(proposed_leverage),
            ),
            ("max_leverage".to_owned(), Value::from(maximum_leverage)),
            ("side".to_owned(), Value::String(side.to_owned())),
            (
                "entry_tag".to_owned(),
                request
                    .signal
                    .tag
                    .as_ref()
                    .map_or(Value::Null, |tag| Value::String(tag.clone())),
            ),
        ]);
        let invocation = CallbackInvocation::new("leverage", request.candle.timestamp_ms, inputs);
        let event = callbacks.invoke(&invocation, &mut custom_state)?;
        event
            .return_value
            .as_ref()
            .and_then(Value::as_f64)
            .unwrap_or(1.0)
            .clamp(1.0, maximum_leverage)
    } else {
        1.0
    };
    let minimum = minimum_pair_stake(
        request.pair,
        rate,
        config.stoploss_ratio,
        leverage,
        config.amount_reserve_percent,
    );
    let inputs = BTreeMap::from([
        ("pair".to_owned(), Value::String(request.pair.pair.clone())),
        (
            "current_time".to_owned(),
            Value::from(request.candle.timestamp_ms),
        ),
        ("current_rate".to_owned(), Value::from(rate)),
        (
            "proposed_stake".to_owned(),
            Value::from(request.stake.proposed),
        ),
        ("min_stake".to_owned(), Value::from(minimum)),
        ("max_stake".to_owned(), Value::from(request.stake.maximum)),
        ("leverage".to_owned(), Value::from(leverage)),
        ("side".to_owned(), Value::String(side.to_owned())),
        (
            "entry_tag".to_owned(),
            request
                .signal
                .tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
    ]);
    let invocation =
        CallbackInvocation::new("custom_stake_amount", request.candle.timestamp_ms, inputs);
    let event = callbacks.invoke(&invocation, &mut custom_state)?;
    let requested_stake = if event.return_class == CallbackReturnClass::None {
        request.stake.proposed
    } else {
        event
            .return_value
            .as_ref()
            .and_then(Value::as_f64)
            .unwrap_or(request.stake.proposed)
    }
    .min(request.stake.maximum);
    Ok(ExecutableEntrySelection {
        requested_stake,
        leverage,
    })
}

pub(crate) fn executable_entry_confirmation(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    request: &EntryRequest<'_>,
    selection: &ExecutableEntrySelection,
    amount: f64,
    config: &PortfolioConfig,
    rate: f64,
) -> Result<bool, ExecutableCallbackError> {
    let inputs = BTreeMap::from([
        ("pair".to_owned(), Value::String(request.pair.pair.clone())),
        (
            "current_time".to_owned(),
            Value::from(request.candle.timestamp_ms),
        ),
        ("amount".to_owned(), Value::from(amount)),
        ("rate".to_owned(), Value::from(rate)),
        (
            "order_type".to_owned(),
            Value::String(config.entry_order_type.as_str().to_owned()),
        ),
        (
            "stake_amount".to_owned(),
            Value::from(selection.requested_stake),
        ),
        ("leverage".to_owned(), Value::from(selection.leverage)),
        (
            "entry_tag".to_owned(),
            request
                .signal
                .tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
    ]);
    let mut custom_state = BTreeMap::new();
    let invocation =
        CallbackInvocation::new("confirm_trade_entry", request.candle.timestamp_ms, inputs);
    let event = callbacks.invoke(&invocation, &mut custom_state)?;
    Ok(event.return_value.as_ref().and_then(Value::as_bool) != Some(false))
}

fn entry_callback_values(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    rate: f64,
) -> Result<(f64, f64), SimError> {
    let stake_leverage = request.signal.leverage.or(config.leverage).unwrap_or(1.0);
    let requested = requested_entry_stake(request, config, stake_leverage, rate)?;
    trace_callback(CallbackPhase::StakeSizing, CallbackOutcome::Value)?;
    let leverage = entry_leverage(
        request.signal,
        config,
        request.pair,
        request.candle,
        requested,
    )?;
    trace_callback(CallbackPhase::Leverage, CallbackOutcome::Value)?;
    Ok((requested, leverage))
}

pub(crate) fn requested_entry_stake(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    leverage: f64,
    rate: f64,
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
                rate,
                config.stoploss_ratio,
                leverage,
                config.amount_reserve_percent,
            ),
            maximum_stake: request.stake.maximum,
            current_rate: rate,
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
) -> Result<f64, SimError> {
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

pub(crate) fn entry_rate(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
) -> Result<f64, SimError> {
    if config.entry_order_type == OrderType::Market {
        return Ok(request.candle.open);
    }
    let requested = config
        .entry_rates_by_pair
        .get(&request.pair.pair)
        .and_then(|rates| rates.get(&request.candle.timestamp_ms))
        .copied()
        .unwrap_or(request.candle.open);
    round_step(
        requested,
        pair_price_step(request.pair, request.candle, config.price_step),
    )
}

pub(crate) fn entry_is_confirmed(
    request: &EntryRequest<'_>,
    config: &PortfolioConfig,
    amount: f64,
    rate: f64,
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
            rate,
            entry_tag: request.signal.tag.as_deref(),
            side: request.side,
            previous_close: request.candle.previous_close,
            open_trades: request.open_trades,
            max_open_trades: config.max_open_trades,
            is_futures: config.is_futures,
            order_type: config.entry_order_type,
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

pub(crate) fn executable_order_filled(
    callbacks: &mut ExecutableCallbacks<'_, '_, '_>,
    trade: &mut OpenTrade,
    wallet_available: f64,
) -> Result<(), ExecutableCallbackError> {
    let Some(order) = trade.orders.last() else {
        return Ok(());
    };
    let inputs = BTreeMap::from([
        ("pair".to_owned(), Value::String(trade.pair.clone())),
        (
            "current_time".to_owned(),
            Value::from(order.filled_timestamp_ms),
        ),
    ]);
    let mut invocation = CallbackInvocation::new("order_filled", order.filled_timestamp_ms, inputs);
    invocation.trade = BTreeMap::from([
        ("id".to_owned(), Value::from(trade.id)),
        ("amount".to_owned(), Value::from(trade.amount)),
        ("stake_amount".to_owned(), Value::from(trade.stake_amount)),
        ("open_rate".to_owned(), Value::from(trade.open_rate)),
        ("leverage".to_owned(), Value::from(trade.leverage)),
        ("order_count".to_owned(), Value::from(trade.orders.len())),
        (
            "nr_of_successful_entries".to_owned(),
            Value::from(trade.orders.iter().filter(|order| order.is_entry).count()),
        ),
        (
            "nr_of_successful_exits".to_owned(),
            Value::from(trade.orders.iter().filter(|order| !order.is_entry).count()),
        ),
        (
            "orders".to_owned(),
            Value::Array(
                trade
                    .orders
                    .iter()
                    .map(|order| Value::from(order.id))
                    .collect(),
            ),
        ),
        (
            "entry_side".to_owned(),
            Value::String(
                match trade.side {
                    TradeSide::Long => "buy",
                    TradeSide::Short => "sell",
                }
                .to_owned(),
            ),
        ),
    ]);
    invocation.order = BTreeMap::from([
        ("id".to_owned(), Value::from(order.id)),
        ("amount".to_owned(), Value::from(order.amount)),
        ("price".to_owned(), Value::from(order.price)),
        ("cost".to_owned(), Value::from(order.cost)),
        ("is_entry".to_owned(), Value::from(order.is_entry)),
        (
            "ft_order_side".to_owned(),
            Value::String(
                match order.side {
                    crate::OrderSide::Buy => "buy",
                    crate::OrderSide::Sell => "sell",
                }
                .to_owned(),
            ),
        ),
        (
            "ft_order_tag".to_owned(),
            order
                .tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
        (
            "tag".to_owned(),
            order
                .tag
                .as_ref()
                .map_or(Value::Null, |tag| Value::String(tag.clone())),
        ),
    ]);
    invocation.wallet = BTreeMap::from([("available".to_owned(), Value::from(wallet_available))]);
    callbacks.invoke(&invocation, &mut trade.custom_data)?;
    Ok(())
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
