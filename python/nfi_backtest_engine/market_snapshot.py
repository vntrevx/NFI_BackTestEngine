"""Minimal immutable CCXT market snapshot for exact simulator sizing."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ccxt

from .canonical import write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file

MARKET_SNAPSHOT_VERSION = "1.1.0"


def capture_market_snapshot(
    config: dict[str, Any],
    pairs: list[str],
    destination: str | Path,
) -> dict[str, Any]:
    exchange_config = config.get("exchange")
    if not isinstance(exchange_config, dict):
        raise SpecValidationError("market capture requires config.exchange")
    exchange_name = exchange_config.get("name")
    if not isinstance(exchange_name, str) or not exchange_name:
        raise SpecValidationError("market capture requires config.exchange.name")
    constructor = getattr(ccxt, exchange_name, None)
    if not isinstance(constructor, type):
        raise BenchmarkError(f"CCXT does not provide exchange: {exchange_name}")
    ccxt_config = exchange_config.get("ccxt_config", {})
    if not isinstance(ccxt_config, dict):
        raise SpecValidationError("config.exchange.ccxt_config must be an object")
    client = constructor(
        {
            **ccxt_config,
            "enableRateLimit": True,
            "apiKey": "",
            "secret": "",
            "password": "",
            "uid": "",
        }
    )
    try:
        loaded = client.load_markets()
    except Exception as exc:
        raise BenchmarkError(f"CCXT market capture failed for {exchange_name}: {exc}") from exc
    records: dict[str, Any] = {}
    for pair in pairs:
        market = loaded.get(pair)
        if not isinstance(market, dict):
            raise BenchmarkError(f"CCXT market snapshot is missing pair: {pair}")
        precision = market.get("precision")
        if not isinstance(precision, dict):
            raise BenchmarkError(f"CCXT market precision is missing for pair: {pair}")
        amount_step = _precision_step(
            precision.get("amount"),
            precision_mode=client.precisionMode,
            field=f"{pair}.amount",
        )
        price_step = _precision_step(
            precision.get("price"),
            precision_mode=client.precisionMode,
            field=f"{pair}.price",
        )
        taker = market.get("taker")
        if not isinstance(taker, int | float) or isinstance(taker, bool):
            trading_fees = getattr(client, "fees", {}).get("trading", {})
            taker = trading_fees.get("taker")
        if not isinstance(taker, int | float) or isinstance(taker, bool):
            raise BenchmarkError(f"CCXT taker fee is missing for pair: {pair}")
        taker_value = float(taker)
        if not math.isfinite(taker_value) or taker_value < 0.0:
            raise BenchmarkError(f"CCXT taker fee is invalid for pair: {pair}")
        limits = market.get("limits", {})
        if not isinstance(limits, dict):
            raise BenchmarkError(f"CCXT market limits are invalid for pair: {pair}")
        amount_limits = limits.get("amount", {})
        cost_limits = limits.get("cost", {})
        if not isinstance(amount_limits, dict) or not isinstance(cost_limits, dict):
            raise BenchmarkError(f"CCXT amount/cost limits are invalid for pair: {pair}")
        records[pair] = {
            "symbol": market.get("symbol", pair),
            "base": market.get("base"),
            "quote": market.get("quote"),
            "settle": market.get("settle"),
            "active": market.get("active"),
            "spot": market.get("spot"),
            "margin": market.get("margin"),
            "swap": market.get("swap"),
            "future": market.get("future"),
            "contract": market.get("contract"),
            "linear": market.get("linear"),
            "inverse": market.get("inverse"),
            "contractSize": market.get("contractSize"),
            "precision": {
                "amount": amount_step,
                "price": price_step,
            },
            "limits": {
                "amount": {
                    "min": _optional_non_negative_limit(
                        amount_limits.get("min"),
                        field=f"{pair}.limits.amount.min",
                    ),
                    "max": _optional_non_negative_limit(
                        amount_limits.get("max"),
                        field=f"{pair}.limits.amount.max",
                    ),
                },
                "cost": {
                    "min": _optional_non_negative_limit(
                        cost_limits.get("min"),
                        field=f"{pair}.limits.cost.min",
                    ),
                    "max": _optional_non_negative_limit(
                        cost_limits.get("max"),
                        field=f"{pair}.limits.cost.max",
                    ),
                },
            },
            "taker": taker_value,
        }
    document = {
        "schema_version": MARKET_SNAPSHOT_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ccxt_version": ccxt.__version__,
        "exchange": exchange_name,
        "precision_mode": client.precisionMode,
        "pairs": pairs,
        "markets": records,
    }
    write_json(destination, document)
    return {
        **document,
        "path": str(Path(destination).resolve()),
        "sha256": sha256_file(destination),
    }


def _precision_step(value: Any, *, precision_mode: int, field: str) -> float:
    if precision_mode == ccxt.TICK_SIZE:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise BenchmarkError(f"CCXT tick-size precision is invalid: {field}")
        result = float(value)
    elif precision_mode == ccxt.DECIMAL_PLACES:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BenchmarkError(f"CCXT decimal precision is invalid: {field}")
        result = 10.0**-value
    else:
        raise BenchmarkError(
            f"CCXT precision mode {precision_mode} cannot be represented as one exact step"
        )
    if not math.isfinite(result) or not result > 0.0:
        raise BenchmarkError(f"CCXT precision step must be positive: {field}")
    return result


def _optional_non_negative_limit(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BenchmarkError(f"CCXT market limit is invalid: {field}")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise BenchmarkError(f"CCXT market limit is invalid: {field}")
    return result
