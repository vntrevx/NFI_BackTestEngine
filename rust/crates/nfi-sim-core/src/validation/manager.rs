//! NFI trade-manager schema and route validation.

use std::collections::BTreeSet;

use crate::domain::{
    NfiManagedLongProfile, NfiManagedLongRoute, NfiX7AdjustmentConstants, NfiX7TradeManager,
    PortfolioConfig, SimError,
};

use super::adjustment::{
    valid_nfi_adjustment_constants, valid_nfi_adjustment_policy, valid_nfi_legacy_grind_constants,
    valid_nfi_rebuy_constants, valid_nfi_regular_adjustment_constants,
};
use super::config::{
    uses_full_futures_manager_contract, valid_legacy_futures_fallback, valid_scalar_program,
};

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

#[allow(clippy::too_many_lines)] // One fail-closed audit keeps all route invariants co-located.
pub(crate) fn validate_nfi_trade_manager(
    config: &PortfolioConfig,
    manager: &NfiX7TradeManager,
) -> Result<(), SimError> {
    const PROGRAM_ORDER: [&str; 4] = [
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    ];
    const SHORT_PROGRAM_ORDER: [&str; 4] = [
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
        "short_exit_dec",
    ];
    let long_grind = manager.long_grind.as_ref();
    let long_btc = manager.long_btc.as_ref();
    let adjustment = manager.position_adjustment.as_ref();
    let short_adjustment = manager.short_position_adjustment.as_ref();
    let constants = &manager.constants;
    let managed_keys = manager
        .managed_long_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let expected_managed_keys = [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_top_coins",
        "long_scalp",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let managed_tags = manager
        .managed_long_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let total_managed_tag_count = manager
        .managed_long_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    let short_keys = manager
        .managed_short_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let short_tags = manager
        .managed_short_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let total_short_tag_count = manager
        .managed_short_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    let valid_identity = matches!(
        manager.schema_version.as_str(),
        "0.9.0" | "0.10.0" | "0.11.0" | "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0"
    ) && manager.source_sha256.len() == 64
        && manager
            .source_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    let valid_managed_routes = manager.managed_long_routes.len() == expected_managed_keys.len()
        && managed_keys == expected_managed_keys
        && managed_tags.len() == total_managed_tag_count
        && manager
            .managed_long_routes
            .iter()
            .all(valid_nfi_managed_long_route);
    let valid_terminal_exit_version = matches!(
        manager.schema_version.as_str(),
        "0.11.0" | "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0"
    ) || manager
        .managed_long_routes
        .iter()
        .all(|route| route.terminal_exit.is_none());
    let expected_short_order = if uses_full_futures_manager_contract(&manager.schema_version) {
        vec![
            "short_normal",
            "short_pump",
            "short_quick",
            "short_rebuy",
            "short_high_profit",
            "short_rapid",
            "short_scalp",
            "short_top_coins_fallback",
        ]
    } else {
        vec!["short_rebuy"]
    };
    let expected_short_keys = expected_short_order
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    let valid_short_routes = manager.managed_short_routes.len() == expected_short_keys.len()
        && short_keys == expected_short_keys
        && short_tags.len() == total_short_tag_count
        && short_tags.iter().all(|tag| !managed_tags.contains(*tag))
        && manager
            .managed_short_routes
            .iter()
            .all(valid_nfi_managed_short_route)
        && manager.short_route_order
            == expected_short_order
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>();
    let expected_route_order = [
        "long_normal",
        "long_pump",
        "long_quick",
        "long_rebuy",
        "long_high_profit",
        "long_rapid",
        "long_grind",
        "long_btc",
        "long_top_coins",
        "long_scalp",
    ]
    .into_iter()
    .filter(|key| {
        managed_keys.contains(key)
            || (*key == "long_grind" && long_grind.is_some())
            || (*key == "long_btc" && long_btc.is_some())
    })
    .map(ToOwned::to_owned)
    .collect::<Vec<_>>();
    let valid_route_order = manager.route_order == expected_route_order;
    let valid_long_grind = long_grind.is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let tags_are_disjoint = route_tags.iter().all(|tag| !managed_tags.contains(*tag));
        !route.mode_name.is_empty()
            && !route.entry_tags.is_empty()
            && route_tags.len() == route.entry_tags.len()
            && route.entry_tags.iter().all(|tag| !tag.is_empty())
            && tags_are_disjoint
            && route.exit_profit_threshold.is_finite()
            && route.exit_profit_threshold > 0.0
            && match route.adjustment_scope.as_str() {
                // Preserve the narrower replay contract of older evidence.
                "spot-grind-backtest-v1" => true,
                "grind-backtest-v2" => {
                    matches!(
                        manager.schema_version.as_str(),
                        "0.14.0" | "0.15.0" | "0.16.0"
                    )
                }
                _ => false,
            }
            && route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.stateful_input_contract.is_object()
            && route.regular_decision_program.is_none()
            && route.regular_constants.is_none()
            && valid_nfi_legacy_grind_constants(&route.constants)
    });
    let grind_tags = long_grind
        .into_iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let valid_long_btc = long_btc.is_none_or(|route| {
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let tags_are_disjoint = route_tags
            .iter()
            .all(|tag| !managed_tags.contains(*tag) && !grind_tags.contains(*tag));
        !route.mode_name.is_empty()
            && !route.entry_tags.is_empty()
            && route_tags.len() == route.entry_tags.len()
            && route.entry_tags.iter().all(|tag| !tag.is_empty())
            && tags_are_disjoint
            && route.exit_profit_threshold.is_finite()
            && route.exit_profit_threshold > 0.0
            && route.adjustment_scope == "regular-backtest-v2"
            && !route.grind_mode
            && route.decision_program == "long_grind_entry_v3"
            && route.first_entry_profit_threshold_spot.is_finite()
            && route.first_entry_profit_threshold_spot > 0.0
            && route.first_entry_stop_threshold_spot.is_finite()
            && route.first_entry_stop_threshold_spot < 0.0
            && valid_legacy_futures_fallback(route, &manager.schema_version)
            && route.stateful_input_contract.is_object()
            && route.regular_decision_program.as_deref() == Some("long_grind_entry")
            && route
                .regular_constants
                .as_ref()
                .is_some_and(valid_nfi_regular_adjustment_constants)
            && valid_nfi_legacy_grind_constants(&route.constants)
    });
    let valid_programs = manager.programs.len()
        == PROGRAM_ORDER.len()
            + SHORT_PROGRAM_ORDER.len()
            + usize::from(adjustment.is_some())
            + usize::from(short_adjustment.is_some())
            + usize::from(long_btc.is_some())
        && PROGRAM_ORDER.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && SHORT_PROGRAM_ORDER.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && long_btc.is_none_or(|route| {
            route
                .regular_decision_program
                .as_ref()
                .is_some_and(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
        })
        && adjustment.is_none_or(|adjustment| {
            manager
                .programs
                .get(&adjustment.decision_program)
                .is_some_and(valid_scalar_program)
        })
        && short_adjustment.is_none_or(|adjustment| {
            manager
                .programs
                .get(&adjustment.decision_program)
                .is_some_and(valid_scalar_program)
        });
    let valid_adjustment_route = adjustment.is_none_or(|adjustment| {
        let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let versioned_rebuy_multiplier = match manager.schema_version.as_str() {
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
            "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0" => {
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
        };
        adjustment_tags == managed_tags
            && adjustment_tags.len() == adjustment.entry_tags.len()
            && adjustment.system_version == constants.system_v3_2_name
            && adjustment.decision_program == "long_grind_entry_v3"
            && adjustment.program_order == adjustment_program_order(&adjustment.constants)
            && adjustment.stateful_input_contract.is_object()
            && versioned_rebuy_multiplier
            && valid_nfi_adjustment_constants(&adjustment.constants)
    });
    let short_rebuy_tags = manager
        .managed_short_routes
        .iter()
        .find(|route| route.key == "short_rebuy")
        .into_iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let regular_short_tags = short_tags
        .difference(&short_rebuy_tags)
        .copied()
        .collect::<BTreeSet<_>>();
    let valid_short_adjustment_route =
        if uses_full_futures_manager_contract(&manager.schema_version) {
            short_adjustment.is_some_and(|adjustment| {
                let adjustment_tags = adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
                adjustment.enabled
                    && adjustment_tags == regular_short_tags
                    && adjustment_tags.len() == adjustment.entry_tags.len()
                    && adjustment.system_version == constants.system_v3_2_name
                    && adjustment.decision_program == "short_grind_entry_v3"
                    && adjustment.program_order == adjustment_program_order(&adjustment.constants)
                    && adjustment.stateful_input_contract.is_object()
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
        } else {
            short_adjustment.is_none()
        };
    let rebuy_route = manager
        .managed_long_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let rebuy_adjustment = &manager.rebuy_adjustment;
    let valid_rebuy_adjustment = rebuy_route.is_some_and(|route| {
        let adjustment_tags = rebuy_adjustment.entry_tags.iter().collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        rebuy_adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == rebuy_adjustment.entry_tags.len()
            && rebuy_adjustment.system_version == constants.system_v3_2_name
            && rebuy_adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&rebuy_adjustment.constants)
    });
    let short_rebuy_route = manager
        .managed_short_routes
        .iter()
        .find(|route| route.profile == NfiManagedLongProfile::Rebuy);
    let short_rebuy_adjustment = &manager.short_rebuy_adjustment;
    let valid_short_rebuy_adjustment = short_rebuy_route.is_some_and(|route| {
        let adjustment_tags = short_rebuy_adjustment
            .entry_tags
            .iter()
            .collect::<BTreeSet<_>>();
        let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
        let valid_scope = if uses_full_futures_manager_contract(&manager.schema_version) {
            short_rebuy_adjustment.execution_scope == "rebuy-and-grind-v2"
                && short_rebuy_adjustment.post_derisk_action == "short-position-adjustment"
        } else {
            short_rebuy_adjustment.execution_scope == "pre-derisk-only-v1"
                && short_rebuy_adjustment.post_derisk_action == "fail-simulation"
        };
        short_rebuy_adjustment.enabled
            && adjustment_tags == route_tags
            && adjustment_tags.len() == short_rebuy_adjustment.entry_tags.len()
            && short_rebuy_adjustment.system_version == constants.system_v3_2_name
            && valid_scope
            && short_rebuy_adjustment.stateful_input_contract.is_object()
            && valid_nfi_rebuy_constants(&short_rebuy_adjustment.constants)
    });
    let thresholds = [
        constants.stop_threshold_futures,
        constants.stop_threshold_spot,
        constants.system_v3_2_stop_threshold_doom_futures,
        constants.system_v3_2_stop_threshold_doom_spot,
    ];
    let valid_constants = !constants.system_name_use.is_empty()
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
    if !valid_identity
        || !valid_managed_routes
        || !valid_terminal_exit_version
        || !valid_short_routes
        || !valid_route_order
        || !valid_long_grind
        || !valid_long_btc
        || !valid_programs
        || !valid_adjustment_route
        || !valid_short_adjustment_route
        || !valid_rebuy_adjustment
        || !valid_short_rebuy_adjustment
        || !valid_constants
        || !has_system_write
        || config.custom_exit_program.is_some()
    {
        return Err(SimError::InvalidNfiTradeManager);
    }
    Ok(())
}

fn valid_nfi_managed_short_route(route: &NfiManagedLongRoute) -> bool {
    let profile_matches_key = matches!(
        (route.key.as_str(), route.profile),
        (
            "short_normal" | "short_top_coins_fallback",
            NfiManagedLongProfile::Normal
        ) | ("short_pump", NfiManagedLongProfile::Pump)
            | ("short_quick", NfiManagedLongProfile::Quick)
            | ("short_rebuy", NfiManagedLongProfile::Rebuy)
            | ("short_high_profit", NfiManagedLongProfile::HighProfit)
            | ("short_rapid", NfiManagedLongProfile::Rapid)
            | ("short_scalp", NfiManagedLongProfile::Scalp)
    );
    let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
    let stop_thresholds_are_valid = match route.profile {
        NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::Rapid
        | NfiManagedLongProfile::Scalp => {
            route
                .stop_threshold_futures
                .is_some_and(|value| value.is_finite() && value >= 0.0)
                && route
                    .stop_threshold_spot
                    .is_some_and(|value| value.is_finite() && value >= 0.0)
        }
        _ => route.stop_threshold_futures.is_none() && route.stop_threshold_spot.is_none(),
    };
    profile_matches_key
        && !route.mode_name.is_empty()
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && stop_thresholds_are_valid
        && route.terminal_exit.is_none()
}

pub(crate) fn valid_nfi_managed_long_route(route: &NfiManagedLongRoute) -> bool {
    let profile_matches_key = matches!(
        (route.key.as_str(), route.profile),
        ("long_normal", NfiManagedLongProfile::Normal)
            | ("long_pump", NfiManagedLongProfile::Pump)
            | ("long_quick", NfiManagedLongProfile::Quick)
            | ("long_rebuy", NfiManagedLongProfile::Rebuy)
            | ("long_high_profit", NfiManagedLongProfile::HighProfit)
            | ("long_rapid", NfiManagedLongProfile::Rapid)
            | ("long_top_coins", NfiManagedLongProfile::TopCoins)
            | ("long_scalp", NfiManagedLongProfile::Scalp)
    );
    let route_tags = route.entry_tags.iter().collect::<BTreeSet<_>>();
    let terminal_exit_is_valid = route.terminal_exit.as_ref().is_none_or(|terminal| {
        let terminal_tags = terminal.entry_tags.iter().collect::<BTreeSet<_>>();
        route.profile == NfiManagedLongProfile::Rebuy
            && !terminal.entry_tags.is_empty()
            && terminal_tags.len() == terminal.entry_tags.len()
            && terminal_tags.iter().all(|tag| route_tags.contains(*tag))
            && terminal.minimum_age_ms > 0
            && terminal.minimum_profit_ratio.is_finite()
            && !terminal.reason.is_empty()
    });
    let stop_thresholds_are_valid = match route.profile {
        NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::Rapid
        | NfiManagedLongProfile::Scalp => {
            route
                .stop_threshold_futures
                .is_some_and(|value| value.is_finite() && value >= 0.0)
                && route
                    .stop_threshold_spot
                    .is_some_and(|value| value.is_finite() && value >= 0.0)
        }
        _ => route.stop_threshold_futures.is_none() && route.stop_threshold_spot.is_none(),
    };
    profile_matches_key
        && !route.mode_name.is_empty()
        && !route.entry_tags.is_empty()
        && route_tags.len() == route.entry_tags.len()
        && route.entry_tags.iter().all(|tag| !tag.is_empty())
        && stop_thresholds_are_valid
        && terminal_exit_is_valid
}
