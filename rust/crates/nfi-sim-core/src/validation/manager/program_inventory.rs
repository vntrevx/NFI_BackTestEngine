//! Manager scalar-program inventory validation.

use std::collections::BTreeSet;

use crate::domain::NfiX7TradeManager;

use super::valid_scalar_program;

pub(super) fn is_valid(manager: &NfiX7TradeManager) -> bool {
    const LONG: [&str; 4] = [
        "long_exit_signals",
        "long_exit_main",
        "long_exit_williams_r",
        "long_exit_dec",
    ];
    const SHORT: [&str; 4] = [
        "short_exit_signals",
        "short_exit_main",
        "short_exit_williams_r",
        "short_exit_dec",
    ];
    let adjustment = manager.position_adjustment.as_ref();
    let short_adjustment = manager.short_position_adjustment.as_ref();
    let long_btc = manager.long_btc.as_ref();
    let mut required = LONG.into_iter().chain(SHORT).collect::<BTreeSet<_>>();
    for adjustment in [adjustment, short_adjustment].into_iter().flatten() {
        required.insert(adjustment.decision_program.as_str());
        if let Some(program) = &adjustment.program {
            for group in &program.order_scan.counted_entry_groups {
                if let Some(program) = group.entry_program.as_deref() {
                    required.insert(program);
                }
            }
        }
    }
    for route in [
        manager.long_grind.as_ref(),
        long_btc,
        manager.short_grind.as_ref(),
    ]
    .into_iter()
    .flatten()
    {
        required.insert(route.decision_program.as_str());
    }
    if let Some(route) = long_btc {
        let Some(program) = route.regular_decision_program.as_deref() else {
            return false;
        };
        required.insert(program);
    }
    manager.programs.len() == required.len()
        && required
            .into_iter()
            .all(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
}
