//! Fail-closed validation modules for simulator inputs and compiled contracts.

mod adjustment;
mod callback;
mod config;
mod manager;
mod pair;
mod routing;

pub(crate) use config::validate_input;
pub use config::validate_simulator_preflight;
#[cfg(test)]
pub(crate) use manager::{
    valid_nfi_managed_long_route, valid_system_adjustment_binding_level,
    valid_versioned_legacy_grind_program,
};
pub(crate) use pair::freqtrade_entry_signal;
pub(crate) use routing::{
    nfi_entry_signal_is_supported, nfi_managed_route_supports_tags,
    nfi_managed_short_route_supports_tags,
};
