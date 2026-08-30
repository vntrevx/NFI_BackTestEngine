//! Focused NFI trade-manager validation.

use crate::domain::{CompiledRegularAdjustmentProgram, NfiRegularAdjustmentConstants};

use super::{
    shared, BTreeSet, CompiledOrderSequence, CompiledOrderSide, CompiledRegularContinuationGuard,
    CompiledRegularContinuationKind, CompiledRegularExecutionMode, CompiledRegularTransition,
    NfiLongGrindRoute,
};

struct TransitionSummary<'a> {
    grinds_are_valid: bool,
    fallback_thresholds: Vec<f64>,
    derisks_are_valid: bool,
    derisk_tags: Vec<&'a str>,
}

pub(super) fn valid_versioned_regular_adjustment_program(
    schema_version: &str,
    route: &NfiLongGrindRoute,
) -> bool {
    let required = matches!(schema_version, "0.28.0" | "0.29.0" | "0.30.0" | "0.31.0");
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
    let transitions = summarize_transitions(program, constants);
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
        && shared::lists_are_unique_and_non_empty([
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
        && transitions.derisk_tags.iter().all(|tag| {
            scan.derisk_exit_tags
                .iter()
                .any(|candidate| candidate == tag)
        });
    let continuation = &program.continuation;
    program.schema_version == "regular-transition-program-v1"
        && program.execution_mode
            == if matches!(schema_version, "0.29.0" | "0.30.0" | "0.31.0") {
                CompiledRegularExecutionMode::Primary
            } else {
                CompiledRegularExecutionMode::PrimaryWithLegacyShadow
            }
        && program.source_callback == "long_adjust_trade_position_no_derisk"
        && !rebuy_tag.is_empty()
        && valid_location(rebuy_location)
        && transitions.grinds_are_valid
        && transitions.fallback_thresholds.len() == 1
        && route
            .futures_fallback_loss_threshold
            .is_some_and(|threshold| {
                threshold.to_bits() == transitions.fallback_thresholds[0].to_bits()
            })
        && transitions.derisks_are_valid
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
        && shared::valid_sha256(&program.fingerprint)
}

fn summarize_transitions<'a>(
    program: &'a CompiledRegularAdjustmentProgram,
    constants: &NfiRegularAdjustmentConstants,
) -> TransitionSummary<'a> {
    let valid_location = |location: &crate::domain::ManagedExitSourceLocation| {
        location.line > 0 && location.end_line >= location.line
    };
    let grinds = program
        .source_order
        .iter()
        .skip(1)
        .take(constants.grinds.len())
        .collect::<Vec<_>>();
    let grinds_are_valid = grinds.len() == constants.grinds.len()
        && grinds.iter().zip(&constants.grinds).enumerate().all(
            |(index, (transition, constant))| {
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
            },
        );
    let fallback_thresholds = grinds
        .iter()
        .filter_map(|transition| match transition {
            CompiledRegularTransition::Grind {
                futures_fallback_loss_threshold,
                ..
            } => *futures_fallback_loss_threshold,
            _ => None,
        })
        .collect::<Vec<_>>();
    let derisks = program
        .source_order
        .iter()
        .skip(1 + constants.grinds.len())
        .collect::<Vec<_>>();
    let derisk_tags = derisks
        .iter()
        .filter_map(|transition| match transition {
            CompiledRegularTransition::Derisk { tag, .. } => Some(tag.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>();
    let level_one_tags = derisks
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
    let derisks_are_valid = derisks.len() == 2
        && derisk_tags.len() == 2
        && derisk_tags.iter().all(|tag| !tag.is_empty())
        && derisk_tags.iter().collect::<BTreeSet<_>>().len() == derisk_tags.len()
        && level_one_tags.as_slice() == [program.order_scan.derisk_level_one_tag.as_str()]
        && derisks.iter().all(|transition| {
            matches!(
                transition,
                CompiledRegularTransition::Derisk { location, .. }
                    if valid_location(location)
            )
        });
    TransitionSummary {
        grinds_are_valid,
        fallback_thresholds,
        derisks_are_valid,
        derisk_tags,
    }
}
