//! Managed long-exit program validation.

use super::{
    managed_exit_contract, shared, valid_scalar_program, BTreeSet, ManagedExitRoute,
    NfiX7TradeManager,
};

pub(super) fn valid_managed_exit_program(manager: &NfiX7TradeManager) -> bool {
    let Some(program) = manager.managed_exit_program.as_ref() else {
        return !managed_exit_program_required(&manager.schema_version);
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
            | "0.29.0"
            | "0.30.0"
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
        && managed_exit_contract::valid_managed_exit_matcher(
            &route.matcher,
            known_tags,
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
        && valid_state_program(manager, route, known_tags)
        && managed_exit_contract::valid_managed_exit_terminal(route, &matcher_tags)
        && route.location.line > 0
        && route.location.end_line >= route.location.line
}

fn valid_state_program(
    manager: &NfiX7TradeManager,
    route: &ManagedExitRoute,
    known_tags: &BTreeSet<&str>,
) -> bool {
    match route.state_program.as_ref() {
        Some(state) => managed_exit_contract::valid_managed_exit_state_program(
            state,
            "long_exit_stoploss",
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
                | "0.29.0"
                | "0.30.0"
        ),
    }
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
            | "0.29.0"
            | "0.30.0"
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
