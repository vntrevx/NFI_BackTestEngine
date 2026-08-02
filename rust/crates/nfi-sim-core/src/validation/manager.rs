//! NFI trade-manager schema and route validation.

use std::collections::BTreeSet;

use crate::domain::{
    CompiledAdjustmentExecutionMode, CompiledAdjustmentOperation, CompiledAdjustmentProgram,
    CompiledLegacyEntryStakeBasis, CompiledLegacyExitStakeBasis, CompiledLegacyGrindExecutionMode,
    CompiledLegacyGrindTransition, CompiledLegacyRetryPolicy, CompiledLegacyThresholdDivisor,
    CompiledLegacyWalletGuard, CompiledOrderSequence, CompiledOrderSide, CompiledPartialFillPolicy,
    CompiledRegularContinuationGuard, CompiledRegularContinuationKind,
    CompiledRegularExecutionMode, CompiledRegularTransition, CompiledSystemAdjustmentActionKind,
    CompiledSystemAdjustmentExecutionMode, CompiledSystemAdjustmentInputKind,
    CompiledSystemAdjustmentProgram, CompiledSystemAdjustmentSide, ManagedExitExecutionMode,
    ManagedExitInlinePosition, ManagedExitRoute, ManagedExitStateOperation,
    ManagedExitStateProgram, ManagedExitStopPolicy, ManagedExitTagMatcher, ManagedExitTagOperator,
    NfiLongGrindRoute, NfiManagedLongProfile, NfiManagedLongRoute, NfiX7AdjustmentConstants,
    NfiX7AdjustmentPolicy, NfiX7PositionAdjustment, NfiX7TradeManager, PortfolioConfig, SimError,
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
        "0.9.0"
            | "0.10.0"
            | "0.11.0"
            | "0.12.0"
            | "0.13.0"
            | "0.14.0"
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
        "0.11.0"
            | "0.12.0"
            | "0.13.0"
            | "0.14.0"
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
    let valid_route_order = if matches!(
        manager.schema_version.as_str(),
        "0.17.0" | "0.18.0" | "0.19.0"
    ) {
        manager.route_order.len() == expected_route_order.len()
            && manager.route_order.iter().collect::<BTreeSet<_>>()
                == expected_route_order.iter().collect::<BTreeSet<_>>()
    } else {
        manager.route_order == expected_route_order
    };
    let valid_managed_exit_program = valid_managed_exit_program(manager);
    let valid_managed_short_exit_program = valid_managed_short_exit_program(manager);
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
            && route.regular_program.is_none()
            && valid_versioned_legacy_grind_program(&manager.schema_version, route)
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
            && if matches!(manager.schema_version.as_str(), "0.27.0" | "0.28.0") {
                valid_versioned_legacy_grind_program(&manager.schema_version, route)
            } else {
                route.program.is_none()
            }
            && valid_versioned_regular_adjustment_program(&manager.schema_version, route)
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
            "0.12.0" | "0.13.0" | "0.14.0" | "0.15.0" | "0.16.0" | "0.17.0" | "0.18.0"
            | "0.19.0" | "0.20.0" | "0.21.0" | "0.22.0" | "0.23.0" | "0.24.0" | "0.25.0"
            | "0.26.0" | "0.27.0" | "0.28.0" => {
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
            && valid_adjustment_source_callback(
                &manager.schema_version,
                adjustment.source_callback.as_deref(),
            )
            && adjustment.decision_program == "long_grind_entry_v3"
            && adjustment.program_order == adjustment_program_order(&adjustment.constants)
            && adjustment.stateful_input_contract.is_object()
            && valid_versioned_system_adjustment_program(
                &manager.schema_version,
                adjustment.program.as_ref(),
                adjustment,
                CompiledSystemAdjustmentSide::Long,
            )
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
                    && valid_adjustment_source_callback(
                        &manager.schema_version,
                        adjustment.source_callback.as_deref(),
                    )
                    && adjustment.decision_program == "short_grind_entry_v3"
                    && adjustment.program_order == adjustment_program_order(&adjustment.constants)
                    && adjustment.stateful_input_contract.is_object()
                    && valid_versioned_system_adjustment_program(
                        &manager.schema_version,
                        adjustment.program.as_ref(),
                        adjustment,
                        CompiledSystemAdjustmentSide::Short,
                    )
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
            && valid_versioned_rebuy_program(
                &manager.schema_version,
                rebuy_adjustment.program.as_ref(),
                adjustment.and_then(|value| value.constants.policy.as_ref()),
                adjustment.and_then(|value| value.source_callback.as_deref()),
            )
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
            && valid_versioned_rebuy_program(
                &manager.schema_version,
                short_rebuy_adjustment.program.as_ref(),
                short_adjustment.and_then(|value| value.constants.policy.as_ref()),
                short_adjustment.and_then(|value| value.source_callback.as_deref()),
            )
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
        || !valid_managed_exit_program
        || !valid_managed_short_exit_program
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

#[allow(clippy::too_many_lines)] // Keep the serialized transition contract fail-closed in one audit.
fn valid_versioned_legacy_grind_program(schema_version: &str, route: &NfiLongGrindRoute) -> bool {
    let required_program_version = match schema_version {
        "0.25.0" => Some("grind-transition-program-v1"),
        "0.26.0" => Some("grind-transition-program-v2"),
        "0.27.0" | "0.28.0" => Some("grind-transition-program-v3"),
        _ => None,
    };
    let Some(program) = route.program.as_ref() else {
        return required_program_version.is_none();
    };
    if required_program_version.is_some_and(|required| program.schema_version != required) {
        return false;
    }
    let is_v1 = program.schema_version == "grind-transition-program-v1";
    let is_v2 = program.schema_version == "grind-transition-program-v2";
    let is_v3 = program.schema_version == "grind-transition-program-v3";
    if !is_v1 && !is_v2 && !is_v3 {
        return false;
    }
    let scan = &program.order_scan;
    let policy = &program.policy;
    let known_tags = scan
        .known_clusters
        .iter()
        .flat_map(|cluster| [cluster.entry_tag.as_str(), cluster.stop_tag.as_str()])
        .collect::<BTreeSet<_>>();
    let constant_tags = route
        .constants
        .clusters
        .iter()
        .flat_map(|cluster| [cluster.entry_tag.as_str(), cluster.stop_tag.as_str()])
        .collect::<BTreeSet<_>>();
    let ordinary = scan
        .known_clusters
        .iter()
        .filter(|cluster| !cluster.post_derisk)
        .collect::<Vec<_>>();
    let first = program.source_order.first();
    let compiled_clusters = program
        .source_order
        .iter()
        .skip(1)
        .take(if is_v1 { 2 } else { scan.known_clusters.len() })
        .collect::<Vec<_>>();
    let derisk_buyback = is_v3
        .then(|| program.source_order.get(scan.known_clusters.len() + 1))
        .flatten();
    let valid_location = |location: &crate::domain::ManagedExitSourceLocation| {
        location.line > 0 && location.end_line >= location.line
    };
    let transition_location = |transition: &CompiledLegacyGrindTransition| match transition {
        CompiledLegacyGrindTransition::FirstEntryProfit { location, .. }
        | CompiledLegacyGrindTransition::FirstEntry { location, .. }
        | CompiledLegacyGrindTransition::Cluster { location, .. }
        | CompiledLegacyGrindTransition::DeriskBuyback { location, .. } => {
            (location.line, location.column)
        }
    };
    let source_locations_are_ordered = program.source_order.windows(2).all(|window| {
        let left = transition_location(&window[0]);
        let right = transition_location(&window[1]);
        left <= right
    });
    let first_is_valid = match first {
        Some(CompiledLegacyGrindTransition::FirstEntryProfit {
            tag,
            append_entry_ids_from,
            profit_threshold,
            location,
        }) if is_v1 => {
            scan.first_entry_closed_tags.contains(tag)
                && ordinary
                    .first()
                    .is_some_and(|cluster| cluster.entry_tag == *append_entry_ids_from)
                && profit_threshold.to_bits() == route.first_entry_profit_threshold_spot.to_bits()
                && valid_location(location)
        }
        Some(CompiledLegacyGrindTransition::FirstEntry {
            profit_tag,
            stop_tag,
            append_entry_ids_from,
            profit_threshold,
            stop_threshold,
            location,
        }) if is_v2 || is_v3 => {
            profit_tag != stop_tag
                && scan.first_entry_closed_tags.first() == Some(profit_tag)
                && scan.first_entry_closed_tags.get(1) == Some(stop_tag)
                && ordinary
                    .first()
                    .is_some_and(|cluster| cluster.entry_tag == *append_entry_ids_from)
                && profit_threshold.to_bits() == route.first_entry_profit_threshold_spot.to_bits()
                && stop_threshold.to_bits() == route.first_entry_stop_threshold_spot.to_bits()
                && valid_location(location)
        }
        _ => false,
    };
    let cluster_transitions_are_valid = if is_v1 {
        compiled_clusters.len() == 2
            && compiled_clusters.iter().zip(ordinary.iter().take(2)).all(
                |(transition, expected)| {
                    matches!(
                        transition,
                        CompiledLegacyGrindTransition::Cluster {
                            entry_tag,
                            stop_tag,
                            post_derisk: false,
                            append_entry_ids: true,
                            futures_fallback_loss_threshold: None,
                            location,
                        } if entry_tag == &expected.entry_tag
                            && stop_tag == &expected.stop_tag
                            && valid_location(location)
                    )
                },
            )
    } else {
        let fallback_transitions = compiled_clusters
            .iter()
            .filter_map(|transition| match transition {
                CompiledLegacyGrindTransition::Cluster {
                    entry_tag,
                    futures_fallback_loss_threshold: Some(threshold),
                    ..
                } => Some((entry_tag, threshold)),
                _ => None,
            })
            .collect::<Vec<_>>();
        compiled_clusters.len() == scan.known_clusters.len()
            && compiled_clusters.iter().all(|transition| {
                let CompiledLegacyGrindTransition::Cluster {
                    entry_tag,
                    stop_tag,
                    post_derisk,
                    append_entry_ids: true,
                    futures_fallback_loss_threshold,
                    location,
                } = transition
                else {
                    return false;
                };
                scan.known_clusters.iter().any(|expected| {
                    entry_tag == &expected.entry_tag
                        && stop_tag == &expected.stop_tag
                        && *post_derisk == expected.post_derisk
                }) && futures_fallback_loss_threshold
                    .is_none_or(|threshold| threshold.is_finite() && threshold < 0.0)
                    && valid_location(location)
            })
            && compiled_clusters
                .iter()
                .filter_map(|transition| match transition {
                    CompiledLegacyGrindTransition::Cluster { entry_tag, .. } => Some(entry_tag),
                    _ => None,
                })
                .collect::<BTreeSet<_>>()
                .len()
                == scan.known_clusters.len()
            && fallback_transitions.len() == 1
            && route
                .futures_fallback_loss_threshold
                .is_some_and(|expected| fallback_transitions[0].1.to_bits() == expected.to_bits())
            && scan
                .known_clusters
                .iter()
                .find(|cluster| cluster.entry_tag == *fallback_transitions[0].0)
                .is_some_and(|cluster| !cluster.post_derisk)
    };
    let indexed_features = route
        .stateful_input_contract
        .get("indexed_fields")
        .and_then(|value| value.get("last_candle"))
        .and_then(serde_json::Value::as_array)
        .and_then(|values| {
            values
                .iter()
                .map(serde_json::Value::as_str)
                .collect::<Option<Vec<_>>>()
        });
    let derisk_buyback_is_valid = match derisk_buyback {
        Some(CompiledLegacyGrindTransition::DeriskBuyback {
            tag,
            entry_threshold_futures,
            entry_threshold_spot,
            entry_feature_columns,
            entry_retry_policy,
            entry_stake_basis,
            entry_minimum_multiplier,
            entry_wallet_guard,
            exit_threshold_divisor,
            exit_stake_basis,
            exit_minimum_remaining_multiplier,
            location,
        }) if is_v3 => {
            tag == &scan.derisk_entry_tag
                && entry_threshold_futures.to_bits()
                    == route.constants.derisk_1_reentry_futures.to_bits()
                && entry_threshold_spot.to_bits() == route.constants.derisk_1_reentry_spot.to_bits()
                && entry_threshold_futures.is_finite()
                && entry_threshold_spot.is_finite()
                && !entry_feature_columns.is_empty()
                && entry_feature_columns
                    .iter()
                    .all(|column| !column.is_empty())
                && entry_feature_columns.iter().collect::<BTreeSet<_>>().len()
                    == entry_feature_columns.len()
                && indexed_features.is_some_and(|features| {
                    features
                        == entry_feature_columns
                            .iter()
                            .map(String::as_str)
                            .collect::<Vec<_>>()
                })
                && *entry_retry_policy == CompiledLegacyRetryPolicy::BoundedGrindPolicy
                && *entry_stake_basis == CompiledLegacyEntryStakeBasis::DeriskExitCost
                && entry_minimum_multiplier.to_bits() == policy.minimum_entry_multiplier.to_bits()
                && *entry_wallet_guard == CompiledLegacyWalletGuard::ReturnNone
                && *exit_threshold_divisor == CompiledLegacyThresholdDivisor::ModeLeverage
                && *exit_stake_basis == CompiledLegacyExitStakeBasis::ReentryAmountAtCurrentRate
                && exit_minimum_remaining_multiplier.to_bits()
                    == policy.minimum_remaining_multiplier.to_bits()
                && valid_location(location)
        }
        None if !is_v3 => true,
        _ => false,
    };
    let expected_transition_count = if is_v1 {
        3
    } else if is_v2 {
        scan.known_clusters.len() + 1
    } else {
        scan.known_clusters.len() + 2
    };
    program.execution_mode == CompiledLegacyGrindExecutionMode::PrimaryWithLegacyShadow
        && program.source_callback == "long_grind_adjust_trade_position"
        && matches!(scan.sequence, CompiledOrderSequence::Reverse)
        && scan.entry_order_side == CompiledOrderSide::Buy
        && scan.exit_order_side == CompiledOrderSide::Sell
        && scan.exclude_first_entry
        && matches!(
            scan.partial_fill_policy,
            CompiledPartialFillPolicy::FilledOrdersHaveZeroRemaining
        )
        && scan.known_clusters.len() == route.constants.clusters.len()
        && scan
            .known_clusters
            .iter()
            .zip(&route.constants.clusters)
            .all(|(compiled, constant)| {
                compiled.entry_tag == constant.entry_tag
                    && compiled.stop_tag == constant.stop_tag
                    && compiled.post_derisk == constant.post_derisk
            })
        && known_tags.len() == scan.known_clusters.len() * 2
        && known_tags == constant_tags
        && !ordinary.is_empty()
        && scan.first_entry_closed_tags.len() >= 2
        && !scan.derisk_entry_tag.is_empty()
        && lists_are_unique_and_non_empty([
            &scan.level_one_entry_excluded_tags,
            &scan.level_one_exit_excluded_tags,
            &scan.first_entry_closed_tags,
        ])
        && !scan.close_all_exit_tags.is_empty()
        && scan
            .close_all_exit_tags
            .iter()
            .collect::<BTreeSet<_>>()
            .len()
            == scan.close_all_exit_tags.len()
        && scan
            .first_entry_closed_tags
            .iter()
            .all(|tag| scan.level_one_entry_excluded_tags.contains(tag))
        && scan
            .first_entry_closed_tags
            .iter()
            .all(|tag| scan.level_one_exit_excluded_tags.contains(tag))
        && policy.entry_retry_ms > 0
        && policy.order_age_ms > policy.entry_retry_ms
        && policy.force_order_age_ms > policy.order_age_ms
        && policy.forced_entry_loss_gate.is_finite()
        && policy.forced_entry_loss_gate < 0.0
        && policy.minimum_entry_multiplier.is_finite()
        && policy.minimum_entry_multiplier > 1.0
        && policy.minimum_remaining_multiplier.is_finite()
        && policy.minimum_remaining_multiplier > policy.minimum_entry_multiplier
        && policy.derisk_amount_ratio.is_finite()
        && (0.0..1.0).contains(&policy.derisk_amount_ratio)
        && first_is_valid
        && cluster_transitions_are_valid
        && derisk_buyback_is_valid
        && program.source_order.len() == expected_transition_count
        && source_locations_are_ordered
        && program.location.line > 0
        && program.location.end_line >= program.location.line
        && valid_sha256(&program.fingerprint)
}

#[allow(clippy::too_many_lines)] // Keep every source-compiled regular-route invariant together.
fn valid_versioned_regular_adjustment_program(
    schema_version: &str,
    route: &NfiLongGrindRoute,
) -> bool {
    let required = schema_version == "0.28.0";
    let Some(program) = route.regular_program.as_ref() else {
        return !required;
    };
    let Some(constants) = route.regular_constants.as_ref() else {
        return false;
    };
    let valid_location = |location: &crate::domain::ManagedExitSourceLocation| {
        location.line > 0 && location.end_line >= location.line
    };
    let transition_location = |transition: &CompiledRegularTransition| match transition {
        CompiledRegularTransition::Rebuy { location, .. }
        | CompiledRegularTransition::Grind { location, .. }
        | CompiledRegularTransition::Derisk { location, .. } => (location.line, location.column),
    };
    let source_locations_are_ordered = program
        .source_order
        .windows(2)
        .all(|window| transition_location(&window[0]) <= transition_location(&window[1]));
    let Some(CompiledRegularTransition::Rebuy {
        tag: rebuy_tag,
        location: rebuy_location,
    }) = program.source_order.first()
    else {
        return false;
    };
    let grind_transitions = program
        .source_order
        .iter()
        .skip(1)
        .take(constants.grinds.len())
        .collect::<Vec<_>>();
    let grinds_are_valid = grind_transitions.len() == constants.grinds.len()
        && grind_transitions
            .iter()
            .zip(&constants.grinds)
            .enumerate()
            .all(|(index, (transition, constant))| {
                matches!(
                    transition,
                    CompiledRegularTransition::Grind {
                        level,
                        entry_tag,
                        stop_tag,
                        futures_fallback_loss_threshold,
                        location,
                    } if *level == index + 1
                        && !entry_tag.is_empty()
                        && !stop_tag.is_empty()
                        && entry_tag != stop_tag
                        && entry_tag == &constant.entry_tag
                        && stop_tag == &constant.stop_tag
                        && futures_fallback_loss_threshold
                            .is_none_or(|threshold| threshold.is_finite() && threshold < 0.0)
                        && valid_location(location)
                )
            });
    let fallback_thresholds = grind_transitions
        .iter()
        .filter_map(|transition| match transition {
            CompiledRegularTransition::Grind {
                futures_fallback_loss_threshold,
                ..
            } => *futures_fallback_loss_threshold,
            _ => None,
        })
        .collect::<Vec<_>>();
    let derisk_transitions = program
        .source_order
        .iter()
        .skip(1 + constants.grinds.len())
        .collect::<Vec<_>>();
    let derisk_tags = derisk_transitions
        .iter()
        .filter_map(|transition| match transition {
            CompiledRegularTransition::Derisk { tag, .. } => Some(tag.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>();
    let level_one_tags = derisk_transitions
        .iter()
        .filter_map(|transition| match transition {
            CompiledRegularTransition::Derisk {
                tag,
                level_one: true,
                ..
            } => Some(tag.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>();
    let derisk_transitions_are_valid = derisk_transitions.len() == 2
        && derisk_tags.len() == 2
        && derisk_tags.iter().all(|tag| !tag.is_empty())
        && derisk_tags.iter().collect::<BTreeSet<_>>().len() == derisk_tags.len()
        && level_one_tags.as_slice() == [program.order_scan.derisk_level_one_tag.as_str()]
        && derisk_transitions.iter().all(|transition| {
            matches!(
                transition,
                CompiledRegularTransition::Derisk { location, .. }
                    if valid_location(location)
            )
        });
    let scan = &program.order_scan;
    let action_tags = program
        .source_order
        .iter()
        .flat_map(|transition| match transition {
            CompiledRegularTransition::Rebuy { tag, .. }
            | CompiledRegularTransition::Derisk { tag, .. } => vec![tag.as_str()],
            CompiledRegularTransition::Grind {
                entry_tag,
                stop_tag,
                ..
            } => vec![entry_tag.as_str(), stop_tag.as_str()],
        })
        .collect::<Vec<_>>();
    let action_tags_are_unique =
        action_tags.iter().collect::<BTreeSet<_>>().len() == action_tags.len();
    let scan_is_valid = scan.sequence == CompiledOrderSequence::Reverse
        && scan.entry_order_side == CompiledOrderSide::Buy
        && scan.exit_order_side == CompiledOrderSide::Sell
        && scan.exclude_first_entry
        && lists_are_unique_and_non_empty([
            &scan.rebuy_entry_excluded_tags,
            &scan.rebuy_exit_excluded_tags,
            &scan.derisk_exit_tags,
        ])
        && !scan.partial_fill_tag.is_empty()
        && scan
            .rebuy_entry_excluded_tags
            .iter()
            .all(|tag| scan.rebuy_exit_excluded_tags.contains(tag))
        && scan
            .rebuy_exit_excluded_tags
            .contains(&scan.partial_fill_tag)
        && derisk_tags.iter().all(|tag| {
            scan.derisk_exit_tags
                .iter()
                .any(|candidate| candidate == tag)
        });
    let continuation = &program.continuation;
    program.schema_version == "regular-transition-program-v1"
        && program.execution_mode == CompiledRegularExecutionMode::PrimaryWithLegacyShadow
        && program.source_callback == "long_adjust_trade_position_no_derisk"
        && !rebuy_tag.is_empty()
        && valid_location(rebuy_location)
        && grinds_are_valid
        && fallback_thresholds.len() == 1
        && route
            .futures_fallback_loss_threshold
            .is_some_and(|threshold| threshold.to_bits() == fallback_thresholds[0].to_bits())
        && derisk_transitions_are_valid
        && program.source_order.len() == constants.grinds.len() + 3
        && action_tags_are_unique
        && scan_is_valid
        && continuation.kind == CompiledRegularContinuationKind::LegacyGrind
        && continuation.guard
            == CompiledRegularContinuationGuard::PositionAmountBelowFirstEntryRatio
        && continuation.amount_ratio.is_finite()
        && (0.0..1.0).contains(&continuation.amount_ratio)
        && valid_location(&continuation.location)
        && source_locations_are_ordered
        && valid_location(&program.location)
        && valid_sha256(&program.fingerprint)
}

fn lists_are_unique_and_non_empty<const N: usize>(lists: [&Vec<String>; N]) -> bool {
    lists.iter().all(|values| {
        !values.is_empty()
            && values.iter().all(|value| !value.is_empty())
            && values.iter().collect::<BTreeSet<_>>().len() == values.len()
    })
}

#[allow(clippy::too_many_lines)] // The serialized program is an exact execution contract.
fn valid_versioned_system_adjustment_program(
    schema_version: &str,
    program: Option<&CompiledSystemAdjustmentProgram>,
    adjustment: &NfiX7PositionAdjustment,
    expected_side: CompiledSystemAdjustmentSide,
) -> bool {
    let required = matches!(
        schema_version,
        "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0" | "0.28.0"
    ) || (schema_version == "0.23.0"
        && expected_side == CompiledSystemAdjustmentSide::Long);
    if !required {
        return program.is_none();
    }
    let Some(program) = program else {
        return false;
    };
    let grind_levels = program
        .order_scan
        .grind_levels
        .iter()
        .map(|record| record.level)
        .collect::<Vec<_>>();
    let constant_grind_levels = adjustment
        .constants
        .grinds
        .iter()
        .map(|record| record.level)
        .collect::<Vec<_>>();
    let derisk_levels = program
        .order_scan
        .derisk_tags
        .iter()
        .map(|record| record.level)
        .collect::<Vec<_>>();
    let constant_derisk_levels = adjustment
        .constants
        .derisk_levels
        .iter()
        .map(|record| record.level)
        .collect::<Vec<_>>();
    let source_tags = program
        .source_order
        .iter()
        .map(|action| action.tag.as_str())
        .collect::<Vec<_>>();
    let expected_tags = adjustment
        .program_order
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>();
    program.schema_version == "system-adjustment-program-v1"
        && program.execution_mode == CompiledSystemAdjustmentExecutionMode::PrimaryWithLegacyShadow
        && program.side == expected_side
        && program.source_callback == adjustment.source_callback.as_deref().unwrap_or("")
        && !program.source_callback.is_empty()
        && match expected_side {
            CompiledSystemAdjustmentSide::Long => {
                program.order_scan.entry_order_side == CompiledOrderSide::Buy
                    && program.order_scan.exit_order_side == CompiledOrderSide::Sell
            }
            CompiledSystemAdjustmentSide::Short => {
                program.order_scan.entry_order_side == CompiledOrderSide::Sell
                    && program.order_scan.exit_order_side == CompiledOrderSide::Buy
            }
        }
        && program.order_scan.exclude_first_entry
        && !program.order_scan.global_exit_tag.is_empty()
        && grind_levels == constant_grind_levels
        && derisk_levels == constant_derisk_levels
        && grind_levels.iter().copied().collect::<BTreeSet<_>>().len() == grind_levels.len()
        && derisk_levels.iter().copied().collect::<BTreeSet<_>>().len() == derisk_levels.len()
        && source_tags == expected_tags
        && program.retry_policy.entry_retry_ms > 0
        && program.retry_policy.stale_order_ms > 0
        && adjustment.constants.policy.as_ref().is_some_and(|policy| {
            policy.entry_retry_ms == program.retry_policy.entry_retry_ms
                && policy.stale_order_ms == program.retry_policy.stale_order_ms
        })
        && program.input_contract.is_object()
        && program.location.line > 0
        && program.location.end_line >= program.location.line
        && valid_sha256(&program.fingerprint)
        && program.source_order.iter().all(|action| {
            let grind = program
                .order_scan
                .grind_levels
                .iter()
                .find(|record| record.level == action.level);
            let derisk = program
                .order_scan
                .derisk_tags
                .iter()
                .find(|record| record.level == action.level);
            let action_contract = match action.kind {
                CompiledSystemAdjustmentActionKind::Derisk => {
                    !action.append_entry_ids
                        && derisk.is_some_and(|record| record.tag == action.tag)
                }
                CompiledSystemAdjustmentActionKind::GrindEntry => {
                    !action.append_entry_ids
                        && grind.is_some_and(|record| record.entry_tag == action.tag)
                }
                CompiledSystemAdjustmentActionKind::GrindExit => {
                    action.append_entry_ids
                        && grind.is_some_and(|record| record.exit_tag == action.tag)
                }
                CompiledSystemAdjustmentActionKind::GrindDerisk => {
                    action.append_entry_ids
                        && grind.is_some_and(|record| record.derisk_tag == action.tag)
                }
            };
            let binding_names = action
                .bindings
                .iter()
                .map(|binding| binding.name.as_str())
                .collect::<BTreeSet<_>>();
            action_contract
                && !action.tag.is_empty()
                && valid_scalar_program(&action.decision_program)
                && action.input_contract.is_object()
                && action.location.line > 0
                && action.location.end_line >= action.location.line
                && binding_names.len() == action.bindings.len()
                && action.bindings.iter().all(|binding| {
                    if binding.name.is_empty() {
                        return false;
                    }
                    match binding.kind {
                        CompiledSystemAdjustmentInputKind::DeriskFound => binding
                            .level
                            .is_some_and(|level| derisk_levels.contains(&level)),
                        CompiledSystemAdjustmentInputKind::ClusterCount
                        | CompiledSystemAdjustmentInputKind::ClusterMaximumCount
                        | CompiledSystemAdjustmentInputKind::ClusterDistance
                        | CompiledSystemAdjustmentInputKind::ClusterThresholds
                        | CompiledSystemAdjustmentInputKind::ClusterStakes
                        | CompiledSystemAdjustmentInputKind::ClusterTotalAmount
                        | CompiledSystemAdjustmentInputKind::ClusterOpenRate
                        | CompiledSystemAdjustmentInputKind::ClusterProfitRate
                        | CompiledSystemAdjustmentInputKind::ClusterProfitStake
                        | CompiledSystemAdjustmentInputKind::ClusterProfitThreshold
                        | CompiledSystemAdjustmentInputKind::ClusterDeriskThreshold
                        | CompiledSystemAdjustmentInputKind::ClusterMaximumProfitStake
                        | CompiledSystemAdjustmentInputKind::ClusterMaximumProfitRate => binding
                            .level
                            .is_some_and(|level| grind_levels.contains(&level)),
                        _ => binding.level.is_none(),
                    }
                })
        })
}

fn valid_versioned_rebuy_program(
    schema_version: &str,
    program: Option<&CompiledAdjustmentProgram>,
    delegate_policy: Option<&NfiX7AdjustmentPolicy>,
    delegate_source_callback: Option<&str>,
) -> bool {
    if !matches!(
        schema_version,
        "0.22.0" | "0.23.0" | "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0" | "0.28.0"
    ) {
        return program.is_none();
    }
    let Some(program) = program else {
        return false;
    };
    program.schema_version == "adjustment-transition-program-v1"
        && program.execution_mode == CompiledAdjustmentExecutionMode::Primary
        && program.source_order
            == [
                CompiledAdjustmentOperation::Delegate,
                CompiledAdjustmentOperation::Decision,
            ]
        && program.order_scan.cluster_order_side != program.order_scan.boundary_order_side
        && program.order_scan.exclude_first_order
        && !program.delegate.tag.is_empty()
        && !program.delegate.source_target.is_empty()
        && program.delegate.target_entry_retry_ms > 0
        && delegate_policy
            .is_some_and(|policy| policy.entry_retry_ms == program.delegate.target_entry_retry_ms)
        && delegate_source_callback == Some(program.delegate.source_target.as_str())
        && program.input_contract.is_object()
        && program.location.line > 0
        && program.location.end_line >= program.location.line
        && program.delegate.location.line > 0
        && program.delegate.location.end_line >= program.delegate.location.line
        && valid_sha256(&program.fingerprint)
        && valid_scalar_program(&program.decision_program)
}

fn valid_adjustment_source_callback(schema_version: &str, callback: Option<&str>) -> bool {
    if matches!(
        schema_version,
        "0.22.0" | "0.23.0" | "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0" | "0.28.0"
    ) {
        callback.is_some_and(|value| !value.is_empty())
    } else {
        callback.is_none()
    }
}

#[allow(clippy::too_many_lines)] // Route proof keeps source order and state policy co-located.
fn valid_managed_exit_program(manager: &NfiX7TradeManager) -> bool {
    let Some(program) = manager.managed_exit_program.as_ref() else {
        return !managed_exit_program_required(&manager.schema_version);
    };
    if program.schema_version != "managed-exit-program-v1"
        || !valid_managed_exit_execution_mode(&manager.schema_version, program.execution_mode)
        || !valid_sha256(&program.fingerprint)
        || program.routes.is_empty()
    {
        return false;
    }
    let route_ids = program
        .routes
        .iter()
        .map(|route| route.id.as_str())
        .collect::<BTreeSet<_>>();
    if route_ids.len() != program.routes.len() {
        return false;
    }
    if matches!(
        manager.schema_version.as_str(),
        "0.18.0"
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
    ) && route_ids
        != manager
            .managed_long_routes
            .iter()
            .map(|route| route.key.as_str())
            .collect::<BTreeSet<_>>()
    {
        return false;
    }
    let source_order = manager
        .route_order
        .iter()
        .filter(|key| route_ids.contains(key.as_str()))
        .map(String::as_str)
        .collect::<Vec<_>>();
    if source_order
        != program
            .routes
            .iter()
            .map(|route| route.id.as_str())
            .collect::<Vec<_>>()
    {
        return false;
    }
    let known_tags = managed_long_exit_tags(manager);
    program.routes.iter().enumerate().all(|(index, route)| {
        let mut matcher_tags = BTreeSet::new();
        let Some(legacy) = manager
            .managed_long_routes
            .iter()
            .find(|candidate| candidate.key == route.id)
        else {
            return false;
        };
        route.source_order == index
            && !route.id.is_empty()
            && !route.mode_name.is_empty()
            && route.mode_name == legacy.mode_name
            && valid_managed_exit_matcher(
                &route.matcher,
                &known_tags,
                &mut matcher_tags,
                0,
                false,
                true,
            )
            && legacy
                .entry_tags
                .iter()
                .all(|tag| matcher_tags.contains(tag.as_str()))
            && (manager.schema_version != "0.17.0"
                || matcher_tags == legacy.entry_tags.iter().map(String::as_str).collect())
            && route
                .initial_profit_gate
                .as_ref()
                .is_none_or(|gate| gate.value.is_finite())
            && !route.decision_program_order.is_empty()
            && route
                .decision_program_order
                .iter()
                .all(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
            && match route.state_program.as_ref() {
                Some(state) => valid_managed_exit_state_program(
                    state,
                    "long_exit_stoploss",
                    &known_tags,
                    matches!(
                        manager.schema_version.as_str(),
                        "0.20.0"
                            | "0.21.0"
                            | "0.22.0"
                            | "0.23.0"
                            | "0.24.0"
                            | "0.25.0"
                            | "0.26.0"
                            | "0.27.0"
                            | "0.28.0"
                    ),
                ),
                None => !matches!(
                    manager.schema_version.as_str(),
                    "0.19.0"
                        | "0.20.0"
                        | "0.21.0"
                        | "0.22.0"
                        | "0.23.0"
                        | "0.24.0"
                        | "0.25.0"
                        | "0.26.0"
                        | "0.27.0"
                        | "0.28.0"
                ),
            }
            && valid_managed_exit_terminal(route, &matcher_tags)
            && route.location.line > 0
            && route.location.end_line >= route.location.line
    })
}

fn managed_exit_program_required(schema_version: &str) -> bool {
    matches!(
        schema_version,
        "0.17.0"
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
    )
}

fn managed_long_exit_tags(manager: &NfiX7TradeManager) -> BTreeSet<&str> {
    let mut tags = manager
        .managed_long_routes
        .iter()
        .flat_map(|route| route.entry_tags.iter().map(String::as_str))
        .collect::<BTreeSet<_>>();
    if let Some(route) = manager.long_grind.as_ref() {
        tags.extend(route.entry_tags.iter().map(String::as_str));
    }
    if let Some(route) = manager.long_btc.as_ref() {
        tags.extend(route.entry_tags.iter().map(String::as_str));
    }
    tags
}

#[allow(clippy::too_many_lines)] // One audit keeps the entire short exit contract fail-closed.
fn valid_managed_short_exit_program(manager: &NfiX7TradeManager) -> bool {
    let Some(program) = manager.managed_short_exit_program.as_ref() else {
        return !matches!(
            manager.schema_version.as_str(),
            "0.20.0"
                | "0.21.0"
                | "0.22.0"
                | "0.23.0"
                | "0.24.0"
                | "0.25.0"
                | "0.26.0"
                | "0.27.0"
                | "0.28.0"
        );
    };
    if program.schema_version != "managed-exit-program-v1"
        || !valid_managed_exit_execution_mode(&manager.schema_version, program.execution_mode)
        || !valid_sha256(&program.fingerprint)
        || program.routes.is_empty()
    {
        return false;
    }
    let route_ids = program
        .routes
        .iter()
        .map(|route| route.id.as_str())
        .collect::<BTreeSet<_>>();
    let expected_ids = manager
        .managed_short_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    if route_ids.len() != program.routes.len()
        || route_ids != expected_ids
        || manager.short_route_order
            != program
                .routes
                .iter()
                .map(|route| route.id.clone())
                .collect::<Vec<_>>()
    {
        return false;
    }
    let known_tags = manager
        .managed_short_routes
        .iter()
        .flat_map(|route| route.entry_tags.iter().map(String::as_str))
        .collect::<BTreeSet<_>>();
    program.routes.iter().enumerate().all(|(index, route)| {
        let mut matcher_tags = BTreeSet::new();
        let Some(legacy) = manager
            .managed_short_routes
            .iter()
            .find(|candidate| candidate.key == route.id)
        else {
            return false;
        };
        route.source_order == index
            && !route.id.is_empty()
            && route.mode_name == legacy.mode_name
            && valid_managed_exit_matcher(
                &route.matcher,
                &known_tags,
                &mut matcher_tags,
                0,
                true,
                false,
            )
            && (route.id == "short_top_coins_fallback"
                || legacy
                    .entry_tags
                    .iter()
                    .all(|tag| matcher_tags.contains(tag.as_str())))
            && route
                .initial_profit_gate
                .as_ref()
                .is_none_or(|gate| gate.value.is_finite())
            && !route.decision_program_order.is_empty()
            && route
                .decision_program_order
                .iter()
                .all(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
            && route.state_program.as_ref().is_some_and(|state| {
                valid_managed_exit_state_program(
                    state,
                    "short_exit_stoploss",
                    &known_tags,
                    matches!(
                        manager.schema_version.as_str(),
                        "0.20.0"
                            | "0.21.0"
                            | "0.22.0"
                            | "0.23.0"
                            | "0.24.0"
                            | "0.25.0"
                            | "0.26.0"
                            | "0.27.0"
                            | "0.28.0"
                    ),
                )
            })
            && route.terminal_exit.is_none()
            && route.location.line > 0
            && route.location.end_line >= route.location.line
    })
}

fn valid_managed_exit_execution_mode(schema_version: &str, mode: ManagedExitExecutionMode) -> bool {
    if matches!(
        schema_version,
        "0.21.0" | "0.22.0" | "0.23.0" | "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0" | "0.28.0"
    ) {
        mode == ManagedExitExecutionMode::PrimaryWithLegacyShadow
    } else {
        mode == ManagedExitExecutionMode::Shadow
    }
}

fn valid_managed_exit_terminal(route: &ManagedExitRoute, matcher_tags: &BTreeSet<&str>) -> bool {
    route.terminal_exit.as_ref().is_none_or(|terminal| {
        let tags = terminal
            .entry_tags
            .iter()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        !tags.is_empty()
            && tags.len() == terminal.entry_tags.len()
            && tags.iter().all(|tag| matcher_tags.contains(tag))
            && terminal.minimum_age_ms > 0
            && terminal.minimum_profit_ratio.is_finite()
            && !terminal.reason.is_empty()
    })
}

fn valid_managed_exit_state_program(
    state: &ManagedExitStateProgram,
    source_helper: &str,
    known_tags: &BTreeSet<&str>,
    require_scalp_matcher: bool,
) -> bool {
    let expected_order = match state.inline_exit.as_ref().map(|inline| inline.position) {
        Some(ManagedExitInlinePosition::BeforeStop) => vec![
            ManagedExitStateOperation::InlineExit,
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
        Some(ManagedExitInlinePosition::AfterStop) => vec![
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::InlineExit,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
        None => vec![
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
    };
    let valid_inline = state.inline_exit.as_ref().is_none_or(|inline| {
        inline.minimum_profit.is_finite()
            && inline.maximum_profit.is_finite()
            && inline.minimum_profit < inline.maximum_profit
            && valid_scalar_program(&inline.program)
    });
    let valid_stop = match &state.stop {
        ManagedExitStopPolicy::SourceHelper { helper } => helper == source_helper,
        ManagedExitStopPolicy::StakeThreshold {
            futures_threshold,
            spot_threshold,
            ..
        } => {
            futures_threshold.is_finite()
                && *futures_threshold >= 0.0
                && spot_threshold.is_finite()
                && *spot_threshold >= 0.0
        }
    };
    let target = &state.target;
    let valid_scalp_matcher = match (
        target.pure_scalp_trailing,
        target.pure_scalp_matcher.as_ref(),
    ) {
        (true, Some(matcher)) => {
            let mut matcher_tags = BTreeSet::new();
            valid_managed_exit_matcher(matcher, known_tags, &mut matcher_tags, 0, false, true)
                && !matcher_tags.is_empty()
        }
        (true, None) => !require_scalp_matcher,
        (false, None) => true,
        (false, Some(_)) => false,
    };
    state.stateful_order == expected_order
        && valid_inline
        && valid_stop
        && valid_scalp_matcher
        && target.u_e_raise_delta.is_finite()
        && target.u_e_raise_delta >= 0.0
        && target.profit_raise_delta.is_finite()
        && target.profit_raise_delta >= 0.0
        && target.max_target_floor.is_finite()
        && target.max_target_floor >= 0.0
}

fn valid_managed_exit_matcher<'a>(
    matcher: &'a ManagedExitTagMatcher,
    known_tags: &BTreeSet<&str>,
    collected_tags: &mut BTreeSet<&'a str>,
    depth: usize,
    allow_side_operators: bool,
    enforce_known_tags: bool,
) -> bool {
    if depth >= 8 {
        return false;
    }
    match matcher.operator {
        ManagedExitTagOperator::Any | ManagedExitTagOperator::All => {
            let tags = matcher
                .entry_tags
                .iter()
                .map(String::as_str)
                .collect::<BTreeSet<_>>();
            matcher.operands.is_empty()
                && !tags.is_empty()
                && tags.len() == matcher.entry_tags.len()
                && (!enforce_known_tags || tags.iter().all(|tag| known_tags.contains(tag)))
                && {
                    collected_tags.extend(tags);
                    true
                }
        }
        ManagedExitTagOperator::AnyOf | ManagedExitTagOperator::AllOf => {
            matcher.entry_tags.is_empty()
                && matcher.operands.len() >= 2
                && matcher.operands.iter().all(|operand| {
                    valid_managed_exit_matcher(
                        operand,
                        known_tags,
                        collected_tags,
                        depth + 1,
                        allow_side_operators,
                        enforce_known_tags,
                    )
                })
        }
        ManagedExitTagOperator::Not => {
            allow_side_operators
                && matcher.entry_tags.is_empty()
                && matcher.operands.len() == 1
                && valid_managed_exit_matcher(
                    &matcher.operands[0],
                    known_tags,
                    collected_tags,
                    depth + 1,
                    allow_side_operators,
                    enforce_known_tags,
                )
        }
        ManagedExitTagOperator::IsShort => {
            allow_side_operators && matcher.entry_tags.is_empty() && matcher.operands.is_empty()
        }
    }
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
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
