//! NFI position-adjustment route dispatch.
#![allow(clippy::option_option)] // Outer None rejects invalid state; inner None is a valid no-op.

use crate::domain::{
    AdjustmentSignal, Candle, NfiLongGrindRoute, NfiX7TradeManager, PairSeries, PortfolioConfig,
};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::validation::{nfi_managed_route_supports_tags, nfi_managed_short_route_supports_tags};

use super::dispatch_plan::{all_in_scope, any_in_scope};
use super::{
    compiled_rebuy_delegates, evaluate_nfi_legacy_grind_adjustment, evaluate_nfi_rebuy_adjustment,
    evaluate_nfi_regular_adjustment, evaluate_nfi_short_rebuy_adjustment,
    evaluate_nfi_system_v3_adjustment, PositionAdjustmentRequest, RegularAdjustmentOutcome,
};

pub(crate) fn evaluate_nfi_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let dispatch = manager.runtime_dispatch()?;
    let tags = dispatch.intern_trade_tags(trade);
    if trade.side == TradeSide::Short {
        return evaluate_nfi_short_position_adjustment(manager, trade, request);
    }
    let mut initial_stake_multiplier = 1.0;
    let mut rebuy_mode = false;
    if let Some(route) = dispatch
        .long_rebuy_route
        .and_then(|index| manager.managed_long_routes.get(index))
    {
        if nfi_managed_route_supports_tags(manager, route, tags.words) {
            let delegates = if let Some(program) = manager.rebuy_adjustment.program.as_ref() {
                compiled_rebuy_delegates(program, trade)
            } else {
                trade
                    .orders
                    .iter()
                    .find(|order| !order.is_entry)
                    .and_then(|order| order.tag.as_deref())
                    == Some("derisk_level_3")
            };
            if !delegates {
                return evaluate_nfi_rebuy_adjustment(
                    &manager.rebuy_adjustment,
                    trade,
                    request.pair,
                    request.candle_index,
                    request.candle,
                    request.config,
                    request.available_balance,
                );
            }
            // X7 permanently transfers a rebuy trade to the shared grind-v3
            // state machine after its first level-3 de-risk fill. The source
            // first reverses the reduced rebuy entry stake back to the normal
            // slice size; schema 0.9 did not carry this constant and must fail
            // closed if such a transition is reached.
            initial_stake_multiplier = manager
                .position_adjustment
                .as_ref()?
                .constants
                .rebuy_stake_multiplier?;
            rebuy_mode = true;
        }
    }
    if let Some(route) = manager.long_grind.as_ref() {
        let generic_match = all_in_scope(&tags, &dispatch.long_grind_tags);
        if generic_match != nfi_long_grind_supports_trade(route, trade) {
            return None;
        }
        if generic_match {
            return evaluate_nfi_legacy_grind_adjustment(
                manager,
                route,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
    }
    if let Some(route) = manager.long_btc.as_ref() {
        let generic_match = all_in_scope(&tags, &dispatch.long_btc_tags);
        if generic_match != nfi_long_grind_supports_trade(route, trade) {
            return None;
        }
        if generic_match {
            return evaluate_nfi_long_btc_adjustment(
                manager,
                route,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
    }
    let uses_regular_adjustment = rebuy_mode || any_in_scope(&tags, &dispatch.long_regular_scope);
    if !uses_regular_adjustment {
        // This is the source's valid no-op branch for compounds such as a
        // rebuy/grind tag plus an opposite-side tag. Do not accidentally
        // promote those trades into the regular long adjustment machine.
        return Some(None);
    }
    evaluate_nfi_system_v3_adjustment(
        manager,
        manager.position_adjustment.as_ref()?,
        TradeSide::Long,
        trade,
        request,
        initial_stake_multiplier,
        rebuy_mode,
    )
}

#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is a valid no-op.
fn evaluate_nfi_short_position_adjustment(
    manager: &NfiX7TradeManager,
    trade: &mut OpenTrade,
    request: &PositionAdjustmentRequest<'_>,
) -> Option<Option<AdjustmentSignal>> {
    let dispatch = manager.runtime_dispatch()?;
    let tags = dispatch.intern_trade_tags(trade);
    let rebuy_route = dispatch
        .short_rebuy_route
        .and_then(|index| manager.managed_short_routes.get(index))?;
    if nfi_managed_short_route_supports_tags(manager, rebuy_route, tags.words) {
        let delegates = if let Some(program) = manager.short_rebuy_adjustment.program.as_ref() {
            compiled_rebuy_delegates(program, trade)
        } else {
            trade
                .orders
                .iter()
                .find(|order| !order.is_entry)
                .and_then(|order| order.tag.as_deref())
                == Some("derisk_level_3")
        };
        if !delegates {
            return evaluate_nfi_short_rebuy_adjustment(
                &manager.short_rebuy_adjustment,
                trade,
                request.pair,
                request.candle_index,
                request.candle,
                request.config,
                request.available_balance,
            );
        }
        let adjustment = manager.short_position_adjustment.as_ref()?;
        let initial_stake_multiplier = adjustment.constants.rebuy_stake_multiplier?;
        return evaluate_nfi_system_v3_adjustment(
            manager,
            adjustment,
            TradeSide::Short,
            trade,
            request,
            initial_stake_multiplier,
            true,
        );
    }
    let Some(adjustment) = manager.short_position_adjustment.as_ref() else {
        // Older descriptors only compile short-rebuy. If its all-tags
        // predicate did not match, upstream has no reachable short adjustment
        // branch for the remaining compiled cross-side compound.
        return Some(None);
    };
    let uses_regular_adjustment = any_in_scope(&tags, &dispatch.short_regular_scope);
    if !uses_regular_adjustment {
        // A simultaneous long signal can append a long word to X7's shared
        // entry-tag column. Compounds containing only short-rebuy and long
        // words match neither source adjustment branch and are a valid no-op.
        return Some(None);
    }
    evaluate_nfi_system_v3_adjustment(
        manager,
        adjustment,
        TradeSide::Short,
        trade,
        request,
        1.0,
        false,
    )
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::option_option)] // Outer None rejects invalid state; inner None is callback no-op.
fn evaluate_nfi_long_btc_adjustment(
    manager: &NfiX7TradeManager,
    route: &NfiLongGrindRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    available_balance: f64,
) -> Option<Option<AdjustmentSignal>> {
    match evaluate_nfi_regular_adjustment(
        manager,
        route,
        trade,
        pair,
        candle_index,
        candle,
        config,
        available_balance,
    )? {
        RegularAdjustmentOutcome::Return(signal) => Some(signal),
        RegularAdjustmentOutcome::ContinueLegacy => evaluate_nfi_legacy_grind_adjustment(
            manager,
            route,
            trade,
            pair,
            candle_index,
            candle,
            config,
            available_balance,
        ),
    }
}

pub(crate) fn nfi_long_grind_supports_trade(route: &NfiLongGrindRoute, trade: &OpenTrade) -> bool {
    let words = trade.entry_tag_words();
    // X7 uses ``all(c in long_grind_mode_tags for c in enter_tags)`` for
    // this route. Requiring every word matters for mixed NFI tags: top-coins
    // intentionally uses a different, any-tag routing rule.
    !words.is_empty()
        && words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word))
}
