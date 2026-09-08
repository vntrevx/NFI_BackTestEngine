"""Static data settings from the pinned Freqtrade strategy resolver."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from .errors import StrategyAnalysisError

INHERITED_SETTINGS = {
    "minimal_roi": {},
    "exit_profit_offset": 0.0,
    "max_entry_position_adjustment": -1,
    "can_short": False,
    "order_types": {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "limit",
        "stoploss_on_exchange": False,
        "stoploss_on_exchange_interval": 60,
    },
    "order_time_in_force": {"entry": "GTC", "exit": "GTC"},
}
RESOLVER_SETTINGS = frozenset(
    {
        "minimal_roi",
        "timeframe",
        "stoploss",
        "trailing_stop",
        "trailing_stop_positive",
        "trailing_stop_positive_offset",
        "trailing_only_offset_is_reached",
        "use_custom_stoploss",
        "process_only_new_candles",
        "order_types",
        "order_time_in_force",
        "startup_candle_count",
        "use_exit_signal",
        "exit_profit_only",
        "ignore_roi_if_entry_signal",
        "exit_profit_offset",
        "position_adjustment_enable",
        "max_entry_position_adjustment",
    }
)
_BOOL_SETTINGS = frozenset(
    {
        "trailing_stop",
        "trailing_only_offset_is_reached",
        "use_custom_stoploss",
        "process_only_new_candles",
        "use_exit_signal",
        "exit_profit_only",
        "ignore_roi_if_entry_signal",
        "position_adjustment_enable",
        "can_short",
    }
)


def valid_resolver_value(name: str, value: Any) -> bool:
    if name in _BOOL_SETTINGS:
        return type(value) is bool
    if name == "max_entry_position_adjustment":
        return type(value) is int and value >= -1
    if name == "startup_candle_count":
        return type(value) is int and value >= 0
    if name == "timeframe":
        return isinstance(value, str) and bool(value)
    if name in {"order_types", "order_time_in_force"}:
        return isinstance(value, dict) and {"entry", "exit"} <= value.keys()
    if name == "minimal_roi":
        return isinstance(value, dict) and all(
            isinstance(key, str)
            and key.isascii()
            and key.isdigit()
            and finite_number(ratio)
            and ratio >= -1
            for key, ratio in value.items()
        )
    if name in {"trailing_stop_positive", "trailing_stop_positive_offset"}:
        return value is None or finite_number(value) and 0 <= value < 1
    if name == "stoploss":
        return finite_number(value) and -1 < value < 0
    return name == "exit_profit_offset" and finite_number(value)


def finite_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def resolver_overrides(constants: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    if "sell_profit_only" in config:
        raise StrategyAnalysisError(
            "pinned Freqtrade rejects deprecated sell_profit_only; use exit_profit_only"
        )
    updates = {}
    for key in RESOLVER_SETTINGS & config.keys():
        value = config[key]
        if not valid_resolver_value(key, value):
            raise StrategyAnalysisError(f"strategy resolver parameter {key!r} is invalid")
        if value != constants.get(key, INHERITED_SETTINGS.get(key)):
            updates[key] = copy.deepcopy(value)
    if (
        updates.get("exit_profit_only", constants.get("exit_profit_only")) is True
        and "exit_profit_offset" not in constants
        and "exit_profit_offset" not in updates
    ):
        updates["exit_profit_offset"] = 0.0
    return updates
