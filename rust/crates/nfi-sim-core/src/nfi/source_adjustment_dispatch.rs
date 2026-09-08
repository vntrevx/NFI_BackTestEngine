//! Source-compiled routing for managed and legacy position adjustments.
#![allow(clippy::option_option)] // Invalid program/state differs from callback no-op.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::domain::{AdjustmentSignal, ManagedAdjustmentDispatch, NfiX7TradeManager};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::evaluate_scalar_decision_program;

use super::exit::managed_exit_matcher_matches;
use super::{
    compiled_rebuy_delegates, evaluate_nfi_legacy_grind_adjustment, evaluate_nfi_rebuy_adjustment,
    evaluate_nfi_short_rebuy_adjustment, evaluate_nfi_system_v3_adjustment,
    PositionAdjustmentRequest,
};

fn select_target(
    program: &ManagedAdjustmentDispatch,
    system_version: Option<&str>,
    tags: &[String],
    side: TradeSide,
) -> Option<Value> {
    if system_version != Some(program.system_version.as_str()) {
        return None;
    }
    let mut variables =
        BTreeMap::from([("is_short".to_owned(), Value::Bool(side == TradeSide::Short))]);
    variables.extend(program.predicates.iter().map(|(name, matcher)| {
        (
            name.clone(),
            Value::Bool(managed_exit_matcher_matches(matcher, tags, side)),
        )
    }));
    evaluate_scalar_decision_program(&program.program, &variables)
}

pub(super) fn evaluate(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let target = select_target(
        manager.adjustment_dispatch.as_ref()?,
        trade
            .custom_data
            .get("system_version")
            .and_then(Value::as_str),
        trade.entry_tag_words(),
        trade.side,
    )?;
    if target.is_null() {
        return Some(None);
    }
    let target = target.as_str()?;
    match (trade.side, target) {
        (TradeSide::Long, "long-rebuy") | (TradeSide::Short, "short-rebuy") => {
            evaluate_rebuy(manager, trade, request)
        }
        (TradeSide::Long, "long-system") => evaluate_nfi_system_v3_adjustment(
            manager,
            manager.position_adjustment.as_ref()?,
            TradeSide::Long,
            trade,
            request,
            1.0,
            false,
        ),
        (TradeSide::Short, "short-system") => evaluate_nfi_system_v3_adjustment(
            manager,
            manager.short_position_adjustment.as_ref()?,
            TradeSide::Short,
            trade,
            request,
            1.0,
            false,
        ),
        (TradeSide::Long, "long-legacy") | (TradeSide::Short, "short-legacy") => {
            evaluate_legacy(manager, trade, request)
        }
        _ => None,
    }
}

fn evaluate_rebuy(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let is_short = trade.side == TradeSide::Short;
    let program = if is_short {
        manager.short_rebuy_adjustment.program.as_ref()?
    } else {
        manager.rebuy_adjustment.program.as_ref()?
    };
    if compiled_rebuy_delegates(program, trade) {
        let adjustment = if is_short {
            manager.short_position_adjustment.as_ref()?
        } else {
            manager.position_adjustment.as_ref()?
        };
        return evaluate_nfi_system_v3_adjustment(
            manager,
            adjustment,
            trade.side,
            trade,
            request,
            adjustment.constants.rebuy_stake_multiplier?,
            true,
        );
    }
    if is_short {
        evaluate_nfi_short_rebuy_adjustment(
            &manager.short_rebuy_adjustment,
            trade,
            request.pair,
            request.candle_index,
            request.candle,
            request.config,
            request.available_balance,
        )
    } else {
        evaluate_nfi_rebuy_adjustment(
            &manager.rebuy_adjustment,
            trade,
            request.pair,
            request.candle_index,
            request.candle,
            request.config,
            request.available_balance,
        )
    }
}

fn evaluate_legacy(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let tags = trade.entry_tag_words();
    let candidates = if trade.side == TradeSide::Short {
        [manager.short_grind.as_ref(), None]
    } else {
        [manager.long_grind.as_ref(), manager.long_btc.as_ref()]
    };
    let route = candidates.into_iter().flatten().find(|route| {
        !tags.is_empty()
            && tags
                .iter()
                .all(|tag| route.entry_tags.iter().any(|word| word == tag))
    })?;
    evaluate_nfi_legacy_grind_adjustment(
        manager,
        route,
        trade,
        request.pair,
        request.candle_index,
        request.candle,
        request.config,
        request.available_balance,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_router_preserves_compound_and_legacy_routes() {
        let program: ManagedAdjustmentDispatch = serde_json::from_str(include_str!(
            "../../../../../benchmarks/fixtures/source-contracts/x8-system-v4/dispatch-program.json"
        ))
        .expect("source-compiled dispatch fixture");
        let cases = [
            (TradeSide::Long, "1", Some("long-system")),
            (TradeSide::Long, "61", Some("long-rebuy")),
            (TradeSide::Long, "61 120", Some("long-rebuy")),
            (TradeSide::Long, "120", Some("long-legacy")),
            (TradeSide::Long, "121", Some("long-legacy")),
            (TradeSide::Long, "61 501", None),
            (TradeSide::Long, "1 561", Some("long-system")),
            (TradeSide::Short, "501", Some("short-system")),
            (TradeSide::Short, "561 620", Some("short-rebuy")),
            (TradeSide::Short, "620", Some("short-legacy")),
            (TradeSide::Short, "561 1", None),
            (TradeSide::Short, "501 61", Some("short-system")),
        ];
        for (side, words, expected) in cases {
            let tags = words
                .split_whitespace()
                .map(str::to_owned)
                .collect::<Vec<_>>();
            assert_eq!(
                select_target(&program, Some(&program.system_version), &tags, side),
                Some(expected.map_or(Value::Null, |value| Value::String(value.to_owned()))),
                "{side:?} {words}",
            );
            assert!(select_target(&program, None, &tags, side).is_none());
            assert!(select_target(&program, Some("different-system"), &tags, side).is_none());
        }
    }
}
