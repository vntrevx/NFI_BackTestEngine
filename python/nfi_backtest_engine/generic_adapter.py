"""Fail-closed adapter for a small, callback-free Freqtrade signal subset."""

from __future__ import annotations

import math
import numbers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical import canonical_decimal, read_json, write_json
from .errors import StrategyAnalysisError
from .market_precision import historic_price_steps
from .specs import validate_trade_surface
from .strategy_overrides import effective_stoploss_ratio
from .vector_manifest import (
    EMPTY_TAG_TRANSPORT_SENTINEL,
    VECTOR_MANIFEST_VERSION,
    artifact_execution_start_index,
    contained_vector_path,
    feather_column_names,
    require_columns,
    verified_vector_sha256,
)

GENERIC_ADAPTER_VERSION = "1.4.0"


def generic_adapter_blockers(
    analysis: dict[str, Any],
    config: dict[str, Any],
    *,
    market_metadata_path: str | Path | None,
) -> list[dict[str, Any]]:
    strategy = analysis["strategies"][0]
    constants = strategy["constants"]
    blockers: list[dict[str, Any]] = []
    strategy_callbacks = strategy.get(
        "strategy_callbacks",
        strategy.get("hot_callbacks", []),
    )
    if strategy_callbacks:
        blockers.append(
            {
                "code": "STRATEGY_CALLBACK_ADAPTER_UNSUPPORTED",
                "message": "generic signal adapter requires no strategy callbacks",
            }
        )
    if config.get("trading_mode", "spot") != "spot":
        blockers.append(
            {
                "code": "GENERIC_FUTURES_ADAPTER_UNSUPPORTED",
                "message": "generic signal adapter is certified for spot mode only",
            }
        )
    if constants.get("can_short") is True:
        blockers.append(
            {
                "code": "GENERIC_SHORT_ADAPTER_UNSUPPORTED",
                "message": "generic signal adapter does not certify short signals",
            }
        )
    method_names = {
        method.get("name") for method in strategy.get("methods", []) if isinstance(method, dict)
    }
    if "protections" in method_names or config.get("enable_protections") is True:
        blockers.append(
            {
                "code": "PROTECTIONS_UNSUPPORTED",
                "message": "generic signal adapter does not implement protections or pair locks",
            }
        )
    if constants.get("trailing_stop") is True:
        blockers.append(
            {
                "code": "TRAILING_STOP_UNSUPPORTED",
                "message": "trailing_stop is not implemented by the generic adapter",
            }
        )
    if constants.get("position_adjustment_enable") is True:
        blockers.append(
            {
                "code": "POSITION_ADJUSTMENT_IR_REQUIRED",
                "message": "position adjustment requires a compiled callback IR",
            }
        )
    if constants.get("exit_profit_only", config.get("exit_profit_only", False)) is True:
        blockers.append(
            {
                "code": "EXIT_PROFIT_ONLY_UNSUPPORTED",
                "message": "generic signal adapter does not gate exit signals by profit",
            }
        )
    if config.get("available_capital") is not None:
        blockers.append(
            {
                "code": "AVAILABLE_CAPITAL_UNSUPPORTED",
                "message": "generic signal adapter requires dry_run_wallet sizing",
            }
        )
    try:
        effective_stoploss_ratio(constants, config)
    except StrategyAnalysisError:
        blockers.append(
            {
                "code": "STATIC_STOPLOSS_REQUIRED",
                "message": (
                    "effective strategy stoploss must be a finite ratio "
                    "between -1 and 0"
                ),
            }
        )
    numeric_config: dict[str, float] = {}
    for name in ("dry_run_wallet", "stake_amount", "max_open_trades"):
        value = config.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
        ):
            blockers.append(
                {
                    "code": "NUMERIC_CONFIG_REQUIRED",
                    "field": name,
                    "message": f"config.{name} must be numeric",
                }
            )
        else:
            numeric_config[name] = float(value)
    wallet = numeric_config.get("dry_run_wallet")
    stake = numeric_config.get("stake_amount")
    slots = numeric_config.get("max_open_trades")
    if wallet is not None and wallet <= 0.0:
        blockers.append(
            {
                "code": "POSITIVE_WALLET_REQUIRED",
                "message": "config.dry_run_wallet must be positive",
            }
        )
    if stake is not None and stake <= 0.0:
        blockers.append(
            {
                "code": "POSITIVE_FIXED_STAKE_REQUIRED",
                "message": "config.stake_amount must be a positive fixed amount",
            }
        )
    if slots is not None and (slots <= 0.0 or not slots.is_integer()):
        blockers.append(
            {
                "code": "POSITIVE_INTEGER_SLOTS_REQUIRED",
                "message": "config.max_open_trades must be a positive integer",
            }
        )
    raw_ratio = config.get("tradable_balance_ratio", 0.99)
    ratio = (
        float(raw_ratio)
        if isinstance(raw_ratio, int | float)
        and not isinstance(raw_ratio, bool)
        and math.isfinite(float(raw_ratio))
        else None
    )
    if ratio is None or not 0.0 < ratio <= 1.0:
        blockers.append(
            {
                "code": "TRADABLE_BALANCE_RATIO_INVALID",
                "message": "config.tradable_balance_ratio must be between 0 and 1",
            }
        )
    elif (
        wallet is not None
        and stake is not None
        and slots is not None
        and wallet > 0.0
        and stake > 0.0
        and slots > 0.0
        and slots.is_integer()
        and stake * slots > wallet * ratio
    ):
        blockers.append(
            {
                "code": "STAKE_CAPACITY_UNPROVEN",
                "message": (
                    "configured slots can exceed the tradable wallet; "
                    "last-stake amendment is not implemented"
                ),
            }
        )
    if market_metadata_path is None:
        blockers.append(
            {
                "code": "MARKET_METADATA_REQUIRED",
                "message": "pass --markets with a frozen CCXT market snapshot",
            }
        )
    elif not Path(market_metadata_path).resolve().is_file():
        blockers.append(
            {
                "code": "MARKET_METADATA_MISSING",
                "message": (
                    f"market snapshot does not exist: {Path(market_metadata_path).resolve()}"
                ),
            }
        )
    return blockers


def generic_data_blockers(
    analysis: dict[str, Any],
    vector_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prove that an unimplemented ROI table cannot trigger on these sealed candles."""
    constants = analysis["strategies"][0]["constants"]
    roi = constants.get("minimal_roi", {})
    if roi in ({}, None):
        return []
    if not isinstance(roi, dict) or not roi:
        return [
            {
                "code": "ROI_TABLE_INVALID",
                "message": "minimal_roi must be a literal numeric mapping",
            }
        ]
    values = list(roi.values())
    if any(isinstance(value, bool) or not isinstance(value, int | float) for value in values):
        return [
            {
                "code": "ROI_TABLE_INVALID",
                "message": "minimal_roi values must be literal numbers",
            }
        ]
    minimum_roi = min(float(value) for value in values)
    maximum_possible_return = 0.0
    for artifact in vector_report["outputs"]:
        frame = pd.read_feather(artifact["path"], columns=["high", "low"])
        minimum = _finite_series_extreme(frame, "low", minimum=True)
        maximum = _finite_series_extreme(frame, "high", minimum=False)
        if minimum is None or maximum is None or minimum <= 0.0:
            return [
                {
                    "code": "ROI_BOUND_INVALID",
                    "message": "cannot prove ROI reachability with non-positive prices",
                }
            ]
        maximum_possible_return = max(maximum_possible_return, maximum / minimum - 1.0)
    if maximum_possible_return >= minimum_roi:
        return [
            {
                "code": "ROI_TABLE_REACHABLE",
                "message": (
                    f"sealed candle bound {maximum_possible_return:.8f} can reach "
                    f"minimal_roi {minimum_roi:.8f}"
                ),
            }
        ]
    return []


def _finite_series_extreme(
    frame: pd.DataFrame,
    column_name: str,
    *,
    minimum: bool,
) -> float | None:
    column = frame[column_name]
    if not isinstance(column, pd.Series):
        return None
    value = column.min() if minimum else column.max()
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def build_generic_simulation_input(
    *,
    analysis: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    blockers = generic_adapter_blockers(
        analysis,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    data_blockers = generic_data_blockers(analysis, vector_report)
    if data_blockers:
        raise StrategyAnalysisError(data_blockers[0]["message"])
    strategy = analysis["strategies"][0]
    constants = strategy["constants"]
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot.get("markets")
    if not isinstance(markets, dict):
        raise StrategyAnalysisError("market snapshot must contain a markets object")
    pairs: list[dict[str, Any]] = []
    fee_rates: list[float] = []
    can_short = constants.get("can_short") is True
    use_exit_signal = constants.get("use_exit_signal", config.get("use_exit_signal", True))
    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        market = markets.get(pair)
        if not isinstance(market, dict):
            raise StrategyAnalysisError(f"market snapshot is missing {pair}")
        precision = market.get("precision")
        if not isinstance(precision, dict):
            raise StrategyAnalysisError(f"market snapshot precision is missing for {pair}")
        amount_step = _positive_float(precision.get("amount"), f"{pair} amount precision")
        price_step = _positive_float(precision.get("price"), f"{pair} price precision")
        configured_fee = config.get("fee")
        raw_fee = configured_fee if configured_fee is not None else market.get("taker")
        fee_rates.append(_non_negative_float(raw_fee, f"{pair} taker fee"))
        frame = pd.read_feather(artifact["path"])
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(frame),
        )
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": amount_step,
                "price_step": price_step,
                "price_steps": historic_price_steps(frame),
                "candles": _signal_candles(
                    frame,
                    can_short=can_short,
                    use_exit_signal=use_exit_signal is not False,
                ),
            }
        )
    if not fee_rates:
        raise StrategyAnalysisError("generic adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "generic adapter requires one exact fee rate across all selected pairs"
        )
    document = {
        "schema_version": "1.0.0",
        "config": _generic_portfolio_config(
            constants=constants,
            config=config,
            fee_rate=fee_rates[0],
            amount_step=pairs[0]["amount_step"],
            price_step=pairs[0]["price_step"],
            pair_count=len(pairs),
        ),
        "pairs": pairs,
    }
    write_json(destination, document)
    return document


def build_generic_vector_manifest(
    *,
    analysis: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    """Write a compact Feather manifest for the certified generic subset."""
    blockers = generic_adapter_blockers(
        analysis,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    data_blockers = generic_data_blockers(analysis, vector_report)
    if data_blockers:
        raise StrategyAnalysisError(data_blockers[0]["message"])

    strategy = analysis["strategies"][0]
    constants = strategy["constants"]
    can_short = constants.get("can_short") is True
    use_exit_signal = constants.get("use_exit_signal", config.get("use_exit_signal", True))
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot.get("markets")
    if not isinstance(markets, dict):
        raise StrategyAnalysisError("market snapshot must contain a markets object")
    pairs: list[dict[str, Any]] = []
    fee_rates: list[float] = []

    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        source = Path(artifact["path"]).resolve()
        vector_sha256 = verified_vector_sha256(source, artifact, pair)
        columns = feather_column_names(source, pair)
        require_columns(
            columns,
            {
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "nfi_exec_enter_long",
                "nfi_exec_exit_long",
            },
            pair,
        )
        precision_frame = pd.read_feather(
            source,
            columns=["date", "open", "high", "low", "close"],
        )
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(precision_frame),
        )
        market = markets.get(pair)
        if not isinstance(market, dict):
            raise StrategyAnalysisError(f"market snapshot is missing {pair}")
        precision = market.get("precision")
        if not isinstance(precision, dict):
            raise StrategyAnalysisError(f"market snapshot precision is missing for {pair}")
        amount_step = _positive_float(precision.get("amount"), f"{pair} amount precision")
        price_step = _positive_float(precision.get("price"), f"{pair} price precision")
        configured_fee = config.get("fee")
        raw_fee = configured_fee if configured_fee is not None else market.get("taker")
        fee_rates.append(_non_negative_float(raw_fee, f"{pair} taker fee"))
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": amount_step,
                "price_step": price_step,
                "price_steps": historic_price_steps(precision_frame),
                "minimum_stake": None,
                "minimum_amount": None,
                "minimum_cost": None,
                "vector": {
                    "path": contained_vector_path(source, target.parent, pair),
                    "sha256": vector_sha256,
                    "rows": len(precision_frame),
                    "format": "feather-ipc",
                },
                "feature_columns": [],
                "can_short": can_short,
                "use_exit_signal": use_exit_signal is not False,
                # The generic certified subset has no entry callback and never
                # observes analyzed-frame close data during entry confirmation.
                "include_previous_close": False,
            }
        )
    if not pairs:
        raise StrategyAnalysisError("generic adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "generic adapter requires one exact fee rate across all selected pairs"
        )
    document = {
        "schema_version": VECTOR_MANIFEST_VERSION,
        "config": _generic_portfolio_config(
            constants=constants,
            config=config,
            fee_rate=fee_rates[0],
            amount_step=pairs[0]["amount_step"],
            price_step=pairs[0]["price_step"],
            pair_count=len(pairs),
        ),
        "pairs": pairs,
    }
    write_json(target, document)
    return document


def _generic_portfolio_config(
    *,
    constants: dict[str, Any],
    config: dict[str, Any],
    fee_rate: float,
    amount_step: float,
    price_step: float,
    pair_count: int,
) -> dict[str, Any]:
    """Keep the certified portfolio semantics identical across transports."""
    max_open_trades = int(config["max_open_trades"])
    if max_open_trades <= 0:
        max_open_trades = pair_count
    return {
        "starting_balance": float(config["dry_run_wallet"]),
        "max_open_trades": min(max_open_trades, pair_count),
        "stake_amount": float(config["stake_amount"]),
        "fee_rate": fee_rate,
        "fee_open_rate": fee_rate,
        "fee_close_rate": fee_rate,
        "leverage": 1.0,
        "stoploss_ratio": effective_stoploss_ratio(constants, config),
        "amount_step": amount_step,
        "price_step": price_step,
        "custom_exit_after_ms": None,
        "adjustment_rule": None,
    }


def generic_result_to_surface(
    *,
    result_path: str | Path,
    strategy_name: str,
    config: dict[str, Any],
    timeframe: str,
    timerange: str,
    stoploss_ratio: float,
    destination: str | Path,
) -> dict[str, Any]:
    result = read_json(result_path)
    trades = [
        _surface_trade(trade, sequence, stoploss_ratio)
        for sequence, trade in enumerate(result["trades"])
    ]
    surface = {
        "schema_version": "2.0.0",
        "strategy": strategy_name,
        "context": {
            "trading_mode": config.get("trading_mode", "spot"),
            "margin_mode": config.get("margin_mode", ""),
            "timeframe": timeframe,
            "timeframe_detail": "",
            "timerange": timerange,
        },
        "summary": {
            "total_trades": len(trades),
            "starting_balance": _decimal(result["starting_balance"]),
            # Summary values are already produced in Freqtrade's observable
            # aggregation order. Rounding here destroys exact float-token
            # parity even when the native result itself is identical.
            "final_balance": _decimal(result["final_balance"]),
            "profit_total_abs": _decimal(result["profit_total_abs"]),
            "total_volume": _decimal(result["total_volume"]),
            "rejected_signals": result["rejected_signals"],
            "timedout_entry_orders": 0,
            "timedout_exit_orders": 0,
            "canceled_trade_entries": 0,
            "canceled_entry_orders": 0,
            "replaced_entry_orders": 0,
            "max_open_trades": result["maximum_concurrent_trades"],
        },
        "locks": [
            {
                "sequence": sequence,
                **lock,
            }
            for sequence, lock in enumerate(result.get("locks", []))
        ],
        "trades": trades,
    }
    validate_trade_surface(surface)
    write_json(destination, surface)
    return surface


def _signal_candles(
    frame: pd.DataFrame,
    *,
    can_short: bool,
    use_exit_signal: bool,
) -> list[dict[str, Any]]:
    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "nfi_exec_enter_long",
        "nfi_exec_exit_long",
    }
    missing = required - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            f"vector artifact is missing execution columns: {', '.join(sorted(missing))}"
        )
    records = []
    for row in frame.to_dict(orient="records"):
        timestamp = pd.Timestamp(row["date"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        enter_long = _enabled(row["nfi_exec_enter_long"])
        exit_long = use_exit_signal and _enabled(row["nfi_exec_exit_long"])
        enter_short = can_short and _enabled(row.get("nfi_exec_enter_short", 0))
        exit_short = can_short and use_exit_signal and _enabled(row.get("nfi_exec_exit_short", 0))
        entry_tag = _optional_text(row.get("nfi_exec_enter_tag"))
        exit_tag = _optional_text(row.get("nfi_exec_exit_tag"))
        exit_reason = exit_tag or "exit_signal"
        records.append(
            {
                "timestamp_ms": timestamp.value // 1_000_000,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "enter_long": (
                    {
                        "tag": entry_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if enter_long
                    else None
                ),
                "enter_short": (
                    {
                        "tag": entry_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if enter_short
                    else None
                ),
                "exit_long": {"reason": exit_reason} if exit_long else None,
                "exit_short": {"reason": exit_reason} if exit_short else None,
                "funding_rate": None,
                "funding_mark_price": None,
                "adjustment": None,
            }
        )
    return records


def _surface_trade(
    trade: dict[str, Any],
    sequence: int,
    stoploss_ratio: float,
) -> dict[str, Any]:
    open_time = trade["open_timestamp_ms"]
    close_time = trade["close_timestamp_ms"]
    weekday = datetime.fromtimestamp(close_time / 1000, tz=UTC).weekday()
    return {
        "sequence": sequence,
        "pair": trade["pair"],
        "direction": "short" if trade.get("is_short") else "long",
        "open_timestamp_ms": open_time,
        "close_timestamp_ms": close_time,
        "open_rate": _decimal(trade["open_rate"]),
        "close_rate": _decimal(trade["close_rate"]),
        "amount": _decimal(trade["amount"]),
        # Freqtrade's exported trade model normalizes stake fields to eight
        # decimals after its exact order replay.
        "stake_amount": _decimal(round(trade["stake_amount"], 8)),
        "max_stake_amount": _decimal(round(trade["max_stake_amount"], 8)),
        "leverage": _decimal(trade.get("leverage", 1)),
        "entry_tag": trade["entry_tag"],
        "exit_reason": trade["exit_reason"],
        "fees": {
            "open_rate": _decimal(trade["fee_open"]),
            "open_cost": None,
            "open_currency": None,
            "close_rate": _decimal(trade["fee_close"]),
            "close_cost": None,
            "close_currency": None,
            "funding": _decimal(trade.get("funding_fees", 0)),
        },
        "profit": {
            # Partial exits are rounded individually by Freqtrade and then
            # accumulated. Preserve that already-observable total; a second
            # round here erases the exact addition artifact.
            "absolute": _decimal(trade["profit_abs"]),
            "ratio": _decimal(trade["profit_ratio"]),
        },
        # Freqtrade's 2026.5.1 backtest export does not expose the trade's
        # internal liquidation price. Keep the shared comparison surface
        # limited to fields observable on both sides; exact state probes cover
        # the internal value and its update sequence separately.
        "liquidation_price": None,
        "initial_stop_loss": _decimal(trade["initial_stop_loss"]),
        "stop_loss": _decimal(trade["stop_loss"]),
        "orders": [
            {
                "sequence": order_index,
                "side": order["side"],
                "is_entry": order["is_entry"],
                "filled_timestamp_ms": order["filled_timestamp_ms"],
                "amount": _decimal(order["amount"]),
                "price": _decimal(order["price"]),
                "cost": _decimal(order["cost"]),
                "tag": order["tag"],
            }
            for order_index, order in enumerate(trade["orders"])
        ],
        "duration_minutes": (close_time - open_time) // 60_000,
        "is_open": False,
        "minimum_rate": _decimal(trade["minimum_rate"]),
        "maximum_rate": _decimal(trade["maximum_rate"]),
        "initial_stop_loss_ratio": _decimal(stoploss_ratio),
        "stop_loss_ratio": _decimal(stoploss_ratio),
        "weekday": weekday,
    }


def _enabled(value: Any) -> bool:
    return not pd.isna(value) and float(value) != 0.0


def _optional_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    rendered = str(value)
    return (
        rendered
        if rendered and rendered != EMPTY_TAG_TRANSPORT_SENTINEL
        else None
    )


def _positive_float(value: Any, name: str) -> float:
    result = _non_negative_float(value, name)
    if result <= 0.0:
        raise StrategyAnalysisError(f"{name} must be positive")
    return result


def _non_negative_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StrategyAnalysisError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise StrategyAnalysisError(f"{name} must be finite and non-negative")
    return result


def _decimal(value: Any) -> str:
    result = canonical_decimal(value, path="$generic")
    assert result is not None
    return result
