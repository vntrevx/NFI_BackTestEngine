//! Exact projections captured at chronological portfolio mutation boundaries.

use crate::calculations::{checked_finite, checked_float_sum};
use crate::portfolio::OpenTrade;
use crate::{
    ClosedTrade, EntryRejectionReason, PortfolioBoundary, PortfolioBoundaryEvent,
    PortfolioBoundaryState, SimError, PORTFOLIO_EVENT_SCHEMA_VERSION,
};

pub(crate) struct BoundaryContext<'a> {
    pub(crate) timestamp_ms: i64,
    pub(crate) pair: &'a str,
    pub(crate) configured_pair_index: usize,
    pub(crate) processing_order_index: usize,
}

#[derive(Clone, Copy)]
pub(crate) struct BoundaryDetail {
    pub(crate) rejection_reason: Option<EntryRejectionReason>,
    pub(crate) allocated_trade_id: Option<u64>,
    pub(crate) allocated_order_id: Option<u64>,
    pub(crate) proposed_stake: Option<f64>,
    pub(crate) compounding_base: Option<f64>,
    pub(crate) partial_exit_slot_retained: Option<bool>,
    pub(crate) force_exit_index: Option<usize>,
}

impl BoundaryDetail {
    pub(crate) const fn plain() -> Self {
        Self {
            rejection_reason: None,
            allocated_trade_id: None,
            allocated_order_id: None,
            proposed_stake: None,
            compounding_base: None,
            partial_exit_slot_retained: None,
            force_exit_index: None,
        }
    }
}

pub(crate) fn state(
    wallet_free: f64,
    open_trades: &[OpenTrade],
    closed_trades: &[ClosedTrade],
    slot_limit: usize,
    next_trade_id: u64,
    next_order_id: u64,
    rejected_signals: u64,
) -> Result<PortfolioBoundaryState, SimError> {
    let wallet_tied = sum_open(
        open_trades,
        |trade| trade.stake_amount,
        "boundary-wallet-tied",
    )?;
    let realized_partial = sum_open(
        open_trades,
        |trade| trade.realized_partial_profit,
        "boundary-realized-partial",
    )?;
    let realized_closed = checked_float_sum(
        &closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
        "boundary-realized-closed",
    )?;
    Ok(PortfolioBoundaryState {
        wallet_free: checked_finite(wallet_free, "boundary-wallet-free")?,
        wallet_tied,
        realized_closed,
        realized_partial,
        occupied_slots: open_trades.len(),
        slot_limit,
        open_trade_ids: open_trades.iter().map(|trade| trade.id).collect(),
        open_trade_pairs: open_trades.iter().map(|trade| trade.pair.clone()).collect(),
        open_order_ids: open_trades
            .iter()
            .flat_map(|trade| trade.orders.iter().map(|order| order.id))
            .collect(),
        next_trade_id,
        next_order_id,
        rejected_signals,
    })
}

pub(crate) fn adjustment_event(
    sequence: &mut u64,
    context: &BoundaryContext<'_>,
    state_before: PortfolioBoundaryState,
    state_after: PortfolioBoundaryState,
    is_partial_exit: bool,
) -> PortfolioBoundaryEvent {
    let mut detail = BoundaryDetail::plain();
    detail.allocated_order_id = Some(state_before.next_order_id);
    if is_partial_exit {
        detail.partial_exit_slot_retained =
            Some(state_before.occupied_slots == state_after.occupied_slots);
    }
    event(
        sequence,
        context,
        if is_partial_exit {
            PortfolioBoundary::PartialExit
        } else {
            PortfolioBoundary::PositionAdjustment
        },
        state_before,
        state_after,
        detail,
    )
}

pub(crate) fn event(
    sequence: &mut u64,
    context: &BoundaryContext<'_>,
    boundary: PortfolioBoundary,
    state_before: PortfolioBoundaryState,
    state_after: PortfolioBoundaryState,
    detail: BoundaryDetail,
) -> PortfolioBoundaryEvent {
    let value = PortfolioBoundaryEvent {
        schema_version: PORTFOLIO_EVENT_SCHEMA_VERSION,
        sequence: *sequence,
        timestamp_ms: context.timestamp_ms,
        boundary,
        pair: context.pair.to_owned(),
        configured_pair_index: context.configured_pair_index,
        processing_order_index: context.processing_order_index,
        state_before,
        state_after,
        rejection_reason: detail.rejection_reason,
        allocated_trade_id: detail.allocated_trade_id,
        allocated_order_id: detail.allocated_order_id,
        proposed_stake: detail.proposed_stake,
        compounding_base: detail.compounding_base,
        partial_exit_slot_retained: detail.partial_exit_slot_retained,
        force_exit_index: detail.force_exit_index,
        force_exit_trade_id: None,
        force_exit_order_ids: Vec::new(),
    };
    *sequence = sequence.saturating_add(1);
    value
}

fn sum_open(
    trades: &[OpenTrade],
    value: impl Fn(&OpenTrade) -> f64,
    operation: &'static str,
) -> Result<f64, SimError> {
    checked_float_sum(&trades.iter().map(value).collect::<Vec<_>>(), operation)
}
