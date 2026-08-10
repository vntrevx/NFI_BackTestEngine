//! Versioned Freqtrade-compatible scheduling primitives.

/// Versioned scheduler descriptor consumed by Python contract checks.
#[must_use]
pub const fn contract_json() -> &'static str {
    include_str!("scheduler_contract.json")
}

/// Select the analyzed source row visible to callbacks for one execution row.
pub(crate) fn callback_feature_index(execution_index: usize) -> Option<usize> {
    execution_index.checked_sub(1)
}

/// Fill one same-timestamp order without allocating a new pair list.
///
/// Freqtrade visits pairs holding open trades first in trade insertion order,
/// then every remaining pair in configured order. A pair is emitted once.
pub(crate) fn fill_pair_processing_order<I>(
    open_pair_indices: I,
    pair_count: usize,
    processing_order: &mut Vec<usize>,
    open_pair_flags: &mut [bool],
) where
    I: IntoIterator<Item = usize>,
{
    processing_order.clear();
    for pair_index in open_pair_indices {
        if !open_pair_flags[pair_index] {
            open_pair_flags[pair_index] = true;
            processing_order.push(pair_index);
        }
    }
    for (pair_index, is_open) in open_pair_flags.iter().copied().enumerate().take(pair_count) {
        if !is_open {
            processing_order.push(pair_index);
        }
    }
    for pair_index in processing_order.iter().copied() {
        open_pair_flags[pair_index] = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_visibility_uses_the_previous_analyzed_row() {
        assert_eq!(callback_feature_index(0), None);
        assert_eq!(callback_feature_index(1), Some(0));
        assert_eq!(callback_feature_index(42), Some(41));
    }

    #[test]
    fn open_pairs_precede_remaining_configured_pairs_once() {
        let mut order = Vec::new();
        let mut flags = vec![false; 4];

        fill_pair_processing_order([2, 0, 2], 4, &mut order, &mut flags);

        assert_eq!(order, [2, 0, 1, 3]);
        assert_eq!(flags, [false; 4]);
    }

    #[test]
    fn embedded_scheduler_contract_is_valid_json() {
        let document: serde_json::Value =
            serde_json::from_str(contract_json()).expect("valid scheduler contract");

        assert_eq!(
            document["schema_version"],
            "freqtrade-scheduler-contract-v1"
        );
        assert_eq!(document["visibility"]["signal_source_row_shift"], 1);
        assert_eq!(
            document["chronology"]["wallet_mutation"],
            "serial-global-event-loop"
        );
    }
}
