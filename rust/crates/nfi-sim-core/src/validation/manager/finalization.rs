//! Manager constants, callback system write, and runtime finalization.

use std::collections::BTreeSet;

use crate::domain::{
    ManagedCustomExitPrefix, ManagedExitExecutionMode, ManagedExitTagOperator, NfiX7TradeManager,
    PortfolioConfig,
};

pub(super) fn static_contract_is_valid(
    config: &PortfolioConfig,
    manager: &NfiX7TradeManager,
) -> bool {
    let constants = &manager.constants;
    let thresholds = [
        constants.stop_threshold_futures,
        constants.stop_threshold_spot,
        constants.system_v3_2_stop_threshold_doom_futures,
        constants.system_v3_2_stop_threshold_doom_spot,
    ];
    let constants_are_valid = !constants.system_name_use.is_empty()
        && (constants.system_name_use == constants.system_v3_2_name
            || manager.system_exit_programs.is_some())
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
    constants_are_valid
        && virtual_fees_are_valid(manager)
        && has_system_write
        && config.custom_exit_program.is_none()
        && dispatch_is_valid(config, manager)
        && source_exits_are_valid(manager)
}

fn virtual_fees_are_valid(manager: &NfiX7TradeManager) -> bool {
    manager.virtual_fees.as_ref().is_none_or(|fees| {
        manager.system_exit_programs.is_some()
            && [fees.open_rate, fees.close_rate]
                .into_iter()
                .flatten()
                .all(|rate| rate.is_finite() && (0.0..1.0).contains(&rate))
    })
}

fn source_exits_are_valid(manager: &NfiX7TradeManager) -> bool {
    let Some(system) = manager.system_exit_programs.as_ref() else {
        return manager.custom_exit_prefix.is_none() && manager.adjustment_dispatch.is_none();
    };
    let Some(prefix) = manager.custom_exit_prefix.as_ref() else {
        return false;
    };
    let target = &system.profit_target;
    let programs = [&system.long_stop, &system.short_stop, &target.program];
    let tags = target.derisk.order_tags.iter().collect::<BTreeSet<_>>();
    manager.schema_version == "0.31.0"
        && manager.adjustment_dispatch.is_some()
        && system.system_version == manager.constants.system_name_use
        && programs
            .iter()
            .all(|program| super::valid_scalar_program(program))
        && [
            &manager.managed_exit_program,
            &manager.managed_short_exit_program,
        ]
        .iter()
        .all(|program| {
            program
                .as_ref()
                .is_some_and(|program| program.execution_mode == ManagedExitExecutionMode::Primary)
        })
        && target.derisk.amount_ratio.is_finite()
        && target.derisk.amount_ratio > 0.0
        && target.derisk.amount_ratio <= 1.0
        && !tags.is_empty()
        && tags.len() == target.derisk.order_tags.len()
        && tags.iter().all(|tag| !tag.is_empty())
        && target.predicates.values().all(|matcher| {
            super::managed_exit_contract::valid_managed_exit_matcher(
                matcher,
                &BTreeSet::new(),
                &mut BTreeSet::new(),
                0,
                true,
                false,
            )
        })
        && crate::validation::config::valid_scalar_program_bundle(&prefix.bundle)
        && optional_columns_match_program(prefix)
        && prefix.optional_columns.iter().all(|name| !name.is_empty())
        && prefix
            .optional_columns
            .iter()
            .collect::<BTreeSet<_>>()
            .len()
            == prefix.optional_columns.len()
}

pub(super) fn source_disables_adjustments(manager: &NfiX7TradeManager) -> bool {
    let Some(dispatch) = manager.adjustment_dispatch.as_ref() else {
        return false;
    };
    [
        manager.position_adjustment.as_ref(),
        manager.short_position_adjustment.as_ref(),
    ]
    .iter()
    .all(|adjustment| adjustment.is_some_and(|value| !value.enabled))
        && !manager.rebuy_adjustment.enabled
        && !manager.short_rebuy_adjustment.enabled
        && returns_none_immediately(&dispatch.program)
}

fn returns_none_immediately(program: &crate::domain::ScalarDecisionProgram) -> bool {
    let Some(statement) = program
        .statements
        .first()
        .and_then(serde_json::Value::as_array)
    else {
        return false;
    };
    statement.len() == 2
        && statement[0].as_str() == Some("return")
        && statement[1]
            .as_u64()
            .and_then(|index| usize::try_from(index).ok())
            .and_then(|index| program.expressions.get(index))
            == Some(&serde_json::json!(["literal", null]))
}

fn optional_columns_match_program(prefix: &ManagedCustomExitPrefix) -> bool {
    let mut columns = BTreeSet::new();
    for program in prefix.bundle.programs.values() {
        for expression in &program.expressions {
            if expression.get(0).and_then(serde_json::Value::as_str) != Some("mapping-get") {
                continue;
            }
            let operand = |position| {
                expression
                    .get(position)?
                    .as_u64()
                    .and_then(|index| usize::try_from(index).ok())
                    .and_then(|index| program.expressions.get(index))
            };
            let (Some(base), Some(key)) = (operand(1), operand(2)) else {
                return false;
            };
            if base != &serde_json::json!(["variable", "last_candle"])
                || key.get(0).and_then(serde_json::Value::as_str) != Some("literal")
            {
                return false;
            }
            let Some(name) = key.get(1).and_then(serde_json::Value::as_str) else {
                return false;
            };
            columns.insert(name);
        }
    }
    columns == prefix.optional_columns.iter().map(String::as_str).collect()
}

fn dispatch_is_valid(config: &PortfolioConfig, manager: &NfiX7TradeManager) -> bool {
    let Some(dispatch) = manager.adjustment_dispatch.as_ref() else {
        return true;
    };
    let mut parameters = dispatch
        .predicates
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    parameters.insert("is_short");
    let guarded_initial_state = config
        .callback_program
        .as_ref()
        .and_then(|program| program.order_filled.as_ref())
        .is_some_and(|program| program.initial_entry_requires_no_exits);
    manager.schema_version == "0.31.0"
        && guarded_initial_state
        && dispatch.schema_version == "adjustment-dispatch-program-v1"
        && dispatch.system_version == manager.constants.system_name_use
        && !dispatch.predicates.is_empty()
        && !dispatch.predicates.contains_key("is_short")
        && dispatch.program.parameters.len() == parameters.len()
        && dispatch
            .program
            .parameters
            .iter()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == parameters
        && super::shared::valid_sha256(&dispatch.fingerprint)
        && super::valid_scalar_program(&dispatch.program)
        && dispatch.predicates.values().all(|matcher| {
            matches!(
                matcher.operator,
                ManagedExitTagOperator::Any | ManagedExitTagOperator::All
            ) && super::managed_exit_contract::valid_managed_exit_matcher(
                matcher,
                &BTreeSet::new(),
                &mut BTreeSet::new(),
                0,
                false,
                false,
            )
        })
}

pub(super) fn runtime_is_valid(manager: &NfiX7TradeManager) -> bool {
    manager.initialize_feature_projection_caches() && manager.runtime_dispatch().is_some()
}

#[cfg(test)]
mod tests {
    #[test]
    fn disabled_dispatch_requires_an_unconditional_null_return() {
        let mut program: crate::domain::ScalarDecisionProgram =
            serde_json::from_value(serde_json::json!({
                "schema_version": "1.2.0", "opcode": "scalar-decision-program-v1",
                "parameters": [], "expressions": [["literal", null]],
                "statements": [["return", 0]]
            }))
            .expect("scalar program");
        assert!(super::returns_none_immediately(&program));
        program.expressions[0] = serde_json::json!(["literal", false]);
        assert!(!super::returns_none_immediately(&program));
        program.expressions[0] = serde_json::json!(["literal", null]);
        program.statements[0] = serde_json::json!(["if", 0, [["return", 0]], []]);
        assert!(!super::returns_none_immediately(&program));
    }

    use super::*;

    #[test]
    fn optional_columns_cannot_hide_a_source_candle_read() {
        let mut prefix: ManagedCustomExitPrefix = serde_json::from_str(include_str!(
            "../../../../../../benchmarks/fixtures/source-contracts/x8-system-v4/prefix-program.json"
        )).unwrap();
        assert!(optional_columns_match_program(&prefix));
        prefix.optional_columns.pop();
        assert!(!optional_columns_match_program(&prefix));
    }
}
