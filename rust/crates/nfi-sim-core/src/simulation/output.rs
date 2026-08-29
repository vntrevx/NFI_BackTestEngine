//! Force-exit ordering and canonical aggregate result assembly.

use crate::calculations::{checked_float_sum, checked_pairwise_sum, checked_python_float_sum};
use crate::execution::{close_trade, CloseTradeContext};
use crate::execution_observer::{self, ExitFillEventInput};
use crate::portfolio::{wallet_free, OpenTrade};
use crate::protections::ProtectionState;
use crate::scheduler_observer::{self, BoundaryContext, BoundaryDetail};
use crate::{
    ClosedTrade, ExecutionBoundaryEvent, PortfolioBoundary, PortfolioBoundaryEvent, SimError,
    SimulationInput, SimulationResult, SIMULATOR_SCHEMA_VERSION,
};

pub(super) struct FinalizationInput<'input, 'sequence, 'portfolio, 'execution> {
    pub(super) input: &'input SimulationInput,
    pub(super) open_trades: Vec<OpenTrade>,
    pub(super) closed_trades: Vec<ClosedTrade>,
    pub(super) protection_state: ProtectionState,
    pub(super) rejected_signals: u64,
    pub(super) maximum_concurrent_trades: usize,
    pub(super) next_trade_id: u64,
    pub(super) next_order_id: u64,
    pub(super) portfolio_event_sequence: &'sequence mut u64,
    pub(super) portfolio_observer: Option<&'portfolio mut dyn FnMut(&PortfolioBoundaryEvent)>,
    pub(super) execution_event_sequence: &'sequence mut u64,
    pub(super) execution_observer: Option<&'execution mut dyn FnMut(&ExecutionBoundaryEvent)>,
}

struct FinalPortfolio {
    closed_trades: Vec<ClosedTrade>,
    protection_state: ProtectionState,
}

pub(super) fn finalize_simulation(
    state: FinalizationInput<'_, '_, '_, '_>,
) -> Result<SimulationResult, SimError> {
    let maximum_concurrent_trades = state.maximum_concurrent_trades;
    let rejected_signals = state.rejected_signals;
    let config = &state.input.config;
    let mut final_portfolio = force_exit_open_trades(state)?;
    for (sequence, trade) in final_portfolio.closed_trades.iter_mut().enumerate() {
        trade.sequence = sequence;
    }
    let profit_total_abs = checked_pairwise_sum(
        &final_portfolio
            .closed_trades
            .iter()
            .map(|trade| trade.profit_abs)
            .collect::<Vec<_>>(),
        "profit-total-abs",
    )?;
    let per_trade_volumes = final_portfolio
        .closed_trades
        .iter()
        .map(|trade| {
            checked_python_float_sum(
                trade.orders.iter().map(|order| order.cost),
                "trade-total-volume",
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    let total_volume = checked_python_float_sum(per_trade_volumes, "total-volume")?;
    let final_balance = checked_float_sum(
        &[config.starting_balance, profit_total_abs],
        "final-wallet-balance",
    )?;
    let result = SimulationResult {
        schema_version: SIMULATOR_SCHEMA_VERSION,
        starting_balance: config.starting_balance,
        final_balance,
        profit_total_abs,
        total_volume,
        rejected_signals,
        maximum_concurrent_trades,
        locks: final_portfolio.protection_state.locks().to_vec(),
        trades: final_portfolio.closed_trades,
    };
    ensure_result_finite(&result)?;
    Ok(result)
}

#[allow(clippy::too_many_lines)] // Force exit is one ordered allocator/wallet transition.
fn force_exit_open_trades(
    mut state: FinalizationInput<'_, '_, '_, '_>,
) -> Result<FinalPortfolio, SimError> {
    let config = &state.input.config;
    let mut available_balance = wallet_free(
        config.starting_balance,
        &state.open_trades,
        &state.closed_trades,
    )?;
    let mut force_exit_index = 0_usize;
    while !state.open_trades.is_empty() {
        let before = scheduler_observer::state(
            available_balance,
            &state.open_trades,
            &state.closed_trades,
            config.max_open_trades,
            state.next_trade_id,
            state.next_order_id,
            state.rejected_signals,
        )?;
        let trade = state
            .open_trades
            .pop()
            .ok_or(SimError::InvalidCallbackRuntime)?;
        let frozen_price_step = trade.price_step;
        let pair_index = trade.pair_index;
        let pair = &state.input.pairs[pair_index];
        let last = pair
            .candles
            .try_last()?
            .ok_or_else(|| SimError::EmptyCandles(pair.pair.clone()))?;
        let (closed, _) = close_trade(
            trade,
            last.timestamp_ms,
            last.open,
            "force_exit".to_owned(),
            config,
            CloseTradeContext {
                sequence: state.closed_trades.len(),
                order_id: state.next_order_id,
                executable_callbacks: None,
                wallet_available_before: available_balance,
            },
        )?;
        let allocated_order_id = state.next_order_id;
        state.next_order_id += 1;
        let force_exit_trade_id = closed.id;
        let force_exit_order_ids = closed.orders.iter().map(|order| order.id).collect();
        state.closed_trades.push(closed);
        apply_force_exit_protection(&mut state.protection_state, &state.closed_trades, config)?;
        available_balance = wallet_free(
            config.starting_balance,
            &state.open_trades,
            &state.closed_trades,
        )?;
        let after = scheduler_observer::state(
            available_balance,
            &state.open_trades,
            &state.closed_trades,
            config.max_open_trades,
            state.next_trade_id,
            state.next_order_id,
            state.rejected_signals,
        )?;
        let mut detail = BoundaryDetail::plain();
        detail.allocated_order_id = Some(allocated_order_id);
        detail.force_exit_index = Some(force_exit_index);
        let mut event = scheduler_observer::event(
            state.portfolio_event_sequence,
            &BoundaryContext {
                timestamp_ms: last.timestamp_ms,
                pair: &pair.pair,
                configured_pair_index: pair_index,
                processing_order_index: force_exit_index,
            },
            PortfolioBoundary::ForceExit,
            before.clone(),
            after.clone(),
            detail,
        );
        event.force_exit_trade_id = Some(force_exit_trade_id);
        event.force_exit_order_ids = force_exit_order_ids;
        if let Some(callback) = state.portfolio_observer.as_deref_mut() {
            callback(&event);
        }
        if let Some(callback) = state.execution_observer.as_deref_mut() {
            let closed = state
                .closed_trades
                .last()
                .ok_or(SimError::InvalidCallbackRuntime)?;
            let order = closed
                .orders
                .last()
                .ok_or(SimError::InvalidCallbackRuntime)?;
            let execution_event = execution_observer::exit_fill_event(
                state.execution_event_sequence,
                ExitFillEventInput {
                    pair,
                    candle: &last,
                    closed,
                    order,
                    requested_rate: last.open,
                    frozen_price_step,
                    config,
                    state_before: before,
                    state_after: after,
                },
            );
            callback(&execution_event);
        }
        force_exit_index += 1;
    }
    Ok(FinalPortfolio {
        closed_trades: state.closed_trades,
        protection_state: state.protection_state,
    })
}

fn apply_force_exit_protection(
    protection_state: &mut ProtectionState,
    closed_trades: &[ClosedTrade],
    config: &crate::PortfolioConfig,
) -> Result<(), SimError> {
    if let (Some(program), Some(closed_trade)) = (&config.protection_program, closed_trades.last())
    {
        protection_state.after_trade_close(
            program,
            closed_trade,
            closed_trades,
            config.starting_balance,
        )?;
    }
    Ok(())
}

fn ensure_result_finite(result: &SimulationResult) -> Result<(), SimError> {
    if result.numbers_are_finite() {
        Ok(())
    } else {
        Err(SimError::ExactArithmetic {
            operation: "simulation-result",
        })
    }
}
