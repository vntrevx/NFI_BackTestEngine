"""Validation for X7 leverage, protection, funding, and liquidation contracts."""

from __future__ import annotations

import math
import numbers
from pathlib import Path
from typing import Any

from ..canonical import read_json
from ..data_seal import timeframe_milliseconds
from ..errors import SpecValidationError, StrategyAnalysisError
from ..strategy_overrides import effective_stoploss_ratio

_PROTECTION_TIMING_KEYS = {
    "lookback_period",
    "lookback_period_candles",
    "stop_duration",
    "stop_duration_candles",
    "unlock_at",
}


_SERIALIZED_CALLBACK_BACKENDS = frozenset(
    {
        "rust-entry-confirm-vm",
        "rust-exit-confirm-vm",
        "rust-custom-exit-vm",
        "rust-nfi-x7-trade-manager",
        "rust-nfi-x7-position-adjustment",
        "rust-nfi-x7-leverage",
        "rust-adjustment-vm",
        "rust-noop",
        "rust-order-state",
        "rust-stake-vm",
    }
)


def x7_adapter_blockers(
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    *,
    market_metadata_path: str | Path | None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if not hot_ir.get("hot_loop_ready"):
        blockers.append(
            {
                "code": "X7_CALLBACK_IR_INCOMPLETE",
                "message": "all active X7 callbacks must have exact Rust lowerings",
            }
        )
    callbacks: dict[str, dict[str, Any]] = {}
    for item in hot_ir.get("callbacks", []):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            callbacks[item["name"]] = item
    unsupported = sorted(
        name
        for name, callback in callbacks.items()
        if callback.get("active_for_run") and not _callback_backend_is_consumed(callback)
    )
    if unsupported:
        blockers.append(
            {
                "code": "X7_ADAPTER_BACKEND_UNSUPPORTED",
                "callbacks": unsupported,
                "message": "X7 adapter cannot serialize one or more callback backends",
            }
        )
    trading_mode = config.get("trading_mode", "spot")
    try:
        _x7_protection_contract(analysis, config)
    except StrategyAnalysisError as exc:
        blockers.append(
            {
                "code": "X7_PROTECTION_CONTRACT_INVALID",
                "message": str(exc),
            }
        )
    if trading_mode != "spot":
        leverage_error = _x7_leverage_program_error(callbacks)
        if leverage_error is not None:
            blockers.append(leverage_error)
    constants = analysis["strategies"][0]["constants"]
    try:
        effective_stoploss_ratio(constants, config)
    except StrategyAnalysisError as exc:
        blockers.append(
            {
                "code": "X7_STOPLOSS_CONFIG_INVALID",
                "message": str(exc),
            }
        )
    if (
        constants.get("position_adjustment_enable") is True
        and "adjust_trade_position" not in callbacks
    ):
        blockers.append(
            {
                "code": "X7_POSITION_CALLBACK_REQUIRED",
                "message": "position adjustment is enabled but no callback IR exists",
            }
        )
    if market_metadata_path is None:
        blockers.append(
            {
                "code": "MARKET_METADATA_REQUIRED",
                "message": "compiled X7 execution requires a frozen market snapshot",
            }
        )
    else:
        market_path = Path(market_metadata_path).resolve()
        if not market_path.is_file():
            blockers.append(
                {
                    "code": "MARKET_METADATA_MISSING",
                    "message": f"market snapshot does not exist: {market_path}",
                }
            )
        else:
            snapshot = read_json(market_path)
            markets = snapshot.get("markets")
            if not isinstance(markets, dict):
                blockers.append(
                    {
                        "code": "MARKET_METADATA_INVALID",
                        "message": "market snapshot must contain a markets object",
                    }
                )
            else:
                for pair in config.get("exchange", {}).get("pair_whitelist", []):
                    market = markets.get(pair)
                    if not isinstance(market, dict) or not _market_has_limits(market):
                        blockers.append(
                            {
                                "code": "MARKET_LIMITS_REQUIRED",
                                "pair": pair,
                                "message": f"market snapshot lacks amount/cost limits for {pair}",
                            }
                        )
                if trading_mode == "futures" and not blockers:
                    try:
                        _x7_liquidation_contract(
                            config,
                            snapshot,
                            config.get("exchange", {}).get("pair_whitelist", []),
                        )
                    except StrategyAnalysisError as exc:
                        blockers.append(
                            {
                                "code": "X7_LIQUIDATION_CONTRACT_INVALID",
                                "message": str(exc),
                            }
                        )
    for field in ("dry_run_wallet", "max_open_trades"):
        value = config.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            blockers.append(
                {
                    "code": "X7_NUMERIC_CONFIG_REQUIRED",
                    "field": field,
                    "message": f"config.{field} must be numeric",
                }
            )
    stake = config.get("stake_amount")
    if stake != "unlimited" and (isinstance(stake, bool) or not isinstance(stake, int | float)):
        blockers.append(
            {
                "code": "X7_STAKE_CONFIG_INVALID",
                "message": "config.stake_amount must be numeric or 'unlimited'",
            }
        )
    return blockers


def _callback_backend_is_consumed(callback: dict[str, Any]) -> bool:
    """Return whether the adapter serializes or proves away this callback.

    Open-order timeout callbacks are not simulator programs: the native backtest
    fills accepted orders immediately, so no open order exists on which Freqtrade
    could invoke them. The callback compiler proves that exact execution scope.
    Rechecking the proof envelope here prevents an arbitrary backend label from
    silently dropping executable behavior at the adapter boundary.
    """

    backend = callback.get("backend")
    if backend in _SERIALIZED_CALLBACK_BACKENDS:
        return True
    if backend != "rust-immediate-fill-open-order-proof":
        return False
    lowering = callback.get("lowering")
    operation = lowering.get("operation") if isinstance(lowering, dict) else None
    return (
        callback.get("name") in {"check_entry_timeout", "check_exit_timeout"}
        and callback.get("kind") == "open-order"
        and callback.get("executable_in_rust") is True
        and isinstance(operation, dict)
        and operation.get("opcode") == "open-order-timeout-policy-v1"
        and operation.get("execution_scope") == "unreachable-immediate-fill-backtest-v1"
        and operation.get("orderbook_depth") == 1
    )


def _x7_leverage_program_error(
    callbacks: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate the source-ordered tag-to-leverage contract."""
    callback = callbacks.get("leverage")
    lowering = callback.get("lowering") if isinstance(callback, dict) else None
    operation = lowering.get("operation") if isinstance(lowering, dict) else None
    if (
        not isinstance(callback, dict)
        or callback.get("backend") != "rust-nfi-x7-leverage"
        or not isinstance(operation, dict)
        or operation.get("opcode") != "nfi-x7-leverage-v1"
    ):
        return {
            "code": "X7_FUTURES_LEVERAGE_REQUIRED",
            "message": "X7 futures execution requires the compiled leverage callback",
        }
    default = operation.get("default")
    overrides = operation.get("ordered_tag_overrides")
    if (
        isinstance(default, bool)
        or not isinstance(default, int | float)
        or not math.isfinite(float(default))
        or float(default) <= 0.0
        or not isinstance(overrides, list)
        or not overrides
    ):
        return {
            "code": "X7_FUTURES_LEVERAGE_INVALID",
            "message": "compiled X7 leverage operation is invalid",
        }
    for override in overrides:
        value = override.get("leverage") if isinstance(override, dict) else None
        tags = override.get("entry_tags") if isinstance(override, dict) else None
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) <= 0.0
            or not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) and tag for tag in tags)
        ):
            return {
                "code": "X7_FUTURES_LEVERAGE_INVALID",
                "message": "compiled X7 leverage operation is invalid",
            }
    return None


def _x7_funding_fee_interval_ms(
    config: dict[str, Any],
    vector_report: dict[str, Any],
) -> int | None:
    """Validate the futures refresh cadence sealed by vector preparation."""
    if config.get("trading_mode", "spot") != "futures":
        return None

    contract = vector_report.get("futures_execution")
    if not isinstance(contract, dict) or contract.get("schema_version") != "1.0.0":
        raise StrategyAnalysisError(
            "futures vector report is missing its funding execution contract"
        )
    timeframe = contract.get("funding_fee_timeframe")
    interval_ms = contract.get("funding_fee_interval_ms")
    mark_timeframe = contract.get("mark_timeframe")
    if (
        not isinstance(timeframe, str)
        or not isinstance(mark_timeframe, str)
        or not isinstance(interval_ms, int)
        or isinstance(interval_ms, bool)
        or interval_ms <= 0
    ):
        raise StrategyAnalysisError("futures funding execution contract is invalid")
    try:
        expected_interval_ms = timeframe_milliseconds(timeframe)
        timeframe_milliseconds(mark_timeframe)
    except SpecValidationError as exc:
        raise StrategyAnalysisError(
            "futures funding execution contract contains an invalid timeframe"
        ) from exc
    if interval_ms != expected_interval_ms:
        raise StrategyAnalysisError("futures funding interval does not match its sealed timeframe")
    return interval_ms


def _x7_leverage_contract(
    callbacks: dict[str, dict[str, Any]],
    *,
    trading_mode: Any,
) -> tuple[float, dict[str, Any] | None]:
    if trading_mode != "futures":
        return 1.0, None
    error = _x7_leverage_program_error(callbacks)
    if error is not None:
        raise StrategyAnalysisError(error["message"])
    callback = callbacks["leverage"]
    operation = callback["lowering"]["operation"]
    program = {
        "default": float(operation["default"]),
        "ordered_tag_overrides": [
            {
                "entry_tags": list(override["entry_tags"]),
                "leverage": float(override["leverage"]),
            }
            for override in operation["ordered_tag_overrides"]
        ],
    }
    return program["default"], program


def _market_maximum_leverage(market: Any, pair: str) -> float | None:
    """Read an optional sealed maximum without guessing an exchange limit."""
    if not isinstance(market, dict):
        raise StrategyAnalysisError(f"market snapshot is invalid for {pair}")
    direct = market.get("maximum_leverage")
    limits = market.get("limits")
    leverage_limits = limits.get("leverage") if isinstance(limits, dict) else None
    nested = leverage_limits.get("max") if isinstance(leverage_limits, dict) else None
    raw = direct if direct is not None else nested
    if raw is None:
        return None
    value = _positive_float(raw, f"{pair} maximum leverage")
    if value < 1.0:
        raise StrategyAnalysisError(f"{pair} maximum leverage must be at least 1")
    return value


def _x7_protection_contract(
    analysis: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Compile Freqtrade protection settings into a validated state program."""
    enabled = config.get("enable_protections", False)
    if not isinstance(enabled, bool):
        raise StrategyAnalysisError("config.enable_protections must be boolean")
    if not enabled:
        return None
    strategy = analysis["strategies"][0]
    if strategy.get("protections_static") is not True:
        raise StrategyAnalysisError("strategy.protections must have one static literal return")
    definitions = strategy.get("protections", [])
    if not isinstance(definitions, list):
        raise StrategyAnalysisError("strategy.protections must return a list")

    constants = strategy.get("constants")
    configured_timeframe = config.get("timeframe")
    strategy_timeframe = constants.get("timeframe") if isinstance(constants, dict) else None
    timeframe = configured_timeframe if configured_timeframe is not None else strategy_timeframe
    if not isinstance(timeframe, str) or not timeframe:
        raise StrategyAnalysisError("protection execution requires an effective timeframe")
    try:
        timeframe_ms = timeframe_milliseconds(timeframe)
    except SpecValidationError as exc:
        raise StrategyAnalysisError(f"unsupported protection timeframe {timeframe!r}") from exc
    if timeframe_ms < 60_000 or timeframe_ms % 60_000 != 0:
        raise StrategyAnalysisError("protection timeframe must contain whole minutes")
    timeframe_minutes = timeframe_ms // 60_000

    handlers: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise StrategyAnalysisError(f"protection {index} must be an object")
        method = definition.get("method")
        if method not in {
            "CooldownPeriod",
            "StoplossGuard",
            "MaxDrawdown",
            "LowProfitPairs",
        }:
            raise StrategyAnalysisError(f"protection {index} method is unsupported: {method!r}")
        timing = _protection_timing(
            definition,
            index=index,
            timeframe_minutes=timeframe_minutes,
        )
        common = {"method": method, "timing": timing}
        if method == "CooldownPeriod":
            _protection_keys(definition, index=index, specific=set())
            handlers.append(common)
        elif method == "StoplossGuard":
            _protection_keys(
                definition,
                index=index,
                specific={"trade_limit", "only_per_pair", "only_per_side", "required_profit"},
            )
            handlers.append(
                {
                    **common,
                    "trade_limit": _positive_integer(
                        definition.get("trade_limit", 10),
                        f"protection {index} trade_limit",
                    ),
                    "only_per_pair": _boolean(
                        definition.get("only_per_pair", False),
                        f"protection {index} only_per_pair",
                    ),
                    "only_per_side": _boolean(
                        definition.get("only_per_side", False),
                        f"protection {index} only_per_side",
                    ),
                    "required_profit": _finite_float(
                        definition.get("required_profit", 0.0),
                        f"protection {index} required_profit",
                    ),
                }
            )
        elif method == "LowProfitPairs":
            _protection_keys(
                definition,
                index=index,
                specific={"trade_limit", "only_per_side", "required_profit"},
            )
            required_profit = definition.get("required_profit", 0.0)
            handler = {
                **common,
                "trade_limit": _positive_integer(
                    definition.get("trade_limit", 1),
                    f"protection {index} trade_limit",
                ),
                "only_per_side": _boolean(
                    definition.get("only_per_side", False),
                    f"protection {index} only_per_side",
                ),
                "required_profit": _finite_float(
                    required_profit,
                    f"protection {index} required_profit",
                ),
            }
            required_profit_repr = _integer_numeric_repr(
                required_profit,
                f"protection {index} required_profit",
            )
            if required_profit_repr is not None:
                handler["required_profit_repr"] = required_profit_repr
            handlers.append(handler)
        else:
            _protection_keys(
                definition,
                index=index,
                specific={"trade_limit", "max_allowed_drawdown", "calculation_mode"},
            )
            calculation_mode = definition.get("calculation_mode", "ratios")
            if calculation_mode not in {"ratios", "equity"}:
                raise StrategyAnalysisError(
                    f"protection {index} calculation_mode must be ratios or equity"
                )
            maximum_allowed_drawdown = definition.get("max_allowed_drawdown", 0.0)
            handler = {
                **common,
                "trade_limit": _positive_integer(
                    definition.get("trade_limit", 1),
                    f"protection {index} trade_limit",
                ),
                "maximum_allowed_drawdown": _non_negative_float(
                    maximum_allowed_drawdown,
                    f"protection {index} max_allowed_drawdown",
                ),
                "calculation_mode": calculation_mode,
            }
            maximum_allowed_drawdown_repr = _integer_numeric_repr(
                maximum_allowed_drawdown,
                f"protection {index} max_allowed_drawdown",
            )
            if maximum_allowed_drawdown_repr is not None:
                handler["maximum_allowed_drawdown_repr"] = maximum_allowed_drawdown_repr
            handlers.append(handler)
    return {
        "timeframe_ms": timeframe_ms,
        "handlers": handlers,
    }


def _protection_keys(
    definition: dict[str, Any],
    *,
    index: int,
    specific: set[str],
) -> None:
    unknown = set(definition) - {"method", *_PROTECTION_TIMING_KEYS, *specific}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise StrategyAnalysisError(f"protection {index} has unsupported fields: {names}")


def _protection_timing(
    definition: dict[str, Any],
    *,
    index: int,
    timeframe_minutes: int,
) -> dict[str, Any]:
    lookback_minutes, lookback_text = _protection_period(
        definition,
        minute_key="lookback_period",
        candle_key="lookback_period_candles",
        default_minutes=60,
        timeframe_minutes=timeframe_minutes,
        field=f"protection {index} lookback",
    )
    unlock_at = definition.get("unlock_at")
    has_duration = "stop_duration" in definition or "stop_duration_candles" in definition
    if unlock_at is not None and has_duration:
        raise StrategyAnalysisError(
            f"protection {index} must use unlock_at or stop_duration, not both"
        )
    if unlock_at is not None:
        unlock_minute = _unlock_at_minute(unlock_at, f"protection {index} unlock_at")
        duration_ms = None
        lock_text = f"until {unlock_at}"
    else:
        duration_minutes, duration_text = _protection_period(
            definition,
            minute_key="stop_duration",
            candle_key="stop_duration_candles",
            default_minutes=60,
            timeframe_minutes=timeframe_minutes,
            field=f"protection {index} stop duration",
        )
        unlock_minute = None
        duration_ms = duration_minutes * 60_000
        lock_text = f"for {duration_text}"
    return {
        "lookback_ms": lookback_minutes * 60_000,
        "lookback_text": lookback_text,
        "duration_ms": duration_ms,
        "unlock_at_minute_utc": unlock_minute,
        "lock_text": lock_text,
    }


def _protection_period(
    definition: dict[str, Any],
    *,
    minute_key: str,
    candle_key: str,
    default_minutes: int,
    timeframe_minutes: int,
    field: str,
) -> tuple[int, str]:
    if minute_key in definition and candle_key in definition:
        raise StrategyAnalysisError(f"{field} must use minutes or candles, not both")
    if candle_key in definition:
        count = _positive_integer(definition[candle_key], f"{field} candles")
        return timeframe_minutes * count, f"{count} {_plural(count, 'candle', 'candles')}"
    minutes = _positive_integer(definition.get(minute_key, default_minutes), f"{field} minutes")
    return minutes, f"{minutes} {_plural(minutes, 'minute', 'minutes')}"


def _unlock_at_minute(value: Any, field: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise StrategyAnalysisError(f"{field} must use HH:MM")
    hour, minute = value.split(":", maxsplit=1)
    if not hour.isdigit() or not minute.isdigit():
        raise StrategyAnalysisError(f"{field} must use HH:MM")
    hour_value = int(hour)
    minute_value = int(minute)
    if hour_value > 23 or minute_value > 59:
        raise StrategyAnalysisError(f"{field} must use a valid UTC time")
    return hour_value * 60 + minute_value


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StrategyAnalysisError(f"{field} must be a positive integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise StrategyAnalysisError(f"{field} must be boolean")
    return value


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise StrategyAnalysisError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise StrategyAnalysisError(f"{field} must be finite")
    if isinstance(value, numbers.Integral) and int(result) != value:
        raise StrategyAnalysisError(f"{field} integer must be exactly representable as a float")
    return result


def _integer_numeric_repr(value: Any, field: str) -> str | None:
    """Preserve Python's display for integer thresholds used in protection reasons."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        return None
    if int(float(value)) != value:
        raise StrategyAnalysisError(f"{field} integer must be exactly representable as a float")
    return str(value)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _x7_liquidation_contract(
    config: dict[str, Any],
    market_snapshot: dict[str, Any],
    pairs: list[str],
) -> dict[str, Any] | None:
    """Build the sealed Binance isolated-liquidation input for futures runs."""
    if config.get("trading_mode", "spot") != "futures":
        return None
    if config.get("margin_mode") != "isolated":
        raise StrategyAnalysisError("X7 futures execution requires isolated margin mode")

    exchange_config = config.get("exchange")
    configured_exchange = exchange_config.get("name") if isinstance(exchange_config, dict) else None
    snapshot_exchange = market_snapshot.get("exchange")
    if not isinstance(configured_exchange, str) or not configured_exchange:
        raise StrategyAnalysisError("X7 futures execution requires config.exchange.name")
    if not isinstance(snapshot_exchange, str) or not snapshot_exchange:
        raise StrategyAnalysisError("futures market snapshot lacks its exchange identity")
    exchange = configured_exchange.casefold()
    if exchange != snapshot_exchange.casefold():
        raise StrategyAnalysisError(
            "configured exchange and frozen market snapshot exchange differ"
        )
    if exchange not in {"binance", "binanceusdm"}:
        raise StrategyAnalysisError(
            f"isolated liquidation is not implemented for exchange {configured_exchange}"
        )

    buffer = _non_negative_float(
        config.get("liquidation_buffer", 0.05),
        "config.liquidation_buffer",
    )
    if buffer > 0.99:
        raise StrategyAnalysisError("config.liquidation_buffer must not exceed 0.99")

    markets = market_snapshot.get("markets")
    if not isinstance(markets, dict):
        raise StrategyAnalysisError("market snapshot must contain a markets object")
    tiers_by_pair: dict[str, list[dict[str, float | None]]] = {}
    for pair in pairs:
        market = markets.get(pair)
        tiers = market.get("leverage_tiers") if isinstance(market, dict) else None
        if not isinstance(tiers, list) or not tiers:
            raise StrategyAnalysisError(f"market snapshot lacks leverage tiers for {pair}")
        normalized: list[dict[str, float | None]] = []
        previous_minimum: float | None = None
        for index, tier in enumerate(tiers):
            if not isinstance(tier, dict):
                raise StrategyAnalysisError(f"{pair} leverage tier {index} must be an object")
            minimum = _non_negative_float(
                tier.get("min_notional"),
                f"{pair} leverage tier {index} min_notional",
            )
            maximum = _optional_non_negative_float(
                tier.get("max_notional"),
                f"{pair} leverage tier {index} max_notional",
            )
            maximum_leverage = _positive_float(
                tier.get("maximum_leverage"),
                f"{pair} leverage tier {index} maximum_leverage",
            )
            maintenance_rate = _positive_float(
                tier.get("maintenance_margin_rate"),
                f"{pair} leverage tier {index} maintenance_margin_rate",
            )
            maintenance_amount = _non_negative_float(
                tier.get("maintenance_amount"),
                f"{pair} leverage tier {index} maintenance_amount",
            )
            if maximum is not None and maximum <= minimum:
                raise StrategyAnalysisError(
                    f"{pair} leverage tier {index} max_notional must exceed min_notional"
                )
            if maximum_leverage < 1.0:
                raise StrategyAnalysisError(
                    f"{pair} leverage tier {index} maximum_leverage must be at least 1"
                )
            if maintenance_rate >= 1.0:
                raise StrategyAnalysisError(
                    f"{pair} leverage tier {index} maintenance_margin_rate must be below 1"
                )
            if previous_minimum is not None and minimum <= previous_minimum:
                raise StrategyAnalysisError(
                    f"{pair} leverage tiers must be strictly ordered by min_notional"
                )
            previous_minimum = minimum
            normalized.append(
                {
                    "min_notional": minimum,
                    "max_notional": maximum,
                    "maximum_leverage": maximum_leverage,
                    "maintenance_margin_rate": maintenance_rate,
                    "maintenance_amount": maintenance_amount,
                }
            )
        if normalized[0]["min_notional"] != 0.0:
            raise StrategyAnalysisError(f"{pair} leverage tiers must begin at zero notional")
        tiers_by_pair[pair] = normalized
    return {
        "exchange": exchange,
        "margin_mode": "isolated",
        "buffer": buffer,
        "tiers_by_pair": tiers_by_pair,
    }


def _market_has_limits(market: dict[str, Any]) -> bool:
    limits = market.get("limits")
    return (
        isinstance(limits, dict)
        and isinstance(limits.get("amount"), dict)
        and isinstance(limits.get("cost"), dict)
        and ("min" in limits["amount"] or "min" in limits["cost"])
    )


def _positive_float(value: Any, name: str) -> float:
    result = _non_negative_float(value, name)
    if result <= 0.0:
        raise StrategyAnalysisError(f"{name} must be positive")
    return result


def _optional_non_negative_float(value: Any, name: str) -> float | None:
    return None if value is None else _non_negative_float(value, name)


def _non_negative_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StrategyAnalysisError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise StrategyAnalysisError(f"{name} must be finite and non-negative")
    return result
