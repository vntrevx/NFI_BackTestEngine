//! Long and short rebuy-route adjustment validation.

use std::collections::BTreeSet;

use crate::domain::{NfiManagedLongProfile, NfiX7TradeManager};

use super::{
    uses_full_futures_manager_contract, valid_nfi_rebuy_constants, valid_versioned_rebuy_program,
};

pub(super) struct RebuySummary {
    long: bool,
    short: bool,
}

impl RebuySummary {
    pub(super) fn is_valid(&self) -> bool {
        self.long && self.short
    }
}

pub(super) fn summarize(manager: &NfiX7TradeManager) -> RebuySummary {
    RebuySummary {
        long: valid_long(manager),
        short: valid_short(manager),
    }
}

fn valid_long(manager: &NfiX7TradeManager) -> bool {
    let route = manager
        .managed_long_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let adjustment = &manager.rebuy_adjustment;
    route.is_some_and(|route| {
        let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == adjustment.entry_tags.len()
            && adjustment.system_version == manager.constants.system_v3_2_name
            && adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&adjustment.constants)
            && valid_versioned_rebuy_program(
                &manager.schema_version,
                adjustment.program.as_ref(),
                manager
                    .position_adjustment
                    .as_ref()
                    .and_then(|value| value.constants.policy.as_ref()),
                manager
                    .position_adjustment
                    .as_ref()
                    .and_then(|value| value.source_callback.as_deref()),
            )
    })
}

fn valid_short(manager: &NfiX7TradeManager) -> bool {
    let route = manager
        .managed_short_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let adjustment = &manager.short_rebuy_adjustment;
    route.is_some_and(|route| {
        let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let valid_scope = if uses_full_futures_manager_contract(&manager.schema_version) {
            adjustment.execution_scope == "rebuy-and-grind-v2"
                && adjustment.post_derisk_action == "short-position-adjustment"
        } else {
            adjustment.execution_scope == "pre-derisk-only-v1"
                && adjustment.post_derisk_action == "fail-simulation"
        };
        adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == adjustment.entry_tags.len()
            && adjustment.system_version == manager.constants.system_v3_2_name
            && valid_scope
            && adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&adjustment.constants)
            && valid_versioned_rebuy_program(
                &manager.schema_version,
                adjustment.program.as_ref(),
                manager
                    .short_position_adjustment
                    .as_ref()
                    .and_then(|value| value.constants.policy.as_ref()),
                manager
                    .short_position_adjustment
                    .as_ref()
                    .and_then(|value| value.source_callback.as_deref()),
            )
    })
}
