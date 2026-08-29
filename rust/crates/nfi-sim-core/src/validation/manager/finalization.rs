//! Manager constants, callback system write, and runtime finalization.

use crate::domain::{NfiX7TradeManager, PortfolioConfig};

pub(super) fn static_contract_is_valid(
    config: &PortfolioConfig,
    manager: &NfiX7TradeManager,
) -> bool {
    let constants = &manager.constants;
    let thresholds = [
        constants.stop_threshold_futures,
        constants.stop_threshold_spot,
        constants.system_v3_2_stop_threshold_doom_futures,
        constants.system_v3_2_stop_threshold_doom_spot,
    ];
    let constants_are_valid = !constants.system_name_use.is_empty()
        && constants.system_name_use == constants.system_v3_2_name
        && thresholds
            .iter()
            .all(|threshold| threshold.is_finite() && *threshold >= 0.0);
    let has_system_write = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
        .is_some_and(|program| {
            program.initial_successful_entry_writes.iter().any(|write| {
                write.key == "system_version"
                    && write.value.as_str() == Some(constants.system_name_use.as_str())
            })
        });
    constants_are_valid && has_system_write && config.custom_exit_program.is_none()
}

pub(super) fn runtime_is_valid(manager: &NfiX7TradeManager) -> bool {
    manager.initialize_feature_projection_caches() && manager.runtime_dispatch().is_some()
}
