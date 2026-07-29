//! Stable aggregate profiling record construction.

use crate::SimulationProfile;

const SIMULATION_PROFILE_SCHEMA_VERSION: &str = "1.0.0";

pub(crate) const fn build_simulation_profile(
    validation_ns: u64,
    event_loop_ns: u64,
    finalization_ns: u64,
    timestamp_batches: u64,
    pair_events: u64,
) -> SimulationProfile {
    SimulationProfile {
        schema_version: SIMULATION_PROFILE_SCHEMA_VERSION,
        validation_ns,
        event_loop_ns,
        finalization_ns,
        timestamp_batches,
        pair_events,
    }
}
