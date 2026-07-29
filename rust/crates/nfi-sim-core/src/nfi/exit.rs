//! NFI custom-exit routing and profit-target state machine.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::calculations::{fee_close, fee_open};
use crate::callbacks::{feature_number_at, insert_projected_feature_window, scalar_trade_value};
use crate::domain::{
    Candle, NfiManagedLongProfile, NfiManagedLongRoute, NfiX7TradeManager, PairSeries,
    PortfolioConfig,
};
use crate::portfolio::{OpenTrade, TradeSide};
use crate::scalar_vm::{evaluate_scalar_program_bundle_from_base, number_value};
use crate::validation::{nfi_managed_route_supports_tags, nfi_managed_short_route_supports_tags};

use super::dispatch::nfi_long_grind_supports_trade;
use super::state::{
    nfi_profit_bucket, nfi_profit_snapshot, nfi_trade_is_derisked, set_profit_target,
    NfiProfitSnapshot, ProfitTarget,
};

pub(crate) enum CustomExitDecision {
    NoExit,
    Exit(String),
}

pub(crate) const NFI_LONG_EXIT_PROGRAMS: &[&str] = &[
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
    "long_exit_dec",
];
pub(crate) const NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING: &[&str] = &[
    "long_exit_signals",
    "long_exit_main",
    "long_exit_williams_r",
];
pub(crate) const NFI_SHORT_EXIT_PROGRAMS: &[&str] = &[
    "short_exit_signals",
    "short_exit_main",
    "short_exit_williams_r",
    "short_exit_dec",
];

/// Route NFI custom exits in the exact order used by the strategy.
///
/// A route that does not exit may still update the pair-level target cache.
/// Therefore this loop must continue through later matching routes instead of
/// selecting one route up front. That distinction is observable for mixed NFI
/// entry tags and is why ``route_order`` is part of the sealed input.
#[allow(clippy::too_many_arguments)]
pub(crate) fn evaluate_nfi_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    for key in &manager.route_order {
        if let Some(route) = manager
            .managed_long_routes
            .iter()
            .find(|route| &route.key == key)
        {
            if !nfi_managed_route_supports_tags(manager, route, &words) {
                continue;
            }
            match evaluate_nfi_managed_long_exit(
                manager,
                route,
                nfi_profile_program_order(route.profile),
                trade,
                pair,
                candle_index,
                candle,
                config,
                profit_targets,
            )? {
                CustomExitDecision::Exit(reason) => {
                    return Some(CustomExitDecision::Exit(reason));
                }
                CustomExitDecision::NoExit => continue,
            }
        }

        let legacy = match key.as_str() {
            "long_grind" => manager.long_grind.as_ref(),
            "long_btc" => manager.long_btc.as_ref(),
            _ => None,
        };
        if let Some(route) = legacy.filter(|route| nfi_long_grind_supports_trade(route, trade)) {
            let snapshot = nfi_profit_snapshot(
                trade,
                candle.open,
                fee_open(config),
                fee_close(config),
                config.is_futures,
            )?;
            if snapshot.initial_stake_ratio > route.exit_profit_threshold {
                let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
                let reason = format!("exit_{}_g", route.mode_name);
                return Some(CustomExitDecision::Exit(nfi_exit_reason(
                    &reason, entry_tag,
                )));
            }
        }
    }
    // X7's custom_exit callback checks every long block before every short
    // block without filtering on trade.is_short. This is observable when its
    // shared enter_tag column contains labels from both sides.
    if let Some(decision) = evaluate_nfi_short_exit(
        manager,
        trade,
        pair,
        candle_index,
        candle,
        config,
        profit_targets,
    ) {
        if let CustomExitDecision::Exit(_) = decision {
            return Some(decision);
        }
    }
    // A compound of individually compiled words may intentionally match no
    // all-tags route. The source callback returns None in that case.
    Some(CustomExitDecision::NoExit)
}

/// Execute the bounded short-rebuy branch in source order.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_short_exit(
    manager: &NfiX7TradeManager,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let words = trade
        .entry_tag
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>();
    let mut matched = false;
    for key in &manager.short_route_order {
        let route = manager
            .managed_short_routes
            .iter()
            .find(|route| &route.key == key)?;
        if !nfi_managed_short_route_supports_tags(manager, route, &words) {
            continue;
        }
        matched = true;
        match evaluate_nfi_managed_long_exit(
            manager,
            route,
            NFI_SHORT_EXIT_PROGRAMS,
            trade,
            pair,
            candle_index,
            candle,
            config,
            profit_targets,
        )? {
            CustomExitDecision::Exit(reason) => {
                return Some(CustomExitDecision::Exit(reason));
            }
            CustomExitDecision::NoExit => {}
        }
    }
    matched.then_some(CustomExitDecision::NoExit)
}

/// Execute one source-bound NFI X7 managed custom-exit route.
///
/// Every profile follows the source callback's order: pure signal programs,
/// optional inline quick/rapid logic, profile stoploss, existing target,
/// target mutation, then the profile's ignored-signal filter. Target writes
/// happen even when `confirm_trade_exit` later rejects the candidate, exactly
/// as in Freqtrade.
#[allow(clippy::too_many_arguments)]
fn evaluate_nfi_managed_long_exit(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    config: &PortfolioConfig,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<CustomExitDecision> {
    let entry_tag = trade.entry_tag.as_deref().unwrap_or("empty");
    let enter_tags = entry_tag
        .split_whitespace()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    let snapshot = nfi_profit_snapshot(
        trade,
        candle.open,
        fee_open(config),
        fee_close(config),
        config.is_futures,
    )?;
    let (mut sell, mut signal_name) = nfi_managed_long_signals(
        manager,
        route,
        program_order,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        &enter_tags,
    )?;

    // X7 places rapid's inline RSI/MFI checks before its custom stop, while
    // quick places the same-shaped checks after `long_exit_stoploss()`. The
    // distinction matters when both predicates are true because the returned
    // reason changes.
    if !sell && route.profile == NfiManagedLongProfile::Rapid {
        (sell, signal_name) =
            nfi_inline_profile_exit(route, pair, candle_index, snapshot, trade.side)?;
    }
    if !sell {
        (sell, signal_name) = nfi_managed_long_stoploss(
            manager,
            route,
            trade,
            pair,
            candle_index,
            snapshot,
            config.is_futures,
        )?;
    }
    if !sell && route.profile == NfiManagedLongProfile::Quick {
        (sell, signal_name) =
            nfi_inline_profile_exit(route, pair, candle_index, snapshot, trade.side)?;
    }

    let previous_target = profit_targets.get(&trade.pair).cloned();
    if let NfiExistingTargetOutcome::Exit(reason) = evaluate_existing_nfi_target(
        route,
        trade,
        pair,
        candle_index,
        candle,
        snapshot,
        previous_target.as_ref(),
        profit_targets,
    )? {
        return Some(CustomExitDecision::Exit(nfi_exit_reason(
            &reason, entry_tag,
        )));
    }
    update_nfi_target_candidate(
        route,
        trade,
        candle,
        snapshot,
        sell,
        signal_name.as_deref(),
        previous_target.as_ref(),
        profit_targets,
    );

    if let Some(reason) = signal_name {
        if sell && !nfi_ignored_signal(route, &reason) {
            return Some(CustomExitDecision::Exit(nfi_exit_reason(
                &reason, entry_tag,
            )));
        }
    }
    if route.terminal_exit.as_ref().is_some_and(|terminal| {
        enter_tags == terminal.entry_tags
            && candle.timestamp_ms - trade.open_timestamp_ms >= terminal.minimum_age_ms
            && snapshot.initial_stake_ratio >= terminal.minimum_profit_ratio
    }) {
        let reason = &route
            .terminal_exit
            .as_ref()
            .expect("terminal exit was checked immediately above")
            .reason;
        return Some(CustomExitDecision::Exit(nfi_exit_reason(reason, entry_tag)));
    }
    Some(CustomExitDecision::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_signals(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    program_order: &[&str],
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    enter_tags: &[String],
) -> Option<(bool, Option<String>)> {
    if nfi_profile_requires_positive_profit(route.profile) && snapshot.initial_stake_ratio <= 0.0 {
        return Some((false, None));
    }
    let mut base_variables = BTreeMap::from([
        (
            "mode_name".to_owned(),
            Value::String(route.mode_name.clone()),
        ),
        (
            "current_profit".to_owned(),
            number_value(if route.profile == NfiManagedLongProfile::Rebuy {
                snapshot.current_stake_ratio
            } else {
                snapshot.initial_stake_ratio
            })?,
        ),
        ("max_profit".to_owned(), number_value(0.0)?),
        ("max_loss".to_owned(), number_value(0.0)?),
        ("trade".to_owned(), scalar_trade_value(trade)?),
        (
            "current_time".to_owned(),
            Value::Number(candle.timestamp_ms.into()),
        ),
        (
            "buy_tag".to_owned(),
            Value::Array(enter_tags.iter().cloned().map(Value::String).collect()),
        ),
    ]);
    // All methods in this source-ordered callback see the same analyzed
    // dataframe window. Materialize the union once, then give each scalar
    // program a fresh local overlay so temporary assignments cannot leak into
    // the next method.
    insert_projected_feature_window(
        &mut base_variables,
        pair,
        candle_index,
        manager.feature_projection_union(program_order)?,
    )?;
    let mut result = (false, None);
    for program_name in program_order {
        let value = evaluate_scalar_program_bundle_from_base(
            &manager.programs,
            program_name,
            &base_variables,
        )?;
        let fields = value.as_array()?;
        if fields.len() != 2 {
            return None;
        }
        result.0 = fields.first()?.as_bool()?;
        result.1 = match fields.get(1)? {
            Value::Null => None,
            Value::String(reason) => Some(reason.clone()),
            _ => return None,
        };
        if result.0 {
            break;
        }
    }
    Some(result)
}

fn nfi_profile_program_order(profile: NfiManagedLongProfile) -> &'static [&'static str] {
    match profile {
        NfiManagedLongProfile::HighProfit => NFI_LONG_EXIT_PROGRAMS_WITHOUT_DESCENDING,
        _ => NFI_LONG_EXIT_PROGRAMS,
    }
}

fn nfi_profile_requires_positive_profit(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Pump
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
    )
}

pub(crate) fn nfi_inline_profile_exit(
    route: &NfiManagedLongRoute,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    side: TradeSide,
) -> Option<(bool, Option<String>)> {
    let suffix_prefix = match route.profile {
        NfiManagedLongProfile::Quick
            if snapshot.initial_stake_ratio > 0.02 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "q"
        }
        NfiManagedLongProfile::Rapid
            if snapshot.initial_stake_ratio > 0.005 && snapshot.initial_stake_ratio <= 0.09 =>
        {
            "rpd"
        }
        _ => return Some((false, None)),
    };
    let rsi_14 = feature_number_at(pair, candle_index, "RSI_14")?;
    let mfi_14 = feature_number_at(pair, candle_index, "MFI_14")?;
    let willr_14 = feature_number_at(pair, candle_index, "WILLR_14")?;
    let rsi_3 = feature_number_at(pair, candle_index, "RSI_3")?;
    let rsi_3_15m = feature_number_at(pair, candle_index, "RSI_3_15m")?;
    let conditions = match side {
        TradeSide::Long => [
            rsi_14 > 78.0,
            mfi_14 > 84.0,
            willr_14 >= -0.1,
            rsi_14 >= 72.0 && rsi_3 > 90.0 && rsi_3_15m > 90.0,
            rsi_3_15m > 96.0,
            rsi_3 > 85.0 && rsi_3_15m > 85.0,
            rsi_3 > 90.0 && rsi_3_15m > 80.0,
            rsi_3 > 92.0 && rsi_3_15m > 75.0,
            rsi_3 > 94.0 && rsi_3_15m > 70.0,
            rsi_3 > 99.0,
        ],
        TradeSide::Short => {
            let fourth_rsi_limit = if route.profile == NfiManagedLongProfile::Quick {
                18.0
            } else {
                28.0
            };
            [
                rsi_14 < 22.0,
                mfi_14 < 16.0,
                willr_14 <= -99.9,
                rsi_14 <= fourth_rsi_limit && rsi_3 < 10.0 && rsi_3_15m < 10.0,
                rsi_3_15m < 4.0,
                rsi_3 < 15.0 && rsi_3_15m < 15.0,
                rsi_3 < 10.0 && rsi_3_15m < 20.0,
                rsi_3 < 8.0 && rsi_3_15m < 25.0,
                rsi_3 < 6.0 && rsi_3_15m < 30.0,
                rsi_3 < 1.0,
            ]
        }
    };
    let reason = conditions
        .iter()
        .position(|condition| *condition)
        .map(|index| format!("exit_{}_{}_{}", route.mode_name, suffix_prefix, index + 1));
    Some((reason.is_some(), reason))
}

#[allow(clippy::too_many_arguments)]
fn evaluate_existing_nfi_target(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) -> Option<NfiExistingTargetOutcome> {
    let Some(previous) = previous else {
        return Some(NfiExistingTargetOutcome::NoExit);
    };
    let decision =
        nfi_managed_long_profit_target_exit(route, trade, pair, candle_index, snapshot, previous)?;
    if decision.remove {
        profit_targets.remove(&trade.pair);
    }
    if let Some(reason) = decision.exit_reason {
        return Some(NfiExistingTargetOutcome::Exit(format!("{reason}_m")));
    }
    let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
    let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
    if previous.sell_reason == stoploss_u_e
        && snapshot.ratio > previous.profit + nfi_u_e_raise_delta(route.profile)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.ratio,
        );
    } else if snapshot.initial_stake_ratio > previous.profit + 0.001
        && previous.sell_reason != stoploss_doom
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            previous.sell_reason.clone(),
            snapshot.initial_stake_ratio,
        );
    }
    Some(NfiExistingTargetOutcome::NoExit)
}

#[allow(clippy::too_many_arguments)]
fn update_nfi_target_candidate(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    candle: &Candle,
    snapshot: NfiProfitSnapshot,
    sell: bool,
    reason: Option<&str>,
    previous: Option<&ProfitTarget>,
    profit_targets: &mut BTreeMap<String, ProfitTarget>,
) {
    if let (true, Some(reason)) = (sell, reason) {
        let stoploss_doom = format!("exit_{}_stoploss_doom", route.mode_name);
        let stoploss_u_e = format!("exit_{}_stoploss_u_e", route.mode_name);
        let blocked_u_e = format!("exit_profit_{}_stoploss_u_e", route.mode_name);
        let protected = reason == stoploss_doom || reason == stoploss_u_e;
        let blocked_previous = previous.is_some_and(|previous| {
            previous.sell_reason == stoploss_doom || previous.sell_reason == blocked_u_e
        });
        let target_profit = if protected {
            snapshot.ratio
        } else {
            snapshot.initial_stake_ratio
        };
        let should_mark = (protected
            && (!nfi_protected_target_has_reentry_guard(route.profile) || !blocked_previous))
            || (!protected
                && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio));
        if should_mark {
            set_profit_target(
                profit_targets,
                trade,
                candle,
                reason.to_owned(),
                target_profit,
            );
        }
    } else if snapshot.initial_stake_ratio >= nfi_max_target_floor(route.profile)
        && previous.is_none_or(|previous| previous.profit < snapshot.initial_stake_ratio)
    {
        set_profit_target(
            profit_targets,
            trade,
            candle,
            format!("exit_profit_{}_max", route.mode_name),
            snapshot.initial_stake_ratio,
        );
    }
}

fn nfi_ignored_signal(route: &NfiManagedLongRoute, reason: &str) -> bool {
    let maximum = format!("exit_profit_{}_max", route.mode_name);
    if reason == maximum {
        return true;
    }
    // X7 high-profit writes the stop target and still returns the stop in the
    // same callback. Every other managed-long mode suppresses that immediate
    // candidate and lets the target helper decide on a later candle.
    route.profile != NfiManagedLongProfile::HighProfit
        && [
            format!("exit_{}_stoploss_doom", route.mode_name),
            format!("exit_{}_stoploss_u_e", route.mode_name),
        ]
        .iter()
        .any(|ignored| ignored == reason)
}

fn nfi_u_e_raise_delta(profile: NfiManagedLongProfile) -> f64 {
    match profile {
        NfiManagedLongProfile::Normal
        | NfiManagedLongProfile::Pump
        | NfiManagedLongProfile::TopCoins
        | NfiManagedLongProfile::Scalp => 0.005,
        NfiManagedLongProfile::Quick
        | NfiManagedLongProfile::Rebuy
        | NfiManagedLongProfile::HighProfit
        | NfiManagedLongProfile::Rapid => 0.001,
    }
}

fn nfi_max_target_floor(profile: NfiManagedLongProfile) -> f64 {
    if profile == NfiManagedLongProfile::HighProfit {
        0.03
    } else {
        0.005
    }
}

fn nfi_protected_target_has_reentry_guard(profile: NfiManagedLongProfile) -> bool {
    matches!(
        profile,
        NfiManagedLongProfile::Normal
            | NfiManagedLongProfile::Quick
            | NfiManagedLongProfile::Rapid
            | NfiManagedLongProfile::TopCoins
    )
}

enum NfiExistingTargetOutcome {
    NoExit,
    Exit(String),
}

#[derive(Debug, Default)]
struct NfiTargetDecision {
    exit_reason: Option<String>,
    remove: bool,
}

#[derive(Debug, Clone, Copy)]
struct NfiTargetIndicators {
    rsi: f64,
    previous_rsi: f64,
    cmf: f64,
    cmf_1h: f64,
    cmf_4h: f64,
    roc_4h: f64,
}

/// Return the first ordinary trailing branch selected by the source helper.
///
/// Keeping the mirrored long/short predicates together makes direction
/// changes reviewable without obscuring the surrounding target lifecycle.
fn nfi_profit_target_trailing_suffix(
    side: TradeSide,
    initial_stake_ratio: f64,
    previous_profit: f64,
    indicators: NfiTargetIndicators,
) -> Option<usize> {
    let dropped_by = |delta| initial_stake_ratio < previous_profit - delta;
    let branches = match side {
        TradeSide::Long => [
            dropped_by(0.03)
                && indicators.rsi < 50.0
                && indicators.rsi < indicators.previous_rsi
                && indicators.cmf < -0.0,
            dropped_by(0.03)
                && indicators.cmf < -0.0
                && indicators.cmf_1h < -0.0
                && indicators.cmf_4h < -0.0,
            dropped_by(0.05) && indicators.roc_4h > 40.0,
        ],
        TradeSide::Short => [
            dropped_by(0.03)
                && indicators.rsi > 50.0
                && indicators.rsi > indicators.previous_rsi
                && indicators.cmf > 0.0,
            dropped_by(0.03)
                && indicators.cmf > 0.0
                && indicators.cmf_1h > 0.0
                && indicators.cmf_4h > 0.0,
            dropped_by(0.05) && indicators.roc_4h < -40.0,
        ],
    };
    branches
        .iter()
        .position(|selected| *selected)
        .map(|index| index + 1)
}

/// Evaluate the shared profit-target helper for either source side.
///
/// The scalp bucket thresholds are common, while ordinary trailing indicators
/// are mirrored inside upstream's `trade.is_short` branch.
#[allow(clippy::too_many_arguments)]
fn nfi_managed_long_profit_target_exit(
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    previous: &ProfitTarget,
) -> Option<NfiTargetDecision> {
    let mode = &route.mode_name;
    let doom = format!("exit_{mode}_stoploss_doom");
    let ordinary_stop = format!("exit_{mode}_stoploss");
    let u_e = format!("exit_{mode}_stoploss_u_e");
    if previous.sell_reason == doom || previous.sell_reason == ordinary_stop {
        // This adapter is structurally gated to `system_name_use ==
        // system_v3_2_name`; X7 returns the cached stop immediately for all
        // system-v3 variants.
        return Some(NfiTargetDecision {
            exit_reason: Some(previous.sell_reason.clone()),
            remove: false,
        });
    }
    if previous.sell_reason == u_e {
        if snapshot.initial_stake_ratio > 0.0 || nfi_trade_is_derisked(trade)? {
            return Some(NfiTargetDecision {
                exit_reason: None,
                remove: true,
            });
        }
        if snapshot.ratio < previous.profit - 0.04 / trade.leverage {
            return Some(NfiTargetDecision {
                exit_reason: Some(previous.sell_reason.clone()),
                remove: false,
            });
        }
        return Some(NfiTargetDecision::default());
    }
    if previous.sell_reason != format!("exit_profit_{mode}_max") {
        return Some(NfiTargetDecision::default());
    }
    if snapshot.initial_stake_ratio < -0.08 {
        return Some(NfiTargetDecision {
            exit_reason: None,
            remove: true,
        });
    }

    let previous_index = candle_index.checked_sub(1)?;
    let indicators = NfiTargetIndicators {
        rsi: feature_number_at(pair, candle_index, "RSI_14")?,
        previous_rsi: feature_number_at(pair, previous_index, "RSI_14")?,
        cmf: feature_number_at(pair, candle_index, "CMF_20")?,
        cmf_1h: feature_number_at(pair, candle_index, "CMF_20_1h")?,
        cmf_4h: feature_number_at(pair, candle_index, "CMF_20_4h")?,
        roc_4h: feature_number_at(pair, candle_index, "ROC_9_4h")?,
    };
    let Some(bucket) = nfi_profit_bucket(snapshot.initial_stake_ratio) else {
        return Some(NfiTargetDecision::default());
    };
    let pure_scalp_tags = route.profile == NfiManagedLongProfile::Scalp
        && trade.entry_tag.as_deref().is_some_and(|entry_tag| {
            let words = entry_tag.split_whitespace().collect::<Vec<_>>();
            !words.is_empty()
                && words
                    .iter()
                    .all(|word| route.entry_tags.iter().any(|tag| tag == word))
        });
    if pure_scalp_tags {
        let trailing_delta = match bucket {
            0 => 0.008,
            1 | 2 => 0.01,
            3..=6 => 0.015,
            7..=9 => 0.02,
            10..=12 => 0.025,
            _ => return None,
        };
        return Some(NfiTargetDecision {
            exit_reason: (snapshot.initial_stake_ratio < previous.profit - trailing_delta)
                .then(|| format!("exit_profit_{mode}_t_{bucket}_1")),
            remove: false,
        });
    }
    let suffix = nfi_profit_target_trailing_suffix(
        trade.side,
        snapshot.initial_stake_ratio,
        previous.profit,
        indicators,
    );
    Some(NfiTargetDecision {
        exit_reason: suffix.map(|suffix| format!("exit_profit_{mode}_t_{bucket}_{suffix}")),
        remove: false,
    })
}

fn nfi_managed_long_stoploss(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    trade: &OpenTrade,
    pair: &PairSeries,
    candle_index: usize,
    snapshot: NfiProfitSnapshot,
    is_futures: bool,
) -> Option<(bool, Option<String>)> {
    let constants = &manager.constants;
    let first_entry = trade.orders.iter().find(|order| order.is_entry)?;
    let entry_cost = first_entry.amount * first_entry.price;
    let system_version = trade.custom_data.get("system_version")?.as_str()?;
    if system_version != constants.system_name_use {
        return None;
    }

    if matches!(
        route.profile,
        NfiManagedLongProfile::Rebuy | NfiManagedLongProfile::Rapid | NfiManagedLongProfile::Scalp
    ) {
        if !constants.system_v3_2_stops_enable {
            return Some((false, None));
        }
        let threshold = if is_futures {
            route.stop_threshold_futures?
        } else {
            route.stop_threshold_spot?
        };
        let stopped = snapshot.stake < -(entry_cost * threshold / trade.leverage);
        return Some((
            stopped,
            stopped.then(|| format!("exit_{}_stoploss_doom", route.mode_name)),
        ));
    }

    if !constants.stops_enable {
        return Some((false, None));
    }
    if constants.system_v3_2_stops_enable {
        let threshold = if is_futures {
            constants.system_v3_2_stop_threshold_doom_futures
        } else {
            constants.system_v3_2_stop_threshold_doom_spot
        };
        if snapshot.stake < -(entry_cost * threshold / trade.leverage) {
            return Some((
                true,
                Some(format!("exit_{}_stoploss_doom", route.mode_name)),
            ));
        }
    }
    if !constants.u_e_stops_enable {
        return Some((false, None));
    }
    let previous_index = candle_index.checked_sub(1)?;
    let close = feature_number_at(pair, candle_index, "close")?;
    let ema_200 = feature_number_at(pair, candle_index, "EMA_200")?;
    let rsi = feature_number_at(pair, candle_index, "RSI_14")?;
    let cmf = feature_number_at(pair, candle_index, "CMF_20")?;
    let rsi_1h = feature_number_at(pair, candle_index, "RSI_14_1h")?;
    let previous_rsi = feature_number_at(pair, previous_index, "RSI_14")?;
    let threshold = if is_futures {
        constants.stop_threshold_futures
    } else {
        constants.stop_threshold_spot
    };
    let directional_guard = match trade.side {
        TradeSide::Long => {
            close < ema_200
                && cmf < -0.0
                && (ema_200 - close) / close < 0.010
                && rsi > previous_rsi
                && rsi > rsi_1h + 24.0
        }
        TradeSide::Short => {
            close > ema_200
                && cmf > 0.0
                && (close - ema_200) / ema_200 < 0.010
                && rsi < previous_rsi
                && rsi < rsi_1h - 24.0
        }
    };
    let should_stop = snapshot.stake < -(entry_cost * threshold) && directional_guard;
    Some((
        should_stop,
        should_stop.then(|| format!("exit_{}_stoploss_u_e", route.mode_name)),
    ))
}

fn nfi_exit_reason(reason: &str, entry_tag: &str) -> String {
    format!("{reason} ( {entry_tag})")
}
