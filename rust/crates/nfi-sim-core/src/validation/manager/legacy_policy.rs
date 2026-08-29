//! Legacy grind bounded de-risk, retry, stake, and wallet policy validation.

use crate::domain::CompiledLegacyGrindProgram;

use super::{
    legacy_clusters, shared, BTreeSet, CompiledLegacyEntryStakeBasis, CompiledLegacyExitStakeBasis,
    CompiledLegacyGrindExecutionMode, CompiledLegacyGrindSide, CompiledLegacyGrindTransition,
    CompiledLegacyRetryPolicy, CompiledLegacyThresholdDivisor, CompiledLegacyWalletGuard,
    CompiledOrderSequence, CompiledOrderSide, CompiledPartialFillPolicy, NfiLongGrindRoute,
};

pub(super) fn program_is_valid(
    schema_version: &str,
    program: &CompiledLegacyGrindProgram,
    route: &NfiLongGrindRoute,
) -> bool {
    let scan = &program.order_scan;
    let is_v1 = program.schema_version == "grind-transition-program-v1";
    let is_v2 = program.schema_version == "grind-transition-program-v2";
    let ordinary = scan
        .known_clusters
        .iter()
        .filter(|cluster| !cluster.post_derisk)
        .collect::<Vec<_>>();
    let valid_location = |location: &crate::domain::ManagedExitSourceLocation| {
        location.line > 0 && location.end_line >= location.line
    };
    let first_is_valid = match program.source_order.first() {
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
        }) if !is_v1 => {
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
    let transition_location = |transition: &CompiledLegacyGrindTransition| match transition {
        CompiledLegacyGrindTransition::FirstEntryProfit { location, .. }
        | CompiledLegacyGrindTransition::FirstEntry { location, .. }
        | CompiledLegacyGrindTransition::Cluster { location, .. }
        | CompiledLegacyGrindTransition::DeriskBuyback { location, .. } => {
            (location.line, location.column)
        }
    };
    let locations_are_ordered = program
        .source_order
        .windows(2)
        .all(|window| transition_location(&window[0]) <= transition_location(&window[1]));
    let expected_count = if is_v1 {
        3
    } else if is_v2 {
        scan.known_clusters.len() + 1
    } else {
        scan.known_clusters.len() + 2
    };
    first_is_valid
        && scan_and_retry_are_valid(schema_version, program, route, &ordinary)
        && program.source_order.len() == expected_count
        && locations_are_ordered
        && valid_location(&program.location)
        && shared::valid_sha256(&program.fingerprint)
}

fn scan_and_retry_are_valid(
    schema_version: &str,
    program: &CompiledLegacyGrindProgram,
    route: &NfiLongGrindRoute,
    ordinary: &[&crate::domain::CompiledLegacyGrindCluster],
) -> bool {
    let scan = &program.order_scan;
    let policy = &program.policy;
    let (entry_side, exit_side, callback) = match program.side {
        CompiledLegacyGrindSide::Long => (
            CompiledOrderSide::Buy,
            CompiledOrderSide::Sell,
            "long_grind_adjust_trade_position",
        ),
        CompiledLegacyGrindSide::Short => (
            CompiledOrderSide::Sell,
            CompiledOrderSide::Buy,
            "short_grind_adjust_trade_position",
        ),
    };
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
    program.execution_mode
        == if matches!(schema_version, "0.29.0" | "0.30.0") {
            CompiledLegacyGrindExecutionMode::Primary
        } else {
            CompiledLegacyGrindExecutionMode::PrimaryWithLegacyShadow
        }
        && program.source_callback == callback
        && scan.sequence == CompiledOrderSequence::Reverse
        && scan.entry_order_side == entry_side
        && scan.exit_order_side == exit_side
        && scan.exclude_first_entry
        && scan.partial_fill_policy == CompiledPartialFillPolicy::FilledOrdersHaveZeroRemaining
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
        && valid_scan_lists(scan)
        && policy.entry_retry_ms > 0
        && policy.order_age_ms > policy.entry_retry_ms
        && policy.force_order_age_ms > policy.order_age_ms
        && legacy_clusters::threshold_is_valid(program.side, policy.forced_entry_loss_gate)
        && policy.minimum_entry_multiplier.is_finite()
        && policy.minimum_entry_multiplier > 1.0
        && policy.minimum_remaining_multiplier.is_finite()
        && policy.minimum_remaining_multiplier > policy.minimum_entry_multiplier
        && policy.derisk_amount_ratio.is_finite()
        && (0.0..1.0).contains(&policy.derisk_amount_ratio)
}

fn valid_scan_lists(scan: &crate::domain::CompiledLegacyGrindOrderScan) -> bool {
    shared::lists_are_unique_and_non_empty([
        &scan.level_one_entry_excluded_tags,
        &scan.level_one_exit_excluded_tags,
        &scan.first_entry_closed_tags,
    ]) && !scan.close_all_exit_tags.is_empty()
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
}

pub(super) fn derisk_buyback_is_valid(
    program: &CompiledLegacyGrindProgram,
    route: &NfiLongGrindRoute,
    is_v3: bool,
) -> bool {
    let transition = is_v3
        .then(|| {
            program
                .source_order
                .get(program.order_scan.known_clusters.len() + 1)
        })
        .flatten();
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
    match transition {
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
            tag == &program.order_scan.derisk_entry_tag
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
                && entry_minimum_multiplier.to_bits()
                    == program.policy.minimum_entry_multiplier.to_bits()
                && *entry_wallet_guard == CompiledLegacyWalletGuard::ReturnNone
                && *exit_threshold_divisor == CompiledLegacyThresholdDivisor::ModeLeverage
                && *exit_stake_basis == CompiledLegacyExitStakeBasis::ReentryAmountAtCurrentRate
                && exit_minimum_remaining_multiplier.to_bits()
                    == program.policy.minimum_remaining_multiplier.to_bits()
                && location.line > 0
                && location.end_line >= location.line
        }
        None if !is_v3 => true,
        _ => false,
    }
}
