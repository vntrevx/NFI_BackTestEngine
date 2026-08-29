//! Builders for direct, source-ordered execution-boundary observations.

use std::collections::BTreeMap;

use crate::calculations::{fee_close, fee_open};
use crate::execution::pair_price_step;
use crate::portfolio::OpenTrade;
use crate::{
    AdjustmentSignal, Candle, ClosedTrade, EntryRejectionReason, ExecutionBoundary,
    ExecutionBoundaryEvent, ExecutionCandle, FilledOrder, PairSeries, PortfolioBoundaryState,
    PortfolioConfig, EXECUTION_BOUNDARY_EVENT_SCHEMA_VERSION,
};

pub(crate) fn decimal(value: f64) -> String {
    value.to_string()
}

pub(crate) fn fee_amount(amount: f64, price: f64, rate: f64) -> String {
    decimal(amount * price * rate)
}
pub(crate) const fn rejection_reason(reason: EntryRejectionReason) -> &'static str {
    match reason {
        EntryRejectionReason::PairLocked => "pair_locked",
        EntryRejectionReason::SlotLimit => "slot_limit",
        EntryRejectionReason::MinimumStake => "minimum_stake",
        EntryRejectionReason::StakePrecision => "stake_precision",
        EntryRejectionReason::EntryConfirmation => "entry_confirmation",
    }
}

pub(crate) struct EventInput<'a> {
    pub(crate) timestamp_ms: i64,
    pub(crate) pair: &'a str,
    pub(crate) candle: &'a Candle,
    pub(crate) phase: ExecutionBoundary,
    pub(crate) order_type: &'static str,
    pub(crate) state_before: Option<PortfolioBoundaryState>,
    pub(crate) state_after: Option<PortfolioBoundaryState>,
}

pub(crate) fn event(sequence: &mut u64, input: EventInput<'_>) -> ExecutionBoundaryEvent {
    let value = ExecutionBoundaryEvent {
        schema_version: EXECUTION_BOUNDARY_EVENT_SCHEMA_VERSION,
        sequence: *sequence,
        timestamp_ms: input.timestamp_ms,
        pair: input.pair.to_owned(),
        phase: input.phase,
        order_type: input.order_type,
        order_status: None,
        proposed_rate: None,
        clamped_rate: None,
        precision_rate: None,
        within_candle: None,
        timeout_checked: None,
        timed_out: None,
        candle: ExecutionCandle {
            open: decimal(input.candle.open),
            high: decimal(input.candle.high),
            low: decimal(input.candle.low),
            close: decimal(input.candle.close),
        },
        candidates: Vec::new(),
        winner: None,
        confirmation: None,
        rejection_reason: None,
        trade_id: None,
        order_id: None,
        amount_input: None,
        amount_step: None,
        amount_output: None,
        price_input: None,
        current_price_step: None,
        price_step: None,
        price_output: None,
        minimum_stake: None,
        minimum_stake_stage: None,
        minimum_stake_accepted: None,
        fee_open: None,
        fee_close: None,
        fee_applied: None,
        intermediates: BTreeMap::new(),
        state_before: input.state_before,
        state_after: input.state_after,
    };
    *sequence = sequence.saturating_add(1);
    value
}

pub(crate) struct AdjustmentEventInput<'a> {
    pub(crate) pair: &'a PairSeries,
    pub(crate) candle: &'a Candle,
    pub(crate) trade: &'a OpenTrade,
    pub(crate) adjustment: &'a AdjustmentSignal,
    pub(crate) config: &'a PortfolioConfig,
    pub(crate) state_before: PortfolioBoundaryState,
    pub(crate) state_after: PortfolioBoundaryState,
}

pub(crate) fn adjustment_event(
    sequence: &mut u64,
    input: AdjustmentEventInput<'_>,
) -> Option<ExecutionBoundaryEvent> {
    let order = input.trade.orders.last()?;
    let partial = input.adjustment.stake_amount < 0.0;
    let order_type = if partial {
        input.config.exit_order_type
    } else {
        input.config.entry_order_type
    };
    let stake_before = decimal(input.state_before.wallet_tied);
    let stake_after = decimal(input.state_after.wallet_tied);
    let mut value = event(
        sequence,
        EventInput {
            timestamp_ms: input.candle.timestamp_ms,
            pair: &input.pair.pair,
            candle: input.candle,
            phase: if partial {
                ExecutionBoundary::PartialExitFill
            } else {
                ExecutionBoundary::AdjustmentFill
            },
            order_type: order_type.as_str(),
            state_before: Some(input.state_before),
            state_after: Some(input.state_after),
        },
    );
    value.winner = Some(input.adjustment.tag.clone());
    value.trade_id = Some(input.trade.id);
    value.order_id = Some(order.id);
    value.amount_input = Some(decimal(input.adjustment.stake_amount));
    value.amount_step = Some(decimal(input.trade.amount_step));
    value.amount_output = Some(decimal(order.amount));
    value.price_input = Some(decimal(input.candle.open));
    value.current_price_step = Some(decimal(pair_price_step(
        input.pair,
        input.candle,
        input.config.price_step,
    )));
    value.price_step = Some(decimal(input.trade.price_step));
    value.price_output = Some(decimal(order.price));
    value.proposed_rate = Some(decimal(input.candle.open));
    value.clamped_rate = Some(decimal(input.candle.open));
    value.precision_rate = Some(decimal(order.price));
    value.within_candle = Some(input.candle.low <= order.price && order.price <= input.candle.high);
    value.order_status = Some("filled");
    value.timeout_checked = Some(false);
    value.timed_out = Some(false);
    value.fee_open = Some(decimal(fee_open(input.config)));
    value.fee_close = Some(decimal(fee_close(input.config)));
    value.fee_applied = Some(fee_amount(
        order.amount,
        order.price,
        if order.is_entry {
            fee_open(input.config)
        } else {
            fee_close(input.config)
        },
    ));
    value
        .intermediates
        .insert("stake_before".to_owned(), stake_before);
    value
        .intermediates
        .insert("stake_after".to_owned(), stake_after);
    Some(value)
}

pub(crate) struct ExitFillEventInput<'a> {
    pub(crate) pair: &'a PairSeries,
    pub(crate) candle: &'a Candle,
    pub(crate) closed: &'a ClosedTrade,
    pub(crate) order: &'a FilledOrder,
    pub(crate) requested_rate: f64,
    pub(crate) frozen_price_step: f64,
    pub(crate) config: &'a PortfolioConfig,
    pub(crate) state_before: PortfolioBoundaryState,
    pub(crate) state_after: PortfolioBoundaryState,
}

pub(crate) fn exit_fill_event(
    sequence: &mut u64,
    input: ExitFillEventInput<'_>,
) -> ExecutionBoundaryEvent {
    let mut value = event(
        sequence,
        EventInput {
            timestamp_ms: input.candle.timestamp_ms,
            pair: &input.pair.pair,
            candle: input.candle,
            phase: ExecutionBoundary::ExitFill,
            order_type: input.config.exit_order_type.as_str(),
            state_before: Some(input.state_before),
            state_after: Some(input.state_after),
        },
    );
    value.winner = Some(input.closed.exit_reason.clone());
    value.trade_id = Some(input.closed.id);
    value.order_id = Some(input.order.id);
    value.amount_input = Some(decimal(input.closed.amount));
    value.amount_step = Some(decimal(
        input.pair.amount_step.unwrap_or(input.config.amount_step),
    ));
    value.amount_output = Some(decimal(input.order.amount));
    value.price_input = Some(decimal(input.requested_rate));
    value.current_price_step = Some(decimal(pair_price_step(
        input.pair,
        input.candle,
        input.config.price_step,
    )));
    value.price_step = Some(decimal(input.frozen_price_step));
    value.price_output = Some(decimal(input.order.price));
    value.proposed_rate = Some(decimal(input.requested_rate));
    value.clamped_rate = Some(decimal(input.requested_rate));
    value.precision_rate = Some(decimal(input.order.price));
    value.within_candle =
        Some(input.candle.low <= input.order.price && input.order.price <= input.candle.high);
    value.order_status = Some("filled");
    value.timeout_checked = Some(false);
    value.timed_out = Some(false);
    value.fee_open = Some(decimal(input.closed.fee_open));
    value.fee_close = Some(decimal(input.closed.fee_close));
    value.fee_applied = Some(fee_amount(
        input.order.amount,
        input.order.price,
        input.closed.fee_close,
    ));
    value
        .intermediates
        .insert("profit_abs".to_owned(), decimal(input.closed.profit_abs));
    value
}
