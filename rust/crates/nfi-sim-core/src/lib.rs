//! Deterministic global chronological portfolio simulator.
//!
//! Signals cross this boundary as complete arrays. The core never calls Python
//! per candle and never simulates pairs independently before merging results.

#[cfg(test)]
use std::collections::BTreeMap;

#[cfg(test)]
use num_traits::ToPrimitive;
#[cfg(test)]
use serde_json::Value;

mod calculations;
mod callbacks;
#[cfg(test)]
use calculations::{
    ceil_step, entry_sizing, exact_rational, fee_close, fee_open, floor_step, ft_precise_division,
    pairwise_sum, precise_product, precise_product_quotient, python_float_sum, round_eight,
    round_step,
};
mod domain;
mod io;
#[cfg(test)]
use io::CALLBACK_FEATURE_LOOKBACK_ROWS;
pub use io::{
    parse_simulation_input, serialize_simulation_result, CandleSeries, CandleSeriesIter,
    FeatureColumn, FileBackedFeatureKind, FileBackedRows, FILE_BACKED_FEATURE_BYTES,
    FILE_BACKED_ROW_HEADER_BYTES, TRADE_SURFACE_SCHEMA_VERSION,
};
mod portfolio;
mod profiling;
mod scalar_vm;
mod state_machine_vm;
mod validation;
#[cfg(test)]
use portfolio::TradeSide;
#[cfg(test)]
use scalar_vm::evaluate_scalar_program_bundle_from_base;
pub use scalar_vm::{evaluate_scalar_decision_program, evaluate_scalar_program_bundle};
pub use state_machine_vm::{
    evaluate_state_machine, validate_state_machine_program, StateMachineAction,
    StateMachineContext, StateMachineError,
};
mod futures;
#[cfg(test)]
use futures::evaluate_nfi_leverage;
mod execution;
pub use domain::*;
#[cfg(test)]
use execution::{
    adjustment_minimum_pair_stake, enter_trade, evaluate_confirm_program,
    evaluate_exit_confirm_program, evaluate_stake_program, evaluate_state_machine_adjustment,
    evaluate_state_machine_exit, exit_decision, minimum_pair_stake, pair_price_step, ConfirmInputs,
    EntryRequest, EntryStake, StakeInputs,
};
#[cfg(test)]
use validation::nfi_managed_short_route_supports_tags;

mod nfi;
#[cfg(test)]
use callbacks::{callback_feature_index, insert_feature_window};
#[cfg(test)]
use nfi::{nfi_inline_profile_exit, nfi_profit_snapshot, NfiProfitSnapshot};
mod protections;
#[cfg(test)]
use protections::{PairLockState, ProtectionState};
mod simulation;
use simulation::simulate_internal;

/// Version of the simulator input/result contract.
pub const SIMULATOR_SCHEMA_VERSION: &str = "1.0.0";
/// Reports whether the compiled chronological simulator is present.
#[must_use]
pub const fn simulator_available() -> bool {
    true
}

/// Validate and run one global portfolio stream.
///
/// # Errors
///
/// Returns [`SimError`] when the version, configuration, candle ordering,
/// OHLCV values, or adjustment request cannot be represented exactly by this
/// supported simulator subset.
///
/// # Panics
///
/// Panics only if an internally created open trade points outside the already
/// validated immutable pair array. Public input cannot construct that state.
#[allow(clippy::too_many_lines)]
pub fn simulate(input: &SimulationInput) -> Result<SimulationResult, SimError> {
    simulate_internal(input, None).map(|(result, _)| result)
}

/// Run the simulator and stream one compact state projection after each
/// Freqtrade-visible pair candle. Freqtrade reserves the first row for shifted
/// signals and does expose the final row before its separate force-exit pass.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<SimulationResult, SimError>
where
    F: FnMut(&SimulationEvent),
{
    simulate_internal(input, Some(&mut observer)).map(|(result, _)| result)
}

/// Run the simulator and return aggregate phase timings beside the result.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
pub fn simulate_profiled(
    input: &SimulationInput,
) -> Result<(SimulationResult, SimulationProfile), SimError> {
    simulate_internal(input, None)
}

/// Run with an observer and return aggregate phase timings.
///
/// # Errors
///
/// Returns the same validation and semantic errors as [`simulate`].
///
/// # Panics
///
/// Has the same internal invariant boundary as [`simulate`].
#[allow(clippy::if_not_else, clippy::too_many_lines)]
pub fn simulate_with_observer_profiled<F>(
    input: &SimulationInput,
    mut observer: F,
) -> Result<(SimulationResult, SimulationProfile), SimError>
where
    F: FnMut(&SimulationEvent),
{
    simulate_internal(input, Some(&mut observer))
}

#[cfg(test)]
#[path = "tests/mod.rs"]
#[allow(clippy::float_cmp)] // These tests assert exact Freqtrade float tokens.
mod tests;
