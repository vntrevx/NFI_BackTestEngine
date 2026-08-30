//! Focused NFI trade-manager validation.

use crate::domain::{CompiledSystemAdjustmentAction, CompiledSystemGrindTags};

use super::{
    shared, valid_scalar_program, BTreeSet, CompiledOrderSide, CompiledSystemAdjustmentActionKind,
    CompiledSystemAdjustmentExecutionMode, CompiledSystemAdjustmentInputKind,
    CompiledSystemAdjustmentProgram, CompiledSystemAdjustmentSide, NfiX7PositionAdjustment,
};

pub(crate) fn valid_system_adjustment_binding_level(
    kind: CompiledSystemAdjustmentInputKind,
    level: Option<usize>,
    derisk_levels: &[usize],
    grind_levels: &[usize],
) -> bool {
    match kind {
        CompiledSystemAdjustmentInputKind::DeriskFound
        | CompiledSystemAdjustmentInputKind::DeriskEnabled
        | CompiledSystemAdjustmentInputKind::DeriskStake
        | CompiledSystemAdjustmentInputKind::DeriskThreshold => {
            level.is_some_and(|value| derisk_levels.contains(&value))
        }
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
        | CompiledSystemAdjustmentInputKind::ClusterMaximumProfitRate => {
            level.is_some_and(|value| grind_levels.contains(&value))
        }
        _ => level.is_none(),
    }
}

fn valid_system_maximum_keys(schema_version: &str, levels: &[CompiledSystemGrindTags]) -> bool {
    levels.iter().all(|record| {
        let complete = record
            .maximum_profit_stake_key
            .as_ref()
            .is_some_and(|key| !key.is_empty())
            && record
                .maximum_profit_rate_key
                .as_ref()
                .is_some_and(|key| !key.is_empty());
        let absent =
            record.maximum_profit_stake_key.is_none() && record.maximum_profit_rate_key.is_none();
        complete || (schema_version == "0.31.0" && absent)
    })
}

pub(super) fn valid_versioned_system_adjustment_program(
    schema_version: &str,
    program: Option<&CompiledSystemAdjustmentProgram>,
    adjustment: &NfiX7PositionAdjustment,
    expected_side: CompiledSystemAdjustmentSide,
) -> bool {
    let required = matches!(
        schema_version,
        "0.24.0" | "0.25.0" | "0.26.0" | "0.27.0" | "0.28.0" | "0.29.0" | "0.30.0" | "0.31.0"
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
    let expected_program_version = if schema_version == "0.31.0" {
        "system-adjustment-program-v2"
    } else {
        "system-adjustment-program-v1"
    };
    let maximum_keys_valid =
        valid_system_maximum_keys(schema_version, &program.order_scan.grind_levels);
    program.schema_version == expected_program_version
        && program.execution_mode
            == if matches!(schema_version, "0.29.0" | "0.30.0" | "0.31.0") {
                CompiledSystemAdjustmentExecutionMode::Primary
            } else {
                CompiledSystemAdjustmentExecutionMode::PrimaryWithLegacyShadow
            }
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
        && maximum_keys_valid
        && program.retry_policy.entry_retry_ms > 0
        && program.retry_policy.stale_order_ms > 0
        && adjustment.constants.policy.as_ref().is_some_and(|policy| {
            policy.entry_retry_ms == program.retry_policy.entry_retry_ms
                && policy.stale_order_ms == program.retry_policy.stale_order_ms
        })
        && program.input_contract.is_object()
        && program.location.line > 0
        && program.location.end_line >= program.location.line
        && shared::valid_sha256(&program.fingerprint)
        && program
            .source_order
            .iter()
            .all(|action| valid_action(program, action, &derisk_levels, &grind_levels))
}

fn valid_action(
    program: &CompiledSystemAdjustmentProgram,
    action: &CompiledSystemAdjustmentAction,
    derisk_levels: &[usize],
    grind_levels: &[usize],
) -> bool {
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
            !action.append_entry_ids && derisk.is_some_and(|record| record.tag == action.tag)
        }
        CompiledSystemAdjustmentActionKind::GrindEntry => {
            !action.append_entry_ids && grind.is_some_and(|record| record.entry_tag == action.tag)
        }
        CompiledSystemAdjustmentActionKind::GrindExit => {
            action.append_entry_ids && grind.is_some_and(|record| record.exit_tag == action.tag)
        }
        CompiledSystemAdjustmentActionKind::GrindDerisk => {
            action.append_entry_ids && grind.is_some_and(|record| record.derisk_tag == action.tag)
        }
    };
    let binding_names = action
        .bindings
        .iter()
        .map(|binding| binding.name.as_str())
        .collect::<BTreeSet<_>>();
    let valid_bindings = action.bindings.iter().all(|binding| {
        let maximum_binding_valid = match binding.kind {
            CompiledSystemAdjustmentInputKind::ClusterMaximumProfitStake
            | CompiledSystemAdjustmentInputKind::ClusterMaximumProfitRate => binding
                .level
                .and_then(|level| {
                    program
                        .order_scan
                        .grind_levels
                        .iter()
                        .find(|record| record.level == level)
                })
                .is_some_and(|record| {
                    record.maximum_profit_stake_key.is_some()
                        && record.maximum_profit_rate_key.is_some()
                }),
            _ => true,
        };
        !binding.name.is_empty()
            && maximum_binding_valid
            && valid_system_adjustment_binding_level(
                binding.kind,
                binding.level,
                derisk_levels,
                grind_levels,
            )
    });
    action_contract
        && !action.tag.is_empty()
        && valid_scalar_program(&action.decision_program)
        && action.input_contract.is_object()
        && action.location.line > 0
        && action.location.end_line >= action.location.line
        && binding_names.len() == action.bindings.len()
        && valid_bindings
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{valid_system_maximum_keys, CompiledSystemGrindTags};

    fn level(stake_key: Option<&str>, rate_key: Option<&str>) -> CompiledSystemGrindTags {
        serde_json::from_value(json!({
            "level": 1,
            "entry_tag": "entry",
            "exit_tag": "exit",
            "derisk_tag": "derisk",
            "maximum_profit_stake_key": stake_key,
            "maximum_profit_rate_key": rate_key,
            "minimum_scale_leverage": "trade-leverage"
        }))
        .expect("valid system Grind level")
    }

    #[test]
    fn maximum_keys_remain_required_for_v1_and_optional_as_pairs_for_v2() {
        let complete = level(Some("stake"), Some("rate"));
        let absent = level(None, None);
        let partial = level(Some("stake"), None);

        assert!(valid_system_maximum_keys("0.30.0", &[complete.clone()]));
        assert!(!valid_system_maximum_keys("0.30.0", &[absent.clone()]));
        assert!(valid_system_maximum_keys("0.31.0", &[complete]));
        assert!(valid_system_maximum_keys("0.31.0", &[absent]));
        assert!(!valid_system_maximum_keys("0.31.0", &[partial]));
        assert!(!valid_system_maximum_keys(
            "0.31.0",
            &[level(Some(""), Some("rate"))],
        ));
    }
}
