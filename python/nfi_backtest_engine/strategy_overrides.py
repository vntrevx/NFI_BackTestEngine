"""Freqtrade strategy settings whose config values take precedence."""

from __future__ import annotations

import math
from typing import Any

from .errors import StrategyAnalysisError


def effective_stoploss_ratio(
    constants: dict[str, Any],
    config: dict[str, Any],
) -> float:
    """Return Freqtrade's effective static stoploss ratio.

    Freqtrade loads the strategy first and then applies supported config
    overrides. Reading only the source literal would make the native engine
    disagree whenever a backtest config intentionally tightens the stoploss.
    """
    value = config["stoploss"] if "stoploss" in config else constants.get("stoploss")
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not -1.0 < float(value) < 0.0
    ):
        raise StrategyAnalysisError(
            "effective strategy stoploss must be a finite ratio between -1 and 0"
        )
    return float(value)
