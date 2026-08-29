//! Manager identity, managed route/tag sets, and route-order validation.

use std::collections::BTreeSet;

use crate::domain::NfiX7TradeManager;

use super::{
    uses_full_futures_manager_contract, valid_nfi_managed_long_route, valid_nfi_managed_short_route,
};

pub(super) struct RouteSummary<'a> {
    pub(super) managed_tags: BTreeSet<&'a String>,
    pub(super) short_tags: BTreeSet<&'a String>,
    valid: bool,
}

impl RouteSummary<'_> {
    pub(super) fn is_valid(&self) -> bool {
        self.valid
    }
}

pub(super) fn summarize(manager: &NfiX7TradeManager) -> RouteSummary<'_> {
    let managed_keys = manager
        .managed_long_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let managed_tags = manager
        .managed_long_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let short_tags = manager
        .managed_short_routes
        .iter()
        .flat_map(|route| &route.entry_tags)
        .collect::<BTreeSet<_>>();
    let identity = valid_identity(manager);
    let managed_routes = valid_managed_routes(manager, &managed_keys, &managed_tags);
    let terminal_exit_version = valid_terminal_exit_version(manager);
    let short_routes = valid_short_routes(manager, &managed_tags, &short_tags);
    let route_order = valid_route_order(manager, &managed_keys);
    RouteSummary {
        managed_tags,
        short_tags,
        valid: identity && managed_routes && terminal_exit_version && short_routes && route_order,
    }
}

fn valid_identity(manager: &NfiX7TradeManager) -> bool {
    matches!(
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
            | "0.29.0"
            | "0.30.0"
    ) && super::shared::valid_sha256(&manager.source_sha256)
}

fn valid_managed_routes(
    manager: &NfiX7TradeManager,
    managed_keys: &BTreeSet<&str>,
    managed_tags: &BTreeSet<&String>,
) -> bool {
    let expected = [
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
    let tag_count = manager
        .managed_long_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    manager.managed_long_routes.len() == expected.len()
        && managed_keys == &expected
        && managed_tags.len() == tag_count
        && manager
            .managed_long_routes
            .iter()
            .all(valid_nfi_managed_long_route)
}

fn valid_terminal_exit_version(manager: &NfiX7TradeManager) -> bool {
    matches!(
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
            | "0.29.0"
            | "0.30.0"
    ) || manager
        .managed_long_routes
        .iter()
        .all(|route| route.terminal_exit.is_none())
}

fn valid_short_routes(
    manager: &NfiX7TradeManager,
    managed_tags: &BTreeSet<&String>,
    short_tags: &BTreeSet<&String>,
) -> bool {
    let expected_order = if uses_full_futures_manager_contract(&manager.schema_version) {
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
    let expected_keys = expected_order.iter().copied().collect::<BTreeSet<_>>();
    let short_keys = manager
        .managed_short_routes
        .iter()
        .map(|route| route.key.as_str())
        .collect::<BTreeSet<_>>();
    let tag_count = manager
        .managed_short_routes
        .iter()
        .map(|route| route.entry_tags.len())
        .sum::<usize>();
    manager.managed_short_routes.len() == expected_keys.len()
        && short_keys == expected_keys
        && short_tags.len() == tag_count
        && short_tags.iter().all(|tag| !managed_tags.contains(*tag))
        && manager
            .managed_short_routes
            .iter()
            .all(valid_nfi_managed_short_route)
        && manager.short_route_order
            == expected_order
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
}

fn valid_route_order(manager: &NfiX7TradeManager, managed_keys: &BTreeSet<&str>) -> bool {
    let expected = [
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
            || (*key == "long_grind" && manager.long_grind.is_some())
            || (*key == "long_btc" && manager.long_btc.is_some())
    })
    .map(ToOwned::to_owned)
    .collect::<Vec<_>>();
    if matches!(
        manager.schema_version.as_str(),
        "0.17.0" | "0.18.0" | "0.19.0"
    ) {
        manager.route_order.len() == expected.len()
            && manager.route_order.iter().collect::<BTreeSet<_>>()
                == expected.iter().collect::<BTreeSet<_>>()
    } else {
        manager.route_order == expected
    }
}
