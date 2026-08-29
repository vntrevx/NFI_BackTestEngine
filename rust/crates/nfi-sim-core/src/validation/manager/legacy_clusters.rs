//! Legacy grind cluster and liquidation-rescue validation.

use crate::domain::{CompiledLegacyGrindCluster, CompiledLegacyGrindProgram};

use super::{
    BTreeSet, CompiledLegacyComparison, CompiledLegacyGrindSide, CompiledLegacyGrindTransition,
    NfiLongGrindRoute,
};

fn threshold_sign_is_valid(side: CompiledLegacyGrindSide, value: f64) -> bool {
    match side {
        CompiledLegacyGrindSide::Long => value < 0.0,
        CompiledLegacyGrindSide::Short => value > 0.0,
    }
}

fn multiplier_is_valid(side: CompiledLegacyGrindSide, value: f64) -> bool {
    match side {
        CompiledLegacyGrindSide::Long => value > 1.0,
        CompiledLegacyGrindSide::Short => (0.0..1.0).contains(&value),
    }
}

pub(super) fn threshold_is_valid(side: CompiledLegacyGrindSide, value: f64) -> bool {
    value.is_finite() && threshold_sign_is_valid(side, value)
}

pub(super) fn clusters_are_valid(
    program: &CompiledLegacyGrindProgram,
    route: &NfiLongGrindRoute,
    is_v1: bool,
    expected_comparison: CompiledLegacyComparison,
) -> bool {
    let scan = &program.order_scan;
    let ordinary = scan
        .known_clusters
        .iter()
        .filter(|cluster| !cluster.post_derisk)
        .collect::<Vec<_>>();
    let compiled = program
        .source_order
        .iter()
        .skip(1)
        .take(if is_v1 { 2 } else { scan.known_clusters.len() })
        .collect::<Vec<_>>();
    let valid_location = |location: &crate::domain::ManagedExitSourceLocation| {
        location.line > 0 && location.end_line >= location.line
    };
    let transitions_are_valid = if is_v1 {
        compiled.len() == 2
            && compiled
                .iter()
                .zip(ordinary.iter().take(2))
                .all(|(transition, expected)| {
                    matches!(
                        transition,
                        CompiledLegacyGrindTransition::Cluster {
                            entry_tag,
                            stop_tag,
                            post_derisk: false,
                            append_entry_ids: true,
                            futures_fallback_loss_threshold: None,
                            liquidation_rescue: None,
                            location,
                        } if entry_tag == &expected.entry_tag
                            && stop_tag == &expected.stop_tag
                            && valid_location(location)
                    )
                })
    } else {
        valid_versioned_clusters(
            program,
            route,
            &compiled,
            expected_comparison,
            &valid_location,
        )
    };
    transitions_are_valid && valid_liquidation_rescue(&compiled, &ordinary)
}

fn valid_versioned_clusters(
    program: &CompiledLegacyGrindProgram,
    route: &NfiLongGrindRoute,
    compiled: &[&CompiledLegacyGrindTransition],
    expected_comparison: CompiledLegacyComparison,
    valid_location: &impl Fn(&crate::domain::ManagedExitSourceLocation) -> bool,
) -> bool {
    let scan = &program.order_scan;
    let fallback = compiled
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
    compiled.len() == scan.known_clusters.len()
        && compiled.iter().all(|transition| {
            let CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                stop_tag,
                post_derisk,
                append_entry_ids: true,
                futures_fallback_loss_threshold,
                liquidation_rescue,
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
                .is_none_or(|value| threshold_is_valid(program.side, value))
                && liquidation_rescue.as_ref().is_none_or(|policy| {
                    policy.side == program.side
                        && policy.cluster_level > 0
                        && threshold_is_valid(program.side, policy.loss_threshold)
                        && policy.profit_comparison == expected_comparison
                        && policy.liquidation_multiplier.is_finite()
                        && multiplier_is_valid(program.side, policy.liquidation_multiplier)
                        && policy.liquidation_comparison == expected_comparison
                        && !policy.used_state_key.is_empty()
                })
                && valid_location(location)
        })
        && compiled
            .iter()
            .filter_map(|transition| match transition {
                CompiledLegacyGrindTransition::Cluster { entry_tag, .. } => Some(entry_tag),
                _ => None,
            })
            .collect::<BTreeSet<_>>()
            .len()
            == scan.known_clusters.len()
        && fallback.len() == 1
        && route
            .futures_fallback_loss_threshold
            .is_some_and(|expected| fallback[0].1.to_bits() == expected.to_bits())
        && scan
            .known_clusters
            .iter()
            .find(|cluster| cluster.entry_tag == *fallback[0].0)
            .is_some_and(|cluster| !cluster.post_derisk)
}

fn valid_liquidation_rescue(
    compiled: &[&CompiledLegacyGrindTransition],
    ordinary: &[&CompiledLegacyGrindCluster],
) -> bool {
    let rescues = compiled
        .iter()
        .filter_map(|transition| match transition {
            CompiledLegacyGrindTransition::Cluster {
                entry_tag,
                liquidation_rescue: Some(policy),
                ..
            } => Some((entry_tag, policy)),
            _ => None,
        })
        .collect::<Vec<_>>();
    rescues.len() <= 1
        && rescues.iter().all(|(entry_tag, policy)| {
            policy.cluster_level > 0
                && ordinary
                    .get(policy.cluster_level - 1)
                    .is_some_and(|cluster| cluster.entry_tag == **entry_tag)
        })
}
