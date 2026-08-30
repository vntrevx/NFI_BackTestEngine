//! Long/short grind and regular long-BTC route validation.

use std::collections::BTreeSet;

use crate::domain::{CompiledLegacyGrindSide, NfiLongGrindRoute, NfiX7TradeManager};

use super::{
    valid_legacy_futures_fallback, valid_nfi_legacy_grind_constants,
    valid_nfi_regular_adjustment_constants, valid_versioned_legacy_grind_program,
    valid_versioned_regular_adjustment_program,
};

pub(super) struct GrindSummary {
    long: bool,
    short: bool,
    long_btc: bool,
}

impl GrindSummary {
    pub(super) fn is_valid(&self) -> bool {
        self.long && self.short && self.long_btc
    }
}

pub(super) fn summarize(
    manager: &NfiX7TradeManager,
    managed_tags: &BTreeSet<&String>,
    short_tags: &BTreeSet<&String>,
) -> GrindSummary {
    GrindSummary {
        long: valid_long_grind(manager, managed_tags),
        short: valid_short_grind(manager, managed_tags, short_tags),
        long_btc: valid_long_btc(manager, managed_tags),
    }
}

fn valid_long_grind(manager: &NfiX7TradeManager, managed_tags: &BTreeSet<&String>) -> bool {
    manager.long_grind.as_ref().is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let scope_is_valid = match route.adjustment_scope.as_str() {
            "spot-grind-backtest-v1" => true,
            "grind-backtest-v2" => matches!(
                manager.schema_version.as_str(),
                "0.14.0"
                    | "0.15.0"
                    | "0.16.0"
                    | "0.17.0"
                    | "0.18.0"
                    | "0.19.0"
                    | "0.20.0"
                    | "0.21.0"
                    | "0.22.0"
                    | "0.23.0"
                    | "0.24.0"
                    | "0.25.0"
                    | "0.26.0"
                    | "0.27.0"
                    | "0.28.0"
                    | "0.29.0"
                    | "0.30.0"
                    | "0.31.0"
            ),
            _ => false,
        };
        valid_common_route(route, &route_tags)
            && route_tags.iter().all(|tag| !managed_tags.contains(*tag))
            && scope_is_valid
            && route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.regular_decision_program.is_none()
            && route.regular_constants.is_none()
            && route.regular_program.is_none()
            && valid_versioned_legacy_grind_program(&manager.schema_version, route)
            && valid_nfi_legacy_grind_constants(&route.constants)
    })
}

fn valid_short_grind(
    manager: &NfiX7TradeManager,
    managed_tags: &BTreeSet<&String>,
    short_tags: &BTreeSet<&String>,
) -> bool {
    if !matches!(manager.schema_version.as_str(), "0.30.0" | "0.31.0") {
        return manager.short_grind.is_none();
    }
    manager.short_grind.as_ref().is_some_and(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        valid_common_route(route, &route_tags)
            && route_tags
                .iter()
                .all(|tag| !managed_tags.contains(*tag) && !short_tags.contains(*tag))
            && route.adjustment_scope == "grind-backtest-v2"
            && route.grind_mode
            && route.decision_program == "short_grind_entry_v3"
            && route
                .futures_fallback_loss_threshold
                .is_some_and(|threshold| threshold.is_finite() && threshold > 0.0)
            && route.regular_decision_program.is_none()
            && route.regular_constants.is_none()
            && route.regular_program.is_none()
            && route
                .program
                .as_ref()
                .is_some_and(|program| program.side == CompiledLegacyGrindSide::Short)
            && valid_versioned_legacy_grind_program(&manager.schema_version, route)
            && valid_nfi_legacy_grind_constants(&route.constants)
    })
}

fn valid_long_btc(manager: &NfiX7TradeManager, managed_tags: &BTreeSet<&String>) -> bool {
    let grind_tags = manager
        .long_grind
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    manager.long_btc.as_ref().is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        valid_common_route(route, &route_tags)
            && route_tags
                .iter()
                .all(|tag| !managed_tags.contains(*tag) && !grind_tags.contains(*tag))
            && route.adjustment_scope == "regular-backtest-v2"
            && !route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.regular_decision_program.as_deref() == Some("long_grind_entry")
            && route
                .regular_constants
                .as_ref()
                .is_some_and(valid_nfi_regular_adjustment_constants)
            && if matches!(
                manager.schema_version.as_str(),
                "0.27.0" | "0.28.0" | "0.29.0" | "0.30.0" | "0.31.0"
            ) {
                valid_versioned_legacy_grind_program(&manager.schema_version, route)
            } else {
                route.program.is_none()
            }
            && valid_versioned_regular_adjustment_program(&manager.schema_version, route)
            && valid_nfi_legacy_grind_constants(&route.constants)
    })
}

fn valid_common_route(route: &NfiLongGrindRoute, route_tags: &BTreeSet<&String>) -> bool {
    !route.mode_name.is_empty()
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && route.exit_profit_threshold.is_finite()
        && route.exit_profit_threshold > 0.0
        && route.first_entry_profit_threshold_spot.is_finite()
        && route.first_entry_profit_threshold_spot > 0.0
        && route.first_entry_stop_threshold_spot.is_finite()
        && route.first_entry_stop_threshold_spot < 0.0
        && route.stateful_input_contract.is_object()
}
