"""Bind exchange order-stake limits to the frozen markets used for execution."""

from __future__ import annotations

import math
import sys
from typing import Any

from .errors import StrategyAnalysisError


def _bound(value: Any, size: float, name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= sys.float_info.max
    ):
        raise StrategyAnalysisError(f"{name} must be a finite nonnegative bound or null")
    result = float(value) * size
    if not math.isfinite(result):
        raise StrategyAnalysisError(f"{name} overflows contract-size conversion")
    return result


def order_stake_policy(config: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Preserve explicit unbounded limits and Freqtrade's contract-size conversion."""
    exchange = config.get("exchange")
    markets = snapshot.get("markets")
    if not isinstance(exchange, dict) or not isinstance(markets, dict):
        raise StrategyAnalysisError("exchange stake bounds require exchange and markets objects")
    pairs = exchange.get("pair_whitelist")
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
    ):
        raise StrategyAnalysisError("exchange stake bounds require a configured pair whitelist")
    limits = {}
    for pair in pairs:
        market = markets.get(pair)
        if not isinstance(market, dict):
            raise StrategyAnalysisError(f"{pair} frozen market is missing")
        size = market.get("contractSize", 1.0) if config.get("trading_mode") == "futures" else 1.0
        if (
            isinstance(size, bool)
            or not isinstance(size, int | float)
            or not 0 < size <= sys.float_info.max
        ):
            raise StrategyAnalysisError(f"{pair} requires a positive finite contract size")
        raw_limits = market.get("limits")
        if not isinstance(raw_limits, dict):
            raise StrategyAnalysisError(f"{pair} frozen market lacks amount/cost limits")
        record = {}
        for kind in ("amount", "cost"):
            raw = raw_limits.get(kind, {})
            if not isinstance(raw, dict) or "max" not in raw:
                raise StrategyAnalysisError(f"{pair} frozen market lacks its maximum {kind} limit")
            record[f"maximum_{kind}"] = _bound(raw["max"], float(size), f"{pair} maximum {kind}")
        limits[pair] = record
    return {"pair_limits": limits}
