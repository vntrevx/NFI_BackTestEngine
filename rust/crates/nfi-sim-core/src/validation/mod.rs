//! Fail-closed validation modules for simulator inputs and compiled contracts.

mod adjustment;
mod callback;
mod config;
mod manager;
mod pair;
mod routing;

pub(crate) use config::validate_input;
#[cfg(test)]
pub(crate) use manager::valid_nfi_managed_long_route;
pub(crate) use pair::freqtrade_entry_signal;
pub(crate) use routing::{
    nfi_entry_signal_is_supported, nfi_managed_route_supports_tags,
    nfi_managed_short_route_supports_tags,
};
