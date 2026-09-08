"""Separate source-visible trade slots from finite simulator pair capacity."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import StrategyAnalysisError


def source_trade_slots(config: Mapping[str, Any]) -> int:
    if config.get("position_stacking", False) is not False:
        raise StrategyAnalysisError(
            "position_stacking requires multiple open positions per pair, which are not lowered"
        )
    value = config.get("max_open_trades")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StrategyAnalysisError("config.max_open_trades must be numeric")
    if not -1 <= value <= 4294967295 or not float(value).is_integer():
        raise StrategyAnalysisError(
            "fractional or out-of-range max_open_trades has no exact trade-surface contract"
        )
    return int(value)


def simulator_trade_slots(config: Mapping[str, Any], pair_count: int) -> int:
    slots = source_trade_slots(config)
    return pair_count if slots <= 0 else slots
