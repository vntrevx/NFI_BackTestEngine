//! Exact entry, fill, adjustment, order replay, and close execution modules.

mod callback_trace;
mod confirmation;
mod entry;
pub(crate) mod executable_callback;
mod exit;
mod position;
mod stake;
mod state_machine;

pub(crate) use callback_trace::record_trade as trace_trade_callback;
pub(crate) use callback_trace::{
    begin as begin_callback_trace, finish as finish_callback_trace, ExecutableCallbacks,
};
pub(crate) use confirmation::evaluate_exit_confirm_program;
#[cfg(test)]
pub(crate) use confirmation::{evaluate_confirm_program, ConfirmInputs};
#[cfg(test)]
pub(crate) use entry::enter_trade;
pub(crate) use entry::{
    adjustment_minimum_pair_stake, executable_order_filled, minimum_pair_stake, pair_price_step,
    EntryExecution,
};
#[cfg(test)]
pub(crate) use exit::exit_decision;
pub(crate) use exit::{
    close_trade, current_profit_ratio, executable_custom_exit, executable_custom_stoploss,
    executable_exit_confirmation, exit_decisions, ordered_risk_candidates, rule_adjustment,
    CloseTradeContext, ExitDecision,
};
#[cfg(test)]
pub(crate) use position::replay_spot_profit;
pub(crate) use position::{apply_adjustment, executable_position_adjustment, update_extrema};
#[cfg(test)]
pub(crate) use stake::{evaluate_stake_program, EntryRequest, EntryStake, StakeInputs};
pub(crate) use state_machine::evaluate_state_machine_adjustment;
#[cfg(test)]
pub(crate) use state_machine::evaluate_state_machine_exit;
