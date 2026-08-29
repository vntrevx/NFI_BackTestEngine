//! Managed short-exit program validation.

use super::{
    managed_exit_contract, shared, valid_scalar_program, BTreeSet, ManagedExitRoute,
    NfiX7TradeManager,
};

pub(super) fn valid_managed_short_exit_program(manager: &NfiX7TradeManager) -> bool {
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
                | "0.29.0"
                | "0.30.0"
        );
    };
    if program.schema_version != "managed-exit-program-v1"
        || !managed_exit_contract::valid_managed_exit_execution_mode(
            &manager.schema_version,
            program.execution_mode,
        )
        || !shared::valid_sha256(&program.fingerprint)
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
    program
        .routes
        .iter()
        .enumerate()
        .all(|(index, route)| valid_route(manager, route, index, &known_tags))
}

fn valid_route(
    manager: &NfiX7TradeManager,
    route: &ManagedExitRoute,
    index: usize,
    known_tags: &BTreeSet<&str>,
) -> bool {
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
        && managed_exit_contract::valid_managed_exit_matcher(
            &route.matcher,
            known_tags,
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
            managed_exit_contract::valid_managed_exit_state_program(
                state,
                "short_exit_stoploss",
                known_tags,
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
                        | "0.29.0"
                        | "0.30.0"
                ),
            )
        })
        && route.terminal_exit.is_none()
        && route.location.line > 0
        && route.location.end_line >= route.location.line
}
