//! Focused NFI trade-manager validation.

use super::{
    valid_scalar_program, BTreeSet, ManagedExitExecutionMode, ManagedExitInlinePosition,
    ManagedExitRoute, ManagedExitStateOperation, ManagedExitStateProgram, ManagedExitStopPolicy,
    ManagedExitTagMatcher, ManagedExitTagOperator,
};

pub(super) fn valid_managed_exit_execution_mode(
    schema_version: &str,
    mode: ManagedExitExecutionMode,
) -> bool {
    if matches!(
        schema_version,
        "0.21.0"
            | "0.22.0"
            | "0.23.0"
            | "0.24.0"
            | "0.25.0"
            | "0.26.0"
            | "0.27.0"
            | "0.28.0"
            | "0.29.0"
            | "0.30.0"
            | "0.31.0"
    ) {
        mode == if matches!(schema_version, "0.29.0" | "0.30.0" | "0.31.0") {
            ManagedExitExecutionMode::Primary
        } else {
            ManagedExitExecutionMode::PrimaryWithLegacyShadow
        }
    } else {
        mode == ManagedExitExecutionMode::Shadow
    }
}

pub(super) fn valid_managed_exit_terminal(
    route: &ManagedExitRoute,
    matcher_tags: &BTreeSet<&str>,
) -> bool {
    route.terminal_exit.as_ref().is_none_or(|terminal| {
        let tags = terminal
            .entry_tags
            .iter()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        !tags.is_empty()
            && tags.len() == terminal.entry_tags.len()
            && tags.iter().all(|tag| matcher_tags.contains(tag))
            && terminal.minimum_age_ms > 0
            && terminal.minimum_profit_ratio.is_finite()
            && !terminal.reason.is_empty()
    })
}

pub(super) fn valid_managed_exit_state_program(
    state: &ManagedExitStateProgram,
    source_helper: &str,
    known_tags: &BTreeSet<&str>,
    require_scalp_matcher: bool,
) -> bool {
    let expected_order = match state.inline_exit.as_ref().map(|inline| inline.position) {
        Some(ManagedExitInlinePosition::BeforeStop) => vec![
            ManagedExitStateOperation::InlineExit,
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
        Some(ManagedExitInlinePosition::AfterStop) => vec![
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::InlineExit,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
        None => vec![
            ManagedExitStateOperation::Stop,
            ManagedExitStateOperation::ExistingTarget,
            ManagedExitStateOperation::TargetUpdate,
            ManagedExitStateOperation::FinalFilter,
            ManagedExitStateOperation::TerminalExit,
        ],
    };
    let valid_inline = state.inline_exit.as_ref().is_none_or(|inline| {
        inline.minimum_profit.is_finite()
            && inline.maximum_profit.is_finite()
            && inline.minimum_profit < inline.maximum_profit
            && valid_scalar_program(&inline.program)
    });
    let valid_stop = match &state.stop {
        ManagedExitStopPolicy::SourceHelper { helper } => helper == source_helper,
        ManagedExitStopPolicy::StakeThreshold {
            futures_threshold,
            spot_threshold,
            ..
        } => {
            futures_threshold.is_finite()
                && *futures_threshold >= 0.0
                && spot_threshold.is_finite()
                && *spot_threshold >= 0.0
        }
    };
    let target = &state.target;
    let valid_scalp_matcher = match (
        target.pure_scalp_trailing,
        target.pure_scalp_matcher.as_ref(),
    ) {
        (true, Some(matcher)) => {
            let mut matcher_tags = BTreeSet::new();
            valid_managed_exit_matcher(matcher, known_tags, &mut matcher_tags, 0, false, true)
                && !matcher_tags.is_empty()
        }
        (true, None) => !require_scalp_matcher,
        (false, None) => true,
        (false, Some(_)) => false,
    };
    state.stateful_order == expected_order
        && valid_inline
        && valid_stop
        && valid_scalp_matcher
        && target.u_e_raise_delta.is_finite()
        && target.u_e_raise_delta >= 0.0
        && target.profit_raise_delta.is_finite()
        && target.profit_raise_delta >= 0.0
        && target.max_target_floor.is_finite()
        && target.max_target_floor >= 0.0
}

pub(super) fn valid_managed_exit_matcher<'a>(
    matcher: &'a ManagedExitTagMatcher,
    known_tags: &BTreeSet<&str>,
    collected_tags: &mut BTreeSet<&'a str>,
    depth: usize,
    allow_side_operators: bool,
    enforce_known_tags: bool,
) -> bool {
    if depth >= 8 {
        return false;
    }
    match matcher.operator {
        ManagedExitTagOperator::Any | ManagedExitTagOperator::All => {
            let tags = matcher
                .entry_tags
                .iter()
                .map(String::as_str)
                .collect::<BTreeSet<_>>();
            matcher.operands.is_empty()
                && !tags.is_empty()
                && tags.len() == matcher.entry_tags.len()
                && (!enforce_known_tags || tags.iter().all(|tag| known_tags.contains(tag)))
                && {
                    collected_tags.extend(tags);
                    true
                }
        }
        ManagedExitTagOperator::AnyOf | ManagedExitTagOperator::AllOf => {
            matcher.entry_tags.is_empty()
                && matcher.operands.len() >= 2
                && matcher.operands.iter().all(|operand| {
                    valid_managed_exit_matcher(
                        operand,
                        known_tags,
                        collected_tags,
                        depth + 1,
                        allow_side_operators,
                        enforce_known_tags,
                    )
                })
        }
        ManagedExitTagOperator::Not => {
            allow_side_operators
                && matcher.entry_tags.is_empty()
                && matcher.operands.len() == 1
                && valid_managed_exit_matcher(
                    &matcher.operands[0],
                    known_tags,
                    collected_tags,
                    depth + 1,
                    allow_side_operators,
                    enforce_known_tags,
                )
        }
        ManagedExitTagOperator::IsShort => {
            allow_side_operators && matcher.entry_tags.is_empty() && matcher.operands.is_empty()
        }
    }
}
