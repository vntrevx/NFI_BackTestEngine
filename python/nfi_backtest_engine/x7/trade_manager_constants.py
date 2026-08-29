"""Frozen assembly constants for the source-bound X7 trade manager."""

MANAGED_LONG_ADJUSTMENT_PROGRAM = "long_grind_entry_v3"
MANAGED_SHORT_ADJUSTMENT_PROGRAM = "short_grind_entry_v3"
LONG_REGULAR_ADJUSTMENT_PROGRAM = "long_grind_entry"

MANAGED_LONG_FROZEN_CONSTANTS = (
    "derisk_enable",
    "stops_enable",
    "stop_threshold_futures",
    "stop_threshold_spot",
    "system_name_use",
    "system_v3_2_name",
    "system_v3_2_stop_threshold_doom_futures",
    "system_v3_2_stop_threshold_doom_spot",
    "system_v3_2_stops_enable",
    "u_e_stops_enable",
)
REBUY_ADJUSTMENT_LIST_CONSTANTS = (
    "system_v3_rebuy_mode_stakes_futures",
    "system_v3_rebuy_mode_stakes_spot",
    "system_v3_rebuy_mode_thresholds_futures",
    "system_v3_rebuy_mode_thresholds_spot",
)
REBUY_ADJUSTMENT_NUMBER_CONSTANTS = (
    "system_v3_rebuy_mode_derisk_futures",
    "system_v3_rebuy_mode_derisk_spot",
)
ADJUSTMENT_BOOL_CONSTANTS = (
    "derisk_enable",
    "position_adjustment_enable",
    "system_v3_buyback_1_enable",
)
ADJUSTMENT_NUMBER_CONSTANTS = (
    "system_v3_max_stake",
    "system_v3_rebuy_mode_stake_multiplier",
)
ADJUSTMENT_GRIND_FIELDS = (
    "derisk_futures",
    "derisk_spot",
    "profit_threshold_futures",
    "profit_threshold_spot",
    "stakes_futures",
    "stakes_spot",
    "thresholds_futures",
    "thresholds_spot",
)

LONG_GRIND_ADJUSTMENT_SCOPE = "grind-backtest-v2"
LONG_BTC_ADJUSTMENT_SCOPE = "regular-backtest-v2"
LONG_GRIND_STATEFUL_METHODS = (
    "long_exit_grind",
    "long_grind_adjust_trade_position",
)
LONG_BTC_STATEFUL_METHODS = (
    "long_exit_btc",
    "long_grind_adjust_trade_position",
    "long_adjust_trade_position_no_derisk",
)
LONG_GRIND_IMPLEMENTED_STEPS = (
    "legacy first-entry recovery",
    "legacy order-history reconstruction",
    "legacy post-de-risk grind levels 1-2",
    "legacy grind levels 1-6",
    "legacy futures drawdown entry fallback",
    "legacy grind profit exits and stops",
    "legacy de-risk level-1 re-entry",
)
LONG_BTC_IMPLEMENTED_STEPS = (
    "tag-121 regular-mode order-history reconstruction",
    "tag-121 regular-mode rebuy",
    "tag-121 regular-mode grind levels 1-6",
    "tag-121 regular-mode grind profit exits and stops",
    "tag-121 regular-mode de-risk levels",
    "tag-121 post-de-risk legacy grind continuation",
)
BACKTEST_EXCLUSIONS = (
    {
        "code": "filled-order-partial-remainder",
        "runtime_scope": "live-only",
        "policy": "filled-orders-have-zero-remaining",
    },
)
