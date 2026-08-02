//! Fail-closed NFI tag and side routing checks.

use crate::domain::{
    Candle, EntrySignal, NfiManagedLongProfile, NfiManagedLongRoute, NfiX7TradeManager, PairSeries,
    SimError,
};
use crate::portfolio::TradeSide;

use super::pair::freqtrade_entry_signal;

pub(crate) fn unsupported_nfi_pair_signal(
    pair: &PairSeries,
    candle: &Candle,
    manager: &NfiX7TradeManager,
    can_short: bool,
) -> Option<SimError> {
    let (side, signal) = freqtrade_entry_signal(candle, can_short)?;
    if side != TradeSide::Short {
        return None;
    }
    (!nfi_entry_signal_is_supported(manager, TradeSide::Short, signal)).then(|| {
        SimError::UnsupportedNfiEntryTag {
            pair: pair.pair.clone(),
            entry_tag: signal.tag.clone().unwrap_or_else(|| "<short>".to_owned()),
        }
    })
}

pub(crate) fn nfi_entry_signal_is_supported(
    manager: &NfiX7TradeManager,
    side: TradeSide,
    signal: &EntrySignal,
) -> bool {
    signal.tag.as_deref().is_some_and(|entry_tag| {
        let Some(dispatch) = manager.runtime_dispatch() else {
            return false;
        };
        let tag_ids = dispatch.intern_tag_ids(entry_tag);
        if tag_ids.is_empty() {
            return false;
        }
        let all_words_are_compiled = tag_ids.iter().all(|tag| {
            tag.is_some_and(|tag| {
                dispatch.long_scope.contains(&tag) || dispatch.short_scope.contains(&tag)
            })
        });
        let contains_entry_side = match side {
            TradeSide::Long => tag_ids
                .iter()
                .flatten()
                .any(|tag| dispatch.long_scope.contains(tag)),
            TradeSide::Short => tag_ids
                .iter()
                .flatten()
                .any(|tag| dispatch.short_scope.contains(tag)),
        };
        // X7 appends both sides to one tag column. Requiring one word for the
        // side being opened rejects malformed vectors, while the union check
        // permits only opposite-side words whose callback route was compiled
        // and reviewed from the same source snapshot.
        all_words_are_compiled && contains_entry_side
    })
}

pub(crate) fn nfi_managed_short_route_supports_tags<T: AsRef<str>>(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[T],
) -> bool {
    if words.is_empty() {
        return false;
    }
    let contains_primary = words.iter().any(|word| {
        route
            .entry_tags
            .iter()
            .any(|supported| supported == word.as_ref())
    });
    if !contains_primary {
        return false;
    }
    match route.key.as_str() {
        // Upstream uses `any(...)` for these explicit custom-exit blocks.
        "short_normal" | "short_pump" | "short_quick" | "short_high_profit" | "short_rapid" => true,
        // Rebuy is the one strict all-tags route.
        "short_rebuy" => words.iter().all(|word| {
            route
                .entry_tags
                .iter()
                .any(|supported| supported == word.as_ref())
        }),
        // Scalp accepts pure scalp or a scalp/rebuy compound. Tag 620 is not
        // executable yet and is rejected by the entry-scope gate before this
        // helper can run.
        "short_scalp" => {
            let rebuy = manager
                .managed_short_routes
                .iter()
                .find(|candidate| candidate.key == "short_rebuy");
            words.iter().all(|word| {
                route
                    .entry_tags
                    .iter()
                    .any(|supported| supported == word.as_ref())
                    || rebuy.is_some_and(|rebuy| {
                        rebuy
                            .entry_tags
                            .iter()
                            .any(|supported| supported == word.as_ref())
                    })
            })
        }
        // Upstream omits top-coins from `short_exit_known_mode_tags`, so these
        // labels reach the final short-normal fallback only when no explicit
        // short-exit family word is present.
        "short_top_coins_fallback" => words.iter().all(|word| {
            manager.managed_short_routes.iter().all(|candidate| {
                candidate.key == "short_top_coins_fallback"
                    || candidate
                        .entry_tags
                        .iter()
                        .all(|supported| supported != word.as_ref())
            })
        }),
        _ => false,
    }
}

pub(crate) fn nfi_managed_route_supports_tags<T: AsRef<str>>(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[T],
) -> bool {
    let contains_primary = words
        .iter()
        .any(|word| route.entry_tags.iter().any(|tag| tag == word.as_ref()));
    if !contains_primary {
        return false;
    }
    match route.profile {
        NfiManagedLongProfile::Rebuy => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word.as_ref())
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word.as_ref()))
        }),
        NfiManagedLongProfile::Rapid => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word.as_ref())
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word.as_ref()))
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Scalp)
                    .is_some_and(|scalp| scalp.entry_tags.iter().any(|tag| tag == word.as_ref()))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word.as_ref()))
        }),
        NfiManagedLongProfile::Scalp => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word.as_ref())
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word.as_ref()))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word.as_ref()))
        }),
        _ => true,
    }
}
