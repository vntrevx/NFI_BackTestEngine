"""Pinned Freqtrade callback execution semantics used by source-bound IR."""

from __future__ import annotations

from .callback_contract import JsonObject

CALLBACK_EXECUTION_IR_VERSION = "callback-execution-ir-v1"
FREQTRADE_CALLBACK_CONTRACT_VERSION = "2026.5.1"
FREQTRADE_CALLBACK_CONTRACT_FINGERPRINT = (
    "7c26cbaea6853a20b93932dbc0f3bc788cf0d43e58f243e9985029a727d6ec7f"
)

# This is a contract vocabulary, not executable lowering. A callback absent from
# this reviewed table cannot acquire Native behavior through descriptive IR.
_CALLBACK_POLICIES: dict[str, JsonObject] = {
    "bot_loop_start": {
        "predicate": "once-per-main-candle-before-pair-and-detail-iteration",
        "accepted_returns": ["null"],
        "cadence": "once-per-candle-in-backtest-or-hyperopt",
        "exception_fallback": "continue",
    },
    "check_entry_timeout": {
        "predicate": "open-entry-order-before-initial-entry-processing",
        "accepted_returns": ["timeout", "keep-open"],
        "cadence": "per-open-entry-order-candle",
        "exception_fallback": "keep-open",
    },
    "check_exit_timeout": {
        "predicate": "open-exit-order-before-new-exit-candidate-processing",
        "accepted_returns": ["timeout", "keep-open"],
        "cadence": "per-open-exit-order-candle",
        "exception_fallback": "keep-open",
    },
    "custom_stake_amount": {
        "predicate": "initial-entry-after-leverage-and-before-stake-validation",
        "accepted_returns": ["stake-amount"],
        "cadence": "per-entry-attempt",
        "exception_fallback": "proposed-stake",
    },
    "leverage": {
        "predicate": "initial-futures-entry-before-custom-stake-amount",
        "accepted_returns": ["leverage"],
        "cadence": "per-futures-entry-attempt",
        "exception_fallback": "one",
    },
    "confirm_trade_entry": {
        "predicate": "entry-amount-passed-precision-and-exchange-limits",
        "accepted_returns": ["accept", "reject"],
        "cadence": "per-order-entry-attempt",
        "exception_fallback": "accept",
    },
    "order_filled": {
        "predicate": "order-transitioned-to-filled",
        "accepted_returns": ["null"],
        "cadence": "per-filled-order-after-trade-order-replay",
        "exception_fallback": "continue",
    },
    "custom_exit": {
        "predicate": "use-exit-signal-without-unopposed-explicit-exit-signal",
        "accepted_returns": ["exit-reason", "true", "null"],
        "cadence": "per-open-trade-candle",
        "exception_fallback": "reject",
    },
    "custom_stoploss": {
        "predicate": "stoploss-evaluation-with-custom-stoploss-enabled-and-direction-permitted",
        "accepted_returns": ["ratio", "null"],
        "cadence": "per-open-trade-candle-and-after-fill",
        "exception_fallback": "null",
    },
    "confirm_trade_exit": {
        "predicate": "strategy-exit-candidate-before-fill-except-liquidation",
        "accepted_returns": ["accept", "reject"],
        "cadence": "per-confirmable-exit-candidate",
        "exception_fallback": "accept",
    },
    "adjust_trade_position": {
        "predicate": "open-trade-before-stoploss-and-custom-exit-when-position-adjustment-enabled",
        "accepted_returns": ["stake-delta", "stake-delta-with-tag", "null"],
        "cadence": "per-open-trade-candle-when-position-adjustment-enabled",
        "exception_fallback": "null-with-empty-tag",
    },
}

_EXECUTION_ORDER = (
    "bot_loop_start",
    "check_entry_timeout",
    "check_exit_timeout",
    "leverage",
    "custom_stake_amount",
    "confirm_trade_entry",
    "order_filled",
    "adjust_trade_position",
    "custom_stoploss",
    "custom_exit",
    "confirm_trade_exit",
)


def _callback_policy(
    name: str,
    *,
    present: set[str],
    trading_mode: str,
    run_mode: str,
) -> JsonObject | None:
    policy = _CALLBACK_POLICIES.get(name)
    if policy is None:
        return None
    index = _EXECUTION_ORDER.index(name)
    before = next((item for item in _EXECUTION_ORDER[index + 1 :] if item in present), None)
    after = next((item for item in reversed(_EXECUTION_ORDER[:index]) if item in present), None)
    return {
        "invocation_predicate": policy["predicate"],
        "accepted_returns": policy["accepted_returns"],
        "order": {
            "phase": index,
            "after": [after] if after is not None else [],
            "before": [before] if before is not None else [],
        },
        "visibility": {
            "signal_row_offset": -1,
            "callback_dataframe_completed_candle_lag": 2,
            "bot_loop_start_first_visible_rows": 0,
            "startup_executable": False,
            "successful_custom_state_visible": "next-callback-in-scheduler-order",
        },
        "cadence": {
            "kind": policy["cadence"],
            "run_mode": run_mode,
            "trading_mode": trading_mode,
            "active": not (name == "leverage" and trading_mode == "spot"),
        },
        "exception": {
            "fallback": policy["exception_fallback"],
            "ordinary_trade_deltas": "rollback-deepcopy",
            "custom_state_deltas": "persist-shared-storage",
            "scheduler_deltas_before_callback": "preserve",
        },
    }
