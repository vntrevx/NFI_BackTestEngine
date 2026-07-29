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
        let words = entry_tag.split_whitespace().collect::<Vec<_>>();
        if words.is_empty() {
            return false;
        }
        let all_words_are_compiled = words.iter().all(|tag| {
            nfi_long_tag_is_in_compiled_scope(manager, tag)
                || nfi_short_tag_is_in_compiled_scope(manager, tag)
        });
        let contains_entry_side = match side {
            TradeSide::Long => words
                .iter()
                .any(|tag| nfi_long_tag_is_in_compiled_scope(manager, tag)),
            TradeSide::Short => words
                .iter()
                .any(|tag| nfi_short_tag_is_in_compiled_scope(manager, tag)),
        };
        // X7 appends both sides to one tag column. Requiring one word for the
        // side being opened rejects malformed vectors, while the union check
        // permits only opposite-side words whose callback route was compiled
        // and reviewed from the same source snapshot.
        all_words_are_compiled && contains_entry_side
    })
}

fn nfi_long_tag_is_in_compiled_scope(manager: &NfiX7TradeManager, tag: &str) -> bool {
    manager
        .managed_long_routes
        .iter()
        .any(|route| route.entry_tags.iter().any(|supported| supported == tag))
        || manager
            .long_grind
            .as_ref()
            .is_some_and(|route| route.entry_tags.iter().any(|supported| supported == tag))
        || manager
            .long_btc
            .as_ref()
            .is_some_and(|route| route.entry_tags.iter().any(|supported| supported == tag))
}

fn nfi_short_tag_is_in_compiled_scope(manager: &NfiX7TradeManager, tag: &str) -> bool {
    manager
        .managed_short_routes
        .iter()
        .any(|route| route.entry_tags.iter().any(|supported| supported == tag))
}

pub(crate) fn nfi_managed_short_route_supports_tags(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[&str],
) -> bool {
    if words.is_empty() {
        return false;
    }
    let contains_primary = words
        .iter()
        .any(|word| route.entry_tags.iter().any(|supported| supported == word));
    if !contains_primary {
        return false;
    }
    match route.key.as_str() {
        // Upstream uses `any(...)` for these explicit custom-exit blocks.
        "short_normal" | "short_pump" | "short_quick" | "short_high_profit" | "short_rapid" => true,
        // Rebuy is the one strict all-tags route.
        "short_rebuy" => words
            .iter()
            .all(|word| route.entry_tags.iter().any(|supported| supported == word)),
        // Scalp accepts pure scalp or a scalp/rebuy compound. Tag 620 is not
        // executable yet and is rejected by the entry-scope gate before this
        // helper can run.
        "short_scalp" => {
            let rebuy = manager
                .managed_short_routes
                .iter()
                .find(|candidate| candidate.key == "short_rebuy");
            words.iter().all(|word| {
                route.entry_tags.iter().any(|supported| supported == word)
                    || rebuy.is_some_and(|rebuy| {
                        rebuy.entry_tags.iter().any(|supported| supported == word)
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
                        .all(|supported| supported != word)
            })
        }),
        _ => false,
    }
}

pub(crate) fn nfi_managed_route_supports_tags(
    manager: &NfiX7TradeManager,
    route: &NfiManagedLongRoute,
    words: &[&str],
) -> bool {
    let contains_primary = words
        .iter()
        .any(|word| route.entry_tags.iter().any(|tag| tag == word));
    if !contains_primary {
        return false;
    }
    match route.profile {
        NfiManagedLongProfile::Rebuy => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        NfiManagedLongProfile::Rapid => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Scalp)
                    .is_some_and(|scalp| scalp.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        NfiManagedLongProfile::Scalp => words.iter().all(|word| {
            route.entry_tags.iter().any(|tag| tag == word)
                || manager
                    .managed_long_routes
                    .iter()
                    .find(|candidate| candidate.profile == NfiManagedLongProfile::Rebuy)
                    .is_some_and(|rebuy| rebuy.entry_tags.iter().any(|tag| tag == word))
                || manager
                    .long_grind
                    .as_ref()
                    .is_some_and(|grind| grind.entry_tags.iter().any(|tag| tag == word))
        }),
        _ => true,
    }
}
