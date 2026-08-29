//! Focused NFI trade-manager validation.

use super::{BTreeSet, NfiManagedLongProfile, NfiManagedLongRoute};

pub(super) fn valid_nfi_managed_short_route(route: &NfiManagedLongRoute) -> bool {
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
