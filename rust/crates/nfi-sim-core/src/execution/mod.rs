//! Exact entry, fill, adjustment, order replay, and close execution modules.

mod confirmation;
mod entry;
mod exit;
mod position;
mod stake;

pub(crate) use confirmation::evaluate_exit_confirm_program;
#[cfg(test)]
pub(crate) use confirmation::{evaluate_confirm_program, ConfirmInputs};
pub(crate) use entry::{adjustment_minimum_pair_stake, EntryExecution};
#[cfg(test)]
pub(crate) use entry::{enter_trade, minimum_pair_stake, pair_price_step};
pub(crate) use exit::{close_trade, current_profit_ratio, exit_decision, rule_adjustment};
pub(crate) use position::{apply_adjustment, update_extrema};
#[cfg(test)]
pub(crate) use stake::{evaluate_stake_program, EntryRequest, EntryStake, StakeInputs};
