//! Validation for NFI adjustment constants and predicate IR.

use std::collections::BTreeSet;

use crate::domain::{
    NfiLegacyGrindConstants, NfiRegularAdjustmentConstants, NfiX7AdjustmentCondition,
    NfiX7AdjustmentConstants, NfiX7AdjustmentExpression, NfiX7AdjustmentOperand,
    NfiX7AdjustmentPolicy, NfiX7RebuyConstants,
};

const MAX_ADJUSTMENT_EXPRESSION_DEPTH: usize = 16;
const MAX_ADJUSTMENT_EXPRESSION_ARITY: usize = 32;

pub(crate) fn valid_nfi_rebuy_constants(constants: &NfiX7RebuyConstants) -> bool {
    let vectors = [
        (&constants.stakes_futures, &constants.thresholds_futures),
        (&constants.stakes_spot, &constants.thresholds_spot),
    ];
    vectors.iter().all(|(stakes, thresholds)| {
        !stakes.is_empty()
            && stakes.len() == thresholds.len()
            && stakes
                .iter()
                .chain(thresholds.iter())
                .all(|value| value.is_finite())
            && stakes.iter().all(|value| *value > 0.0)
    }) && constants.derisk_futures.is_finite()
        && constants.derisk_spot.is_finite()
        && constants.derisk_futures < 0.0
        && constants.derisk_spot < 0.0
}

pub(crate) fn valid_nfi_legacy_grind_constants(constants: &NfiLegacyGrindConstants) -> bool {
    let multipliers_are_valid = [
        &constants.stake_multipliers_futures,
        &constants.stake_multipliers_spot,
    ]
    .iter()
    .all(|values| {
        !values.is_empty() && values.iter().all(|value| value.is_finite() && *value > 0.0)
    });
    let clusters_are_valid = !constants.clusters.is_empty()
        && unique_cluster_tags(
            constants
                .clusters
                .iter()
                .map(|cluster| (&cluster.entry_tag, &cluster.stop_tag)),
        )
        && constants.clusters.iter().all(|cluster| {
            let vectors = [
                &cluster.stakes_futures,
                &cluster.stakes_spot,
                &cluster.thresholds_futures,
                &cluster.thresholds_spot,
            ];
            [
                cluster.stop_threshold_futures,
                cluster.stop_threshold_spot,
                cluster.profit_threshold_futures,
                cluster.profit_threshold_spot,
            ]
            .iter()
            .all(|value| value.is_finite())
                && vectors.iter().all(|values| {
                    !values.is_empty() && values.iter().all(|value| value.is_finite())
                })
                && cluster.stakes_futures.len() == cluster.thresholds_futures.len()
                && cluster.stakes_spot.len() == cluster.thresholds_spot.len()
        });
    constants.max_stake_multiplier.is_finite()
        && constants.max_stake_multiplier > 0.0
        && constants.derisk_1_reentry_futures.is_finite()
        && constants.derisk_1_reentry_spot.is_finite()
        && multipliers_are_valid
        && clusters_are_valid
}

pub(crate) fn valid_nfi_regular_adjustment_constants(
    constants: &NfiRegularAdjustmentConstants,
) -> bool {
    let policy = &constants.policy;
    let policy_is_valid = policy.entry_retry_ms > 0
        && policy.grind_force_order_age_ms > policy.entry_retry_ms
        && policy.grind_order_age_ms > policy.grind_force_order_age_ms
        && policy.rebuy_order_age_ms > policy.grind_order_age_ms
        && [
            policy.grind_entry_profit_gate,
            policy.additional_grind_profit_gate,
            policy.forced_age_profit_gate,
            policy.minimum_entry_multiplier,
            policy.minimum_remaining_multiplier,
        ]
        .iter()
        .all(|value| value.is_finite())
        && policy.grind_entry_profit_gate > policy.additional_grind_profit_gate
        && policy.additional_grind_profit_gate > policy.forced_age_profit_gate
        && policy.forced_age_profit_gate < 0.0
        && policy.minimum_entry_multiplier > 1.0
        && policy.minimum_remaining_multiplier > policy.minimum_entry_multiplier;
    let rebuy_is_valid = [
        (
            &constants.rebuy_stakes_futures,
            &constants.rebuy_thresholds_futures,
        ),
        (
            &constants.rebuy_stakes_spot,
            &constants.rebuy_thresholds_spot,
        ),
    ]
    .iter()
    .all(|(stakes, thresholds)| {
        !stakes.is_empty()
            && stakes.len() == thresholds.len()
            && stakes.iter().all(|value| value.is_finite() && *value > 0.0)
            && thresholds.iter().all(|value| value.is_finite())
    });
    let grinds_are_valid = !constants.grinds.is_empty()
        && unique_cluster_tags(
            constants
                .grinds
                .iter()
                .map(|grind| (&grind.entry_tag, &grind.stop_tag)),
        )
        && constants.grinds.iter().all(|grind| {
            [
                (&grind.stakes_futures, &grind.thresholds_futures),
                (&grind.stakes_spot, &grind.thresholds_spot),
            ]
            .iter()
            .all(|(stakes, thresholds)| {
                !stakes.is_empty()
                    && stakes.len() == thresholds.len()
                    && stakes.iter().all(|value| value.is_finite() && *value > 0.0)
                    && thresholds.iter().all(|value| value.is_finite())
            }) && grind.stop_threshold_futures.is_finite()
                && grind.stop_threshold_spot.is_finite()
                && grind.profit_threshold_futures.is_finite()
                && grind.profit_threshold_spot.is_finite()
        });
    constants.derisk_threshold_futures.is_finite()
        && constants.derisk_threshold_spot.is_finite()
        && constants.derisk_level_1_threshold_futures.is_finite()
        && constants.derisk_level_1_threshold_spot.is_finite()
        && policy_is_valid
        && rebuy_is_valid
        && grinds_are_valid
}

pub(crate) fn valid_nfi_adjustment_constants(constants: &NfiX7AdjustmentConstants) -> bool {
    let levels = constants
        .derisk_levels
        .iter()
        .map(|level| level.level)
        .collect::<Vec<_>>();
    let grinds = constants
        .grinds
        .iter()
        .map(|grind| grind.level)
        .collect::<Vec<_>>();
    let derisk_numbers_are_valid = constants.derisk_levels.iter().all(|level| {
        [
            level.threshold_futures,
            level.threshold_spot,
            level.stake_futures,
            level.stake_spot,
        ]
        .iter()
        .all(|value| value.is_finite())
            && level.stake_futures > 0.0
            && level.stake_spot > 0.0
    });
    let grind_numbers_are_valid = constants.grinds.iter().all(|grind| {
        let scalars = [
            grind.derisk_futures,
            grind.derisk_spot,
            grind.profit_threshold_futures,
            grind.profit_threshold_spot,
        ];
        let vectors = [
            &grind.stakes_futures,
            &grind.stakes_spot,
            &grind.thresholds_futures,
            &grind.thresholds_spot,
        ];
        scalars.iter().all(|value| value.is_finite())
            && vectors
                .iter()
                .all(|values| !values.is_empty() && values.iter().all(|value| value.is_finite()))
            && grind.stakes_futures.len() == grind.thresholds_futures.len()
            && grind.stakes_spot.len() == grind.thresholds_spot.len()
    });
    constants.max_stake_multiplier.is_finite()
        && constants.max_stake_multiplier > 0.0
        && constants
            .rebuy_stake_multiplier
            .is_none_or(|value| value.is_finite() && value > 0.0)
        && contiguous_levels(&levels)
        && contiguous_levels(&grinds)
        && derisk_numbers_are_valid
        && grind_numbers_are_valid
}

pub(crate) fn valid_nfi_adjustment_policy(
    policy: &NfiX7AdjustmentPolicy,
    derisk_level_count: usize,
    grind_level_count: usize,
) -> bool {
    let fallback_levels = policy
        .grind_entry_fallbacks
        .iter()
        .map(|fallback| fallback.level)
        .collect::<Vec<_>>();
    let valid_derisk_levels = |levels: &[usize]| {
        !levels.is_empty()
            && levels.windows(2).all(|pair| pair[0] < pair[1])
            && levels
                .iter()
                .all(|level| (1..=derisk_level_count).contains(level))
    };
    let fallbacks_are_valid = policy.grind_entry_fallbacks.iter().all(|fallback| {
        fallback.predicates.iter().all(|predicate| {
            if let Some(expression) = &predicate.expression {
                predicate.any_derisk_levels.is_empty()
                    && predicate.conditions.is_empty()
                    && valid_nfi_adjustment_expression(expression, derisk_level_count, 1)
            } else {
                (predicate.any_derisk_levels.is_empty()
                    || valid_derisk_levels(&predicate.any_derisk_levels))
                    && !predicate.conditions.is_empty()
                    && predicate
                        .conditions
                        .iter()
                        .all(valid_nfi_adjustment_condition)
            }
        })
    });

    policy.entry_retry_ms > 0
        && policy.stale_order_ms > policy.entry_retry_ms
        && valid_derisk_levels(&policy.extra_entry_derisk_levels)
        && valid_nfi_adjustment_condition(&policy.extra_entry_profit_condition)
        && fallback_levels == (1..=grind_level_count).collect::<Vec<_>>()
        && fallbacks_are_valid
}

fn contiguous_levels(levels: &[usize]) -> bool {
    !levels.is_empty() && levels.iter().copied().eq(1..=levels.len())
}

fn unique_cluster_tags<'a>(mut tags: impl Iterator<Item = (&'a String, &'a String)>) -> bool {
    let mut seen = BTreeSet::new();
    tags.all(|(entry, stop)| {
        !entry.is_empty()
            && !stop.is_empty()
            && !entry.chars().any(char::is_whitespace)
            && !stop.chars().any(char::is_whitespace)
            && seen.insert(entry)
            && seen.insert(stop)
    })
}

pub(crate) fn valid_nfi_adjustment_condition(condition: &NfiX7AdjustmentCondition) -> bool {
    valid_nfi_legacy_adjustment_operand(&condition.left)
        && valid_nfi_legacy_adjustment_operand(&condition.right)
}

fn valid_nfi_legacy_adjustment_operand(operand: &NfiX7AdjustmentOperand) -> bool {
    match operand {
        NfiX7AdjustmentOperand::Literal { value } => value.is_finite(),
        NfiX7AdjustmentOperand::Variable { name } => matches!(
            name.as_str(),
            "slice_profit"
                | "slice_profit_entry"
                | "slice_profit_exit"
                | "num_open_grinds_and_buybacks"
        ),
        NfiX7AdjustmentOperand::Feature { name, multiplier } => {
            !name.is_empty() && multiplier.is_finite()
        }
        NfiX7AdjustmentOperand::Trade { .. } => false,
    }
}

pub(crate) fn valid_nfi_adjustment_operand(operand: &NfiX7AdjustmentOperand) -> bool {
    match operand {
        NfiX7AdjustmentOperand::Literal { value } => value.is_finite(),
        NfiX7AdjustmentOperand::Variable { name } => matches!(
            name.as_str(),
            "current_rate"
                | "slice_profit"
                | "slice_profit_entry"
                | "slice_profit_exit"
                | "num_open_grinds_and_buybacks"
        ),
        NfiX7AdjustmentOperand::Feature { name, multiplier } => {
            !name.is_empty() && multiplier.is_finite()
        }
        NfiX7AdjustmentOperand::Trade { name, multiplier } => {
            name == "liquidation_price" && multiplier.is_finite()
        }
    }
}

fn valid_nfi_adjustment_expression(
    expression: &NfiX7AdjustmentExpression,
    derisk_level_count: usize,
    depth: usize,
) -> bool {
    if depth > MAX_ADJUSTMENT_EXPRESSION_DEPTH {
        return false;
    }
    let valid_values = |values: &[NfiX7AdjustmentExpression]| {
        !values.is_empty()
            && values.len() <= MAX_ADJUSTMENT_EXPRESSION_ARITY
            && values
                .iter()
                .all(|value| valid_nfi_adjustment_expression(value, derisk_level_count, depth + 1))
    };
    match expression {
        NfiX7AdjustmentExpression::All { values } | NfiX7AdjustmentExpression::Any { values } => {
            valid_values(values)
        }
        NfiX7AdjustmentExpression::Not { value } => {
            valid_nfi_adjustment_expression(value, derisk_level_count, depth + 1)
        }
        NfiX7AdjustmentExpression::Flag { name } => {
            matches!(name.as_str(), "is_futures_mode" | "trade_is_short")
        }
        NfiX7AdjustmentExpression::DeriskFound { level } => {
            (1..=derisk_level_count).contains(level)
        }
        NfiX7AdjustmentExpression::Present { operand } => valid_nfi_adjustment_operand(operand),
        NfiX7AdjustmentExpression::Comparison { left, right, .. } => {
            valid_nfi_adjustment_operand(left) && valid_nfi_adjustment_operand(right)
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use crate::domain::{NfiX7AdjustmentExpression, NfiX7AdjustmentPredicate};

    use super::{
        valid_nfi_adjustment_expression, MAX_ADJUSTMENT_EXPRESSION_ARITY,
        MAX_ADJUSTMENT_EXPRESSION_DEPTH,
    };

    #[test]
    fn legacy_adjustment_predicate_deserializes_without_expression() {
        let predicate: NfiX7AdjustmentPredicate = serde_json::from_value(json!({
            "any_derisk_levels": [1],
            "conditions": [{
                "left": {"kind": "variable", "name": "slice_profit"},
                "operator": "lt",
                "right": {"kind": "literal", "value": -0.03}
            }]
        }))
        .expect("legacy predicate remains compatible");

        assert!(predicate.expression.is_none());
    }

    #[test]
    fn adjustment_expression_rejects_invalid_names_and_shapes() {
        let invalid = [
            json!({"op": "flag", "name": "strategy_specific_mode"}),
            json!({
                "op": "comparison",
                "left": {"kind": "variable", "name": "unknown_rate"},
                "operator": "lt",
                "right": {"kind": "literal", "value": 1.0}
            }),
            json!({
                "op": "present",
                "operand": {"kind": "trade", "name": "open_rate", "multiplier": 1.0}
            }),
            json!({"op": "derisk_found", "level": 4}),
            json!({"op": "all", "values": []}),
        ];
        for value in invalid {
            let expression: NfiX7AdjustmentExpression =
                serde_json::from_value(value).expect("structurally valid expression");
            assert!(!valid_nfi_adjustment_expression(&expression, 3, 1));
        }
    }

    #[test]
    fn adjustment_expression_rejects_excessive_depth_and_arity() {
        let wide = NfiX7AdjustmentExpression::All {
            values: (0..=MAX_ADJUSTMENT_EXPRESSION_ARITY)
                .map(|_| NfiX7AdjustmentExpression::Flag {
                    name: "is_futures_mode".to_owned(),
                })
                .collect(),
        };
        assert!(!valid_nfi_adjustment_expression(&wide, 3, 1));

        let mut deep = NfiX7AdjustmentExpression::Flag {
            name: "is_futures_mode".to_owned(),
        };
        for _ in 0..MAX_ADJUSTMENT_EXPRESSION_DEPTH {
            deep = NfiX7AdjustmentExpression::Not {
                value: Box::new(deep),
            };
        }
        assert!(!valid_nfi_adjustment_expression(&deep, 3, 1));
    }
}
