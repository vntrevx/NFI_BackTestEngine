//! Source-selected decision fees, separate from actual portfolio accounting.

use crate::PortfolioConfig;

pub(crate) fn fee_open(config: &PortfolioConfig) -> f64 {
    config
        .nfi_x7_trade_manager
        .as_ref()
        .and_then(|manager| manager.virtual_fees.as_ref())
        .and_then(|fees| fees.open_rate)
        .unwrap_or_else(|| crate::calculations::fee_open(config))
}

pub(crate) fn fee_close(config: &PortfolioConfig) -> f64 {
    config
        .nfi_x7_trade_manager
        .as_ref()
        .and_then(|manager| manager.virtual_fees.as_ref())
        .and_then(|fees| fees.close_rate)
        .unwrap_or_else(|| crate::calculations::fee_close(config))
}
