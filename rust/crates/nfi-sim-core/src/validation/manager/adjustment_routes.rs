//! Long and short system-adjustment route validation.

use std::collections::BTreeSet;

use crate::domain::{
    CompiledSystemAdjustmentSide, NfiX7AdjustmentConstants, NfiX7PositionAdjustment,
    NfiX7TradeManager,
};

use super::{
    uses_full_futures_manager_contract, valid_adjustment_source_callback,
    valid_nfi_adjustment_constants, valid_nfi_adjustment_policy,
    valid_versioned_system_adjustment_program,
};

pub(super) struct AdjustmentSummary {
    long: bool,
    short: bool,
}

impl AdjustmentSummary {
    pub(super) fn is_valid(&self) -> bool {
        self.long && self.short
    }
}

pub(super) fn summarize(
    manager: &NfiX7TradeManager,
    managed_tags: &BTreeSet<&String>,
    short_tags: &BTreeSet<&String>,
) -> AdjustmentSummary {
    AdjustmentSummary {
        long: valid_long(manager, managed_tags),
        short: valid_short(manager, short_tags),
    }
}

fn valid_long(manager: &NfiX7TradeManager, managed_tags: &BTreeSet<&String>) -> bool {
    manager
        .position_adjustment
        .as_ref()
        .is_none_or(|adjustment| {
            let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
            &adjustment_tags == managed_tags
                && adjustment_tags.len() == adjustment.entry_tags.len()
                && valid_common(manager, adjustment, CompiledSystemAdjustmentSide::Long)
                && valid_versioned_rebuy_multiplier(&manager.schema_version, adjustment)
                && valid_nfi_adjustment_constants(&adjustment.constants)
        })
}

fn valid_short(manager: &NfiX7TradeManager, short_tags: &BTreeSet<&String>) -> bool {
    let short_rebuy_tags = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")
        .into_iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let regular_tags = short_tags
        .difference(&short_rebuy_tags)
        .copied()
        .collect::<BTreeSet<_>>();
    if !uses_full_futures_manager_contract(&manager.schema_version) {
        return manager.short_position_adjustment.is_none();
    }
    manager
        .short_position_adjustment
        .as_ref()
        .is_some_and(|adjustment| {
            let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
            adjustment.enabled
                && adjustment_tags == regular_tags
                && adjustment_tags.len() == adjustment.entry_tags.len()
                && valid_common(manager, adjustment, CompiledSystemAdjustmentSide::Short)
                && adjustment
                    .constants
                    .rebuy_stake_multiplier
                    .is_some_and(|value| value.is_finite() && value > 0.0)
                && adjustment.constants.policy.as_ref().is_some_and(|policy| {
                    valid_nfi_adjustment_policy(
                        policy,
                        adjustment.constants.derisk_levels.len(),
                        adjustment.constants.grinds.len(),
                    )
                })
                && valid_nfi_adjustment_constants(&adjustment.constants)
        })
}

fn valid_common(
    manager: &NfiX7TradeManager,
    adjustment: &NfiX7PositionAdjustment,
    side: CompiledSystemAdjustmentSide,
) -> bool {
    let decision_program = match side {
        CompiledSystemAdjustmentSide::Long => "long_grind_entry_v3",
        CompiledSystemAdjustmentSide::Short => "short_grind_entry_v3",
    };
    adjustment.system_version == manager.constants.system_v3_2_name
        && valid_adjustment_source_callback(
            &manager.schema_version,
            adjustment.source_callback.as_deref(),
        )
        && adjustment.decision_program == decision_program
        && adjustment.program_order == adjustment_program_order(&adjustment.constants)
        && adjustment.stateful_input_contract.is_object()
        && valid_versioned_system_adjustment_program(
            &manager.schema_version,
            adjustment.program.as_ref(),
            adjustment,
            side,
        )
}

fn valid_versioned_rebuy_multiplier(
    schema_version: &str,
    adjustment: &NfiX7PositionAdjustment,
) -> bool {
    match schema_version {
        "0.9.0" => {
            adjustment.constants.rebuy_stake_multiplier.is_none()
                && adjustment.constants.policy.is_none()
        }
        "0.10.0" | "0.11.0" => {
            adjustment
                .constants
                .rebuy_stake_multiplier
                .is_some_and(|value| value.is_finite() && value > 0.0)
                && adjustment.constants.policy.is_none()
        }
        "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0" | "0.17.0" | "0.18.0" | "0.19.0"
        | "0.20.0" | "0.21.0" | "0.22.0" | "0.23.0" | "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0"
        | "0.28.0" | "0.29.0" | "0.30.0" => {
            adjustment
                .constants
                .rebuy_stake_multiplier
                .is_some_and(|value| value.is_finite() && value > 0.0)
                && adjustment.constants.policy.as_ref().is_some_and(|policy| {
                    valid_nfi_adjustment_policy(
                        policy,
                        adjustment.constants.derisk_levels.len(),
                        adjustment.constants.grinds.len(),
                    )
                })
        }
        _ => false,
    }
}

fn adjustment_program_order(constants: &NfiX7AdjustmentConstants) -> Vec<String> {
    constants
        .derisk_levels
        .iter()
        .map(|level| format!("derisk_level_{}", level.level))
        .chain(constants.grinds.iter().flat_map(|grind| {
            ["entry", "exit", "derisk"]
                .into_iter()
                .map(move |action| format!("grind_{}_{action}", grind.level))
        }))
        .collect()
}
