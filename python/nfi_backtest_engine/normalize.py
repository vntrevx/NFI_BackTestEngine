"""Normalize official Freqtrade JSON exports into versioned trade surfaces."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_decimal, canonical_timestamp_ms, read_json, write_json
from .errors import NormalizationError
from .specs import validate_trade_surface


def normalize_freqtrade_result(
    document: Mapping[str, Any],
    *,
    strategy: str | None = None,
    surface_version: str = "1",
) -> dict[str, Any]:
    """Normalize one strategy from an official Freqtrade backtest export."""
    if surface_version not in {"1", "2"}:
        raise NormalizationError(f"unsupported trade surface version {surface_version!r}")
    strategy_name, result = _select_strategy_result(document, strategy)
    raw_trades = result.get("trades")
    if not isinstance(raw_trades, list):
        raise NormalizationError("$.trades: expected a list")

    if surface_version == "1":
        surface = {
            "schema_version": "1.0.0",
            "trades": [_normalize_trade(trade, index) for index, trade in enumerate(raw_trades)],
        }
    else:
        raw_locks = result.get("locks", [])
        if not isinstance(raw_locks, list):
            raise NormalizationError("$.locks: expected a list")
        surface = {
            "schema_version": "2.0.0",
            "strategy": strategy_name,
            "context": {
                "trading_mode": _enum_string(result, ("trading_mode",), "$", {"spot", "futures"}),
                "margin_mode": _nullable_string(result, ("margin_mode",), "$", optional=True),
                "timeframe": _required_string(result, ("timeframe",), "$"),
                "timeframe_detail": _nullable_string(
                    result, ("timeframe_detail",), "$", optional=True
                ),
                "timerange": _required_string(result, ("timerange",), "$"),
            },
            "summary": {
                "total_trades": _integer(result, ("total_trades",), "$", minimum=0),
                "starting_balance": _decimal(result, ("starting_balance",), "$"),
                "final_balance": _decimal(result, ("final_balance",), "$"),
                "profit_total_abs": _decimal(result, ("profit_total_abs",), "$"),
                "total_volume": _decimal(result, ("total_volume",), "$"),
                "rejected_signals": _integer(result, ("rejected_signals",), "$", minimum=0),
                "timedout_entry_orders": _integer(
                    result, ("timedout_entry_orders",), "$", minimum=0
                ),
                "timedout_exit_orders": _integer(result, ("timedout_exit_orders",), "$", minimum=0),
                "canceled_trade_entries": _integer(
                    result, ("canceled_trade_entries",), "$", minimum=0
                ),
                "canceled_entry_orders": _integer(
                    result, ("canceled_entry_orders",), "$", minimum=0
                ),
                "replaced_entry_orders": _integer(
                    result, ("replaced_entry_orders",), "$", minimum=0
                ),
                "max_open_trades": _integer(result, ("max_open_trades",), "$", minimum=0),
            },
            "locks": [_normalize_lock(lock, index) for index, lock in enumerate(raw_locks)],
            "trades": [_normalize_trade_v2(trade, index) for index, trade in enumerate(raw_trades)],
        }
    validate_trade_surface(surface)
    return surface


def normalize_file(
    source: str | Path,
    destination: str | Path,
    *,
    strategy: str | None = None,
    surface_version: str = "1",
) -> dict[str, Any]:
    raw = read_freqtrade_export(source)
    if not isinstance(raw, Mapping):
        raise NormalizationError("$: expected a JSON object")
    surface = normalize_freqtrade_result(raw, strategy=strategy, surface_version=surface_version)
    write_json(destination, surface)
    return surface


def read_freqtrade_export(source: str | Path) -> Any:
    """Read plain JSON or locate the one result JSON inside a Freqtrade ZIP."""
    source_path = Path(source)
    if source_path.suffix.lower() != ".zip":
        return read_json(source_path, decimals=True)

    try:
        with zipfile.ZipFile(source_path) as archive:
            candidates: list[tuple[str, Any]] = []
            for member in archive.namelist():
                if not member.lower().endswith(".json"):
                    continue
                try:
                    document = json.loads(archive.read(member).decode("utf-8"), parse_float=Decimal)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if _looks_like_freqtrade_result(document):
                    candidates.append((member, document))
    except (OSError, zipfile.BadZipFile) as exc:
        raise NormalizationError(f"{source_path}: invalid Freqtrade ZIP export") from exc

    if len(candidates) != 1:
        names = ", ".join(name for name, _ in candidates) or "none"
        raise NormalizationError(
            f"{source_path}: expected exactly one backtest result JSON in ZIP; found {names}"
        )
    return candidates[0][1]


def _looks_like_freqtrade_result(document: Any) -> bool:
    if not isinstance(document, Mapping):
        return False
    if isinstance(document.get("trades"), list):
        return True
    strategies = document.get("strategy")
    return isinstance(strategies, Mapping) and any(
        isinstance(result, Mapping) and isinstance(result.get("trades"), list)
        for result in strategies.values()
    )


def _select_strategy_result(
    document: Mapping[str, Any], strategy: str | None
) -> tuple[str, Mapping[str, Any]]:
    strategies = document.get("strategy")
    if isinstance(strategies, Mapping):
        if strategy is None:
            names = list(strategies)
            if len(names) != 1:
                choices = ", ".join(sorted(str(name) for name in names))
                raise NormalizationError(
                    f"$.strategy: select one strategy explicitly; available: {choices}"
                )
            strategy = str(names[0])
        if strategy not in strategies:
            raise NormalizationError(f"$.strategy: strategy {strategy!r} was not exported")
        result = strategies[strategy]
        if not isinstance(result, Mapping):
            raise NormalizationError(f"$.strategy.{strategy}: expected an object")
        return strategy, result

    if strategy is not None:
        raise NormalizationError(
            "$.strategy: --strategy was supplied, but the export has no strategy map"
        )
    if isinstance(document.get("trades"), list):
        inferred = document.get("strategy_name")
        strategy_name = inferred if isinstance(inferred, str) and inferred else "unknown"
        return strategy_name, document
    raise NormalizationError(
        "$: expected a Freqtrade result with $.strategy.<name>.trades or $.trades"
    )


def _normalize_trade(raw: Any, index: int) -> dict[str, Any]:
    path = f"$.trades[{index}]"
    trade = _mapping(raw, path)
    pair = _required_string(trade, ("pair",), path)
    is_short = _required_bool(trade, ("is_short",), path)
    open_timestamp = canonical_timestamp_ms(
        trade,
        timestamp_keys=("open_timestamp",),
        date_keys=("open_date", "open_date_utc"),
        path=path,
    )
    close_timestamp = canonical_timestamp_ms(
        trade,
        timestamp_keys=("close_timestamp",),
        date_keys=("close_date", "close_date_utc"),
        path=path,
    )
    assert open_timestamp is not None
    assert close_timestamp is not None
    if close_timestamp < open_timestamp:
        raise NormalizationError(
            f"{path}.close_timestamp_ms: close precedes open ({close_timestamp} < {open_timestamp})"
        )

    raw_orders = _required(trade, ("orders",), path)
    if not isinstance(raw_orders, list):
        raise NormalizationError(f"{path}.orders: expected a list")

    return {
        "sequence": index,
        "pair": pair,
        "direction": "short" if is_short else "long",
        "open_timestamp_ms": open_timestamp,
        "close_timestamp_ms": close_timestamp,
        "open_rate": _decimal(trade, ("open_rate",), path),
        "close_rate": _decimal(trade, ("close_rate",), path),
        "amount": _decimal(trade, ("amount",), path),
        "stake_amount": _decimal(trade, ("stake_amount",), path),
        "max_stake_amount": _decimal(trade, ("max_stake_amount",), path),
        "leverage": _decimal(trade, ("leverage",), path),
        "entry_tag": _nullable_string(trade, ("enter_tag", "buy_tag"), path),
        "exit_reason": _required_string(trade, ("exit_reason", "sell_reason"), path),
        "fees": {
            "open_rate": _decimal(trade, ("fee_open",), path),
            "open_cost": _decimal(trade, ("fee_open_cost",), path, nullable=True, optional=True),
            "open_currency": _nullable_string(trade, ("fee_open_currency",), path, optional=True),
            "close_rate": _decimal(trade, ("fee_close",), path),
            "close_cost": _decimal(trade, ("fee_close_cost",), path, nullable=True, optional=True),
            "close_currency": _nullable_string(trade, ("fee_close_currency",), path, optional=True),
            "funding": _decimal(trade, ("funding_fees",), path, nullable=True),
        },
        "profit": {
            "absolute": _decimal(trade, ("profit_abs",), path),
            "ratio": _decimal(trade, ("profit_ratio",), path),
        },
        "liquidation_price": _decimal(
            trade, ("liquidation_price",), path, nullable=True, optional=True
        ),
        "initial_stop_loss": _decimal(
            trade,
            ("initial_stop_loss_abs", "initial_stop_loss"),
            path,
            nullable=True,
            optional=True,
        ),
        "stop_loss": _decimal(
            trade, ("stop_loss_abs", "stop_loss"), path, nullable=True, optional=True
        ),
        "orders": [
            _normalize_order(order, order_index, path)
            for order_index, order in enumerate(raw_orders)
        ],
    }


def _normalize_trade_v2(raw: Any, index: int) -> dict[str, Any]:
    path = f"$.trades[{index}]"
    trade = _mapping(raw, path)
    normalized = _normalize_trade(trade, index)
    return {
        **normalized,
        "duration_minutes": _integer(trade, ("trade_duration",), path, minimum=0),
        "is_open": _required_bool(trade, ("is_open",), path),
        "minimum_rate": _decimal(trade, ("min_rate",), path, nullable=True),
        "maximum_rate": _decimal(trade, ("max_rate",), path, nullable=True),
        "initial_stop_loss_ratio": _decimal(
            trade, ("initial_stop_loss_ratio",), path, nullable=True
        ),
        "stop_loss_ratio": _decimal(trade, ("stop_loss_ratio",), path, nullable=True),
        "weekday": _integer(trade, ("weekday",), path, minimum=0, maximum=6),
    }


def _normalize_lock(raw: Any, index: int) -> dict[str, Any]:
    path = f"$.locks[{index}]"
    lock = _mapping(raw, path)
    return {
        "sequence": index,
        "pair": _required_string(lock, ("pair",), path),
        "side": _required_string(lock, ("side",), path),
        "lock_timestamp_ms": _integer(lock, ("lock_timestamp",), path, minimum=0),
        "lock_end_timestamp_ms": _integer(lock, ("lock_end_timestamp",), path, minimum=0),
        "reason": _nullable_string(lock, ("reason",), path),
        "active": _required_bool(lock, ("active",), path),
    }


def _normalize_order(raw: Any, index: int, trade_path: str) -> dict[str, Any]:
    path = f"{trade_path}.orders[{index}]"
    order = _mapping(raw, path)
    side = _required_string(order, ("ft_order_side", "side"), path).lower()
    if side not in {"buy", "sell"}:
        raise NormalizationError(f"{path}.side: unsupported side {side!r}")

    filled_timestamp = canonical_timestamp_ms(
        order,
        timestamp_keys=("order_filled_timestamp", "filled_timestamp"),
        date_keys=("order_filled_date", "filled_date"),
        path=path,
        nullable=True,
    )
    return {
        "sequence": index,
        "side": side,
        "is_entry": _required_bool(order, ("ft_is_entry", "is_entry"), path),
        "filled_timestamp_ms": filled_timestamp,
        "amount": _decimal(order, ("amount", "filled"), path),
        "price": _decimal(order, ("safe_price", "average", "price"), path, nullable=True),
        "cost": _decimal(order, ("cost",), path, nullable=True),
        "tag": _nullable_string(order, ("order_tag", "ft_order_tag", "tag"), path),
    }


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NormalizationError(f"{path}: expected an object")
    return value


def _required(record: Mapping[str, Any], aliases: tuple[str, ...], path: str) -> Any:
    for alias in aliases:
        if alias in record:
            return record[alias]
    joined = ", ".join(aliases)
    raise NormalizationError(f"{path}: missing required field ({joined})")


def _optional(record: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in record:
            return record[alias]
    return None


def _required_string(record: Mapping[str, Any], aliases: tuple[str, ...], path: str) -> str:
    value = _required(record, aliases, path)
    if not isinstance(value, str) or not value:
        raise NormalizationError(f"{path}.{aliases[0]}: expected a non-empty string")
    return value


def _nullable_string(
    record: Mapping[str, Any],
    aliases: tuple[str, ...],
    path: str,
    *,
    optional: bool = False,
) -> str | None:
    value = _optional(record, aliases) if optional else _required(record, aliases, path)
    if value is None:
        return None
    if not isinstance(value, str):
        raise NormalizationError(f"{path}.{aliases[0]}: expected a string or null")
    return value


def _required_bool(record: Mapping[str, Any], aliases: tuple[str, ...], path: str) -> bool:
    value = _required(record, aliases, path)
    if not isinstance(value, bool):
        raise NormalizationError(f"{path}.{aliases[0]}: expected a boolean")
    return value


def _integer(
    record: Mapping[str, Any],
    aliases: tuple[str, ...],
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _required(record, aliases, path)
    if not isinstance(value, int) or isinstance(value, bool):
        raise NormalizationError(f"{path}.{aliases[0]}: expected an integer")
    if minimum is not None and value < minimum:
        raise NormalizationError(f"{path}.{aliases[0]}: must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise NormalizationError(f"{path}.{aliases[0]}: must be at most {maximum}")
    return value


def _enum_string(
    record: Mapping[str, Any],
    aliases: tuple[str, ...],
    path: str,
    choices: set[str],
) -> str:
    value = _required_string(record, aliases, path)
    if value not in choices:
        joined = ", ".join(sorted(choices))
        raise NormalizationError(f"{path}.{aliases[0]}: expected one of {joined}")
    return value


def _decimal(
    record: Mapping[str, Any],
    aliases: tuple[str, ...],
    path: str,
    *,
    nullable: bool = False,
    optional: bool = False,
) -> str | None:
    value = _optional(record, aliases) if optional else _required(record, aliases, path)
    return canonical_decimal(value, path=f"{path}.{aliases[0]}", nullable=nullable)
