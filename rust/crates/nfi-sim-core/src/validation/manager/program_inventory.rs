//! Manager scalar-program inventory validation.

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
    manager.programs.len()
        == LONG.len()
            + SHORT.len()
            + usize::from(adjustment.is_some())
            + usize::from(short_adjustment.is_some())
            + usize::from(long_btc.is_some())
        && LONG.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && SHORT.iter().all(|name| {
            manager
                .programs
                .get(*name)
                .is_some_and(valid_scalar_program)
        })
        && long_btc.is_none_or(|route| {
            route
                .regular_decision_program
                .as_ref()
                .is_some_and(|name| manager.programs.get(name).is_some_and(valid_scalar_program))
        })
        && adjustment.is_none_or(|route| {
            manager
                .programs
                .get(&route.decision_program)
                .is_some_and(valid_scalar_program)
        })
        && short_adjustment.is_none_or(|route| {
            manager
                .programs
                .get(&route.decision_program)
                .is_some_and(valid_scalar_program)
        })
}
