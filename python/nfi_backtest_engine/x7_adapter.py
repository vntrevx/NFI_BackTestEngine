"""Compiled NFI X7 adapter for the Rust simulator input contract."""

from __future__ import annotations

import math
import numbers
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .canonical import read_json, write_json
from .data_seal import timeframe_milliseconds
from .errors import SpecValidationError, StrategyAnalysisError
from .market_precision import historic_price_steps
from .vector_manifest import (
    EMPTY_TAG_TRANSPORT_SENTINEL,
    VECTOR_MANIFEST_VERSION,
    artifact_execution_start_index,
    contained_vector_path,
    feather_column_names,
    require_columns,
    verified_vector_sha256,
)

X7_ADAPTER_VERSION = "0.14.0"


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
    supported_backends = {
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
    unsupported = sorted(
        name
        for name, callback in callbacks.items()
        if callback.get("active_for_run") and callback.get("backend") not in supported_backends
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


def build_x7_simulation_input(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    blockers = x7_adapter_blockers(
        analysis,
        hot_ir,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot["markets"]
    required_features = _required_trade_features(hot_ir)
    nfi_manager = _nfi_trade_manager_config(hot_ir)
    can_short = config.get("trading_mode", "spot") == "futures"
    pairs = []
    fee_rates = []
    maximum_leverage_by_pair: dict[str, float] = {}
    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        market = markets[pair]
        precision = market["precision"]
        limits = market["limits"]
        fee = config.get("fee", market.get("taker"))
        fee_rates.append(_non_negative_float(fee, f"{pair} fee"))
        maximum_leverage = _market_maximum_leverage(market, pair)
        if maximum_leverage is not None:
            maximum_leverage_by_pair[pair] = maximum_leverage
        frame = pd.read_feather(artifact["path"])
        if can_short:
            require_columns(
                set(frame.columns),
                {"nfi_exec_funding_rate", "nfi_exec_funding_mark_price"},
                pair,
            )
        if nfi_manager is not None:
            _validate_nfi_frame_scope(frame, pair, nfi_manager, can_short=can_short)
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(frame),
        )
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": _positive_float(
                    precision.get("amount"),
                    f"{pair} amount precision",
                ),
                "price_step": _positive_float(
                    precision.get("price"),
                    f"{pair} price precision",
                ),
                "price_steps": historic_price_steps(frame),
                "minimum_stake": None,
                "minimum_amount": _optional_non_negative_float(
                    limits["amount"].get("min"),
                    f"{pair} minimum amount",
                ),
                "minimum_cost": _optional_non_negative_float(
                    limits["cost"].get("min"),
                    f"{pair} minimum cost",
                ),
                "feature_columns": _x7_feature_columns(
                    frame,
                    required_features,
                ),
                "candles": _x7_signal_candles(frame, can_short=can_short),
            }
        )
    if not pairs:
        raise StrategyAnalysisError("compiled X7 adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "compiled X7 adapter requires one exact fee across selected markets"
        )
    portfolio_config = _x7_portfolio_config(
        analysis=analysis,
        hot_ir=hot_ir,
        config=config,
        nfi_manager=nfi_manager,
        fee_rate=fee_rates[0],
        amount_step=pairs[0]["amount_step"],
        price_step=pairs[0]["price_step"],
        pair_count=len(pairs),
        maximum_leverage_by_pair=maximum_leverage_by_pair,
        liquidation_model=_x7_liquidation_contract(
            config,
            market_snapshot,
            [pair["pair"] for pair in pairs],
        ),
    )
    document = {
        "schema_version": "1.0.0",
        "config": portfolio_config,
        "pairs": pairs,
    }
    write_json(destination, document)
    return document


def build_x7_vector_manifest(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    vector_report: dict[str, Any],
    market_metadata_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    """Write the compact, SHA-bound Feather input used by release runs.

    Only OHLC columns needed for historical tick reconstruction and two signal
    columns needed for the NFI route gate cross Python. Rust reads the candles,
    tags, and 100+ callback features directly from the same sealed Feather
    file. This removes the old 300+ MB JSON copy without weakening the
    source-bound NFI entry-tag check.
    """
    blockers = x7_adapter_blockers(
        analysis,
        hot_ir,
        config,
        market_metadata_path=market_metadata_path,
    )
    if blockers:
        raise StrategyAnalysisError(blockers[0]["message"])
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    market_snapshot = read_json(market_metadata_path)
    markets = market_snapshot["markets"]
    required_features = _required_trade_features(hot_ir)
    nfi_manager = _nfi_trade_manager_config(hot_ir)
    can_short = config.get("trading_mode", "spot") == "futures"
    pairs: list[dict[str, Any]] = []
    fee_rates: list[float] = []
    maximum_leverage_by_pair: dict[str, float] = {}

    for artifact in vector_report["outputs"]:
        pair = artifact["pair"]
        source = Path(artifact["path"]).resolve()
        vector_sha256 = verified_vector_sha256(source, artifact, pair)
        columns = feather_column_names(source, pair)
        required_columns = {
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "nfi_exec_enter_long",
                "nfi_exec_exit_long",
                "nfi_exec_enter_tag",
                *required_features,
        }
        if can_short:
            required_columns.update(
                {
                    "nfi_exec_enter_short",
                    "nfi_exec_exit_short",
                    "nfi_exec_funding_rate",
                    "nfi_exec_funding_mark_price",
                }
            )
        require_columns(columns, required_columns, pair)
        precision_columns = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "nfi_exec_enter_long",
            "nfi_exec_enter_tag",
        ]
        if can_short:
            precision_columns.extend(
                [
                    "nfi_exec_enter_short",
                    "nfi_exec_exit_short",
                    "nfi_exec_funding_rate",
                    "nfi_exec_funding_mark_price",
                ]
            )
        precision_frame = pd.read_feather(
            source,
            columns=precision_columns,
        )
        if nfi_manager is not None:
            _validate_nfi_frame_scope(
                precision_frame,
                pair,
                nfi_manager,
                can_short=can_short,
            )
        execution_start_index = artifact_execution_start_index(
            artifact,
            pair,
            len(precision_frame),
        )
        market = markets[pair]
        precision = market["precision"]
        limits = market["limits"]
        fee = config.get("fee", market.get("taker"))
        fee_rates.append(_non_negative_float(fee, f"{pair} fee"))
        maximum_leverage = _market_maximum_leverage(market, pair)
        if maximum_leverage is not None:
            maximum_leverage_by_pair[pair] = maximum_leverage
        relative_path = contained_vector_path(source, target.parent, pair)
        pairs.append(
            {
                "pair": pair,
                "execution_start_index": execution_start_index,
                "amount_step": _positive_float(
                    precision.get("amount"),
                    f"{pair} amount precision",
                ),
                "price_step": _positive_float(
                    precision.get("price"),
                    f"{pair} price precision",
                ),
                "price_steps": historic_price_steps(precision_frame),
                "minimum_stake": None,
                "minimum_amount": _optional_non_negative_float(
                    limits["amount"].get("min"),
                    f"{pair} minimum amount",
                ),
                "minimum_cost": _optional_non_negative_float(
                    limits["cost"].get("min"),
                    f"{pair} minimum cost",
                ),
                "vector": {
                    "path": relative_path,
                    "sha256": vector_sha256,
                    "rows": len(precision_frame),
                    "format": "feather-ipc",
                },
                "feature_columns": required_features,
                "can_short": can_short,
                "include_funding": can_short,
                "use_exit_signal": True,
                # X7 confirm_trade_entry reads the final analyzed close. The
                # vector signal is shifted to the next open, so this must be
                # the preceding row rather than the execution row's close.
                "include_previous_close": True,
            }
        )
    if not pairs:
        raise StrategyAnalysisError("compiled X7 adapter requires vector outputs")
    if any(rate != fee_rates[0] for rate in fee_rates[1:]):
        raise StrategyAnalysisError(
            "compiled X7 adapter requires one exact fee across selected markets"
        )
    document = {
        "schema_version": VECTOR_MANIFEST_VERSION,
        "config": _x7_portfolio_config(
            analysis=analysis,
            hot_ir=hot_ir,
            config=config,
            nfi_manager=nfi_manager,
            fee_rate=fee_rates[0],
            amount_step=pairs[0]["amount_step"],
            price_step=pairs[0]["price_step"],
            pair_count=len(pairs),
            maximum_leverage_by_pair=maximum_leverage_by_pair,
            liquidation_model=_x7_liquidation_contract(
                config,
                market_snapshot,
                [pair["pair"] for pair in pairs],
            ),
        ),
        "pairs": pairs,
    }
    write_json(target, document)
    return document


def _x7_portfolio_config(
    *,
    analysis: dict[str, Any],
    hot_ir: dict[str, Any],
    config: dict[str, Any],
    nfi_manager: dict[str, Any] | None,
    fee_rate: float,
    amount_step: float,
    price_step: float,
    pair_count: int,
    maximum_leverage_by_pair: dict[str, float],
    liquidation_model: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize callbacks once for both JSON and Feather transports."""
    callbacks = {item["name"]: item for item in hot_ir["callbacks"]}
    order_operation = _operation(callbacks, "order_filled", "order-filled-state-v1")
    stake_operation = _operation(
        callbacks,
        "custom_stake_amount",
        "custom-stake-program-v1",
    )
    entry_confirmation = _operation(
        callbacks,
        "confirm_trade_entry",
        "entry-confirm-program-v1",
    )
    exit_confirmation = _operation(
        callbacks,
        "confirm_trade_exit",
        "exit-confirm-program-v1",
    )
    custom_exit = (
        None
        if callbacks.get("custom_exit", {}).get("backend") == "rust-nfi-x7-trade-manager"
        else _operation(
            callbacks,
            "custom_exit",
            "custom-exit-scalar-bundle-v1",
        )
    )
    position_adjustment = (
        None
        if callbacks.get("adjust_trade_position", {}).get("backend")
        == "rust-nfi-x7-position-adjustment"
        else _operation(
            callbacks,
            "adjust_trade_position",
            "adjust-trade-position-scalar-bundle-v1",
        )
    )
    leverage, leverage_program = _x7_leverage_contract(
        callbacks,
        trading_mode=config.get("trading_mode", "spot"),
    )
    constants = analysis["strategies"][0]["constants"]
    max_open_trades = int(config["max_open_trades"])
    if max_open_trades <= 0:
        max_open_trades = pair_count
    raw_stake = config["stake_amount"]
    unlimited = raw_stake == "unlimited"
    starting_balance = float(config["dry_run_wallet"])
    return {
        "starting_balance": starting_balance,
        "max_open_trades": max_open_trades,
        "stake_amount": starting_balance if unlimited else float(raw_stake),
        "fee_rate": fee_rate,
        "fee_open_rate": fee_rate,
        "fee_close_rate": fee_rate,
        "leverage": leverage,
        "nfi_leverage_program": leverage_program,
        "maximum_leverage_by_pair": maximum_leverage_by_pair,
        "liquidation_model": liquidation_model,
        "protection_program": _x7_protection_contract(analysis, config),
        "stoploss_ratio": float(constants["stoploss"]),
        "amount_step": amount_step,
        "price_step": price_step,
        "custom_exit_after_ms": None,
        "adjustment_rule": None,
        "callback_program": (
            {
                "order_filled": {
                    "initial_successful_entry_writes": order_operation[
                        "initial_successful_entry_writes"
                    ],
                    "order_tag_actions": order_operation["order_tag_actions"],
                }
            }
            if order_operation is not None
            else None
        ),
        "stake_program": (
            {"statements": stake_operation["statements"]} if stake_operation is not None else None
        ),
        "amount_reserve_percent": float(config.get("amount_reserve_percent", 0.05)),
        "unlimited_stake": unlimited,
        "tradable_balance_ratio": float(config.get("tradable_balance_ratio", 0.99)),
        "entry_confirmation_program": (
            {
                "statements": entry_confirmation["statements"],
                "functions": entry_confirmation["functions"],
            }
            if entry_confirmation is not None
            else None
        ),
        "exit_confirmation_program": (
            {
                "statements": exit_confirmation["statements"],
                "functions": exit_confirmation["functions"],
            }
            if exit_confirmation is not None
            else None
        ),
        "custom_exit_program": (
            {
                "schema_version": custom_exit["schema_version"],
                "entry": custom_exit["entry"],
                "programs": custom_exit["programs"],
            }
            if custom_exit is not None
            else None
        ),
        "adjust_trade_position_program": (
            {
                "schema_version": position_adjustment["schema_version"],
                "entry": position_adjustment["entry"],
                "programs": position_adjustment["programs"],
            }
            if position_adjustment is not None
            else None
        ),
        "nfi_x7_trade_manager": nfi_manager,
        "max_entry_position_adjustment": int(constants.get("max_entry_position_adjustment", -1)),
        "is_futures": config.get("trading_mode", "spot") == "futures",
    }


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
            handlers.append(
                {
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
                        definition.get("required_profit", 0.0),
                        f"protection {index} required_profit",
                    ),
                }
            )
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
            handlers.append(
                {
                    **common,
                    "trade_limit": _positive_integer(
                        definition.get("trade_limit", 1),
                        f"protection {index} trade_limit",
                    ),
                    "maximum_allowed_drawdown": _non_negative_float(
                        definition.get("max_allowed_drawdown", 0.0),
                        f"protection {index} max_allowed_drawdown",
                    ),
                    "calculation_mode": calculation_mode,
                }
            )
    return {
        "timeframe_ms": timeframe_ms,
        "handlers": handlers,
    }


_PROTECTION_TIMING_KEYS = {
    "lookback_period",
    "lookback_period_candles",
    "stop_duration",
    "stop_duration_candles",
    "unlock_at",
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
    return result


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
    configured_exchange = (
        exchange_config.get("name") if isinstance(exchange_config, dict) else None
    )
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


def _operation(
    callbacks: dict[str, dict[str, Any]],
    name: str,
    opcode: str,
) -> dict[str, Any] | None:
    callback = callbacks.get(name)
    if callback is None or not callback.get("active_for_run"):
        return None
    lowering = callback.get("lowering")
    operation = lowering.get("operation") if isinstance(lowering, dict) else None
    if not isinstance(operation, dict) or operation.get("opcode") != opcode:
        raise StrategyAnalysisError(f"compiled callback operation differs for {name}")
    return operation


def _nfi_trade_manager_config(hot_ir: dict[str, Any]) -> dict[str, Any] | None:
    callbacks = hot_ir.get("callbacks")
    manager_selected = isinstance(callbacks, list) and any(
        isinstance(callback, dict)
        and callback.get("name") == "custom_exit"
        and callback.get("active_for_run")
        and callback.get("backend") == "rust-nfi-x7-trade-manager"
        for callback in callbacks
    )
    if not manager_selected:
        return None
    manager = hot_ir.get("nfi_trade_manager")
    if not isinstance(manager, dict):
        return None
    if not manager.get("executable_in_rust"):
        raise StrategyAnalysisError("NFI trade manager is not executable")
    operation = manager.get("operation")
    if not isinstance(operation, dict) or operation.get("opcode") != "nfi-x7-trade-manager-v1":
        raise StrategyAnalysisError("NFI trade manager operation is invalid")
    routes = operation.get("supported_routes")
    route_order = operation.get("route_order")
    short_routes = operation.get("supported_short_routes")
    short_route_order = operation.get("short_route_order")
    long_grind = routes.get("long_grind") if isinstance(routes, dict) else None
    long_btc = routes.get("long_btc") if isinstance(routes, dict) else None
    adjustment = operation.get("position_adjustment")
    rebuy_adjustment = operation.get("rebuy_adjustment")
    short_rebuy_adjustment = operation.get("short_rebuy_adjustment")
    programs = operation.get("programs")
    constants = operation.get("constants")
    source_sha256 = operation.get("source_sha256")
    if (
        not isinstance(routes, dict)
        or not isinstance(route_order, list)
        or not route_order
        or not all(isinstance(name, str) and name for name in route_order)
        or not isinstance(short_routes, dict)
        or not isinstance(short_route_order, list)
        or not short_route_order
        or not all(isinstance(name, str) and name for name in short_route_order)
        or not isinstance(programs, dict)
        or not isinstance(constants, dict)
        or not isinstance(source_sha256, str)
    ):
        raise StrategyAnalysisError("NFI managed-long operation is incomplete")
    managed_routes: list[dict[str, Any]] = []
    for key in route_order:
        route = routes.get(key)
        if not isinstance(route, dict):
            raise StrategyAnalysisError(f"NFI route order references missing route {key}")
        profile = route.get("profile")
        if not isinstance(profile, str):
            # Legacy grind/BTC routes are serialized in their dedicated
            # fields because their adjustment state machine is different.
            continue
        record = {
            "key": key,
            "profile": profile,
            "mode_name": route["mode_name"],
            "entry_tags": route["entry_tags"],
        }
        for name in ("stop_threshold_futures", "stop_threshold_spot"):
            if name in route:
                record[name] = route[name]
        managed_routes.append(record)
    if not managed_routes:
        raise StrategyAnalysisError("NFI operation has no managed-long route")
    managed_short_routes: list[dict[str, Any]] = []
    for key in short_route_order:
        route = short_routes.get(key)
        if not isinstance(route, dict):
            raise StrategyAnalysisError(f"NFI short route order references missing route {key}")
        required = (
            "profile",
            "mode_name",
            "entry_tags",
            "stop_threshold_futures",
            "stop_threshold_spot",
        )
        if any(name not in route for name in required):
            raise StrategyAnalysisError(f"NFI short route {key} is incomplete")
        managed_short_routes.append(
            {
                "key": key,
                **{name: route[name] for name in required},
            }
        )
    constant_names = (
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
    if any(name not in constants for name in constant_names):
        raise StrategyAnalysisError("NFI top-coins constants are incomplete")

    def legacy_route_config(route: Any) -> dict[str, Any] | None:
        if not isinstance(route, dict):
            return None
        names = (
            "mode_name",
            "entry_tags",
            "exit_profit_threshold",
            "adjustment_scope",
            "grind_mode",
            "decision_program",
            "first_entry_profit_threshold_spot",
            "first_entry_stop_threshold_spot",
            "derisk_use_grind_stops",
            "stateful_input_contract",
            "constants",
        )
        if any(name not in route for name in names):
            raise StrategyAnalysisError("NFI legacy route is incomplete")
        record = {name: route[name] for name in names}
        for name in ("regular_decision_program", "regular_constants"):
            if name in route:
                record[name] = route[name]
        return record

    return {
        "schema_version": operation["schema_version"],
        "source_sha256": source_sha256,
        "route_order": route_order,
        "managed_long_routes": managed_routes,
        "short_route_order": short_route_order,
        "managed_short_routes": managed_short_routes,
        "long_grind": legacy_route_config(long_grind),
        "long_btc": legacy_route_config(long_btc),
        "position_adjustment": adjustment if isinstance(adjustment, dict) else None,
        "rebuy_adjustment": (rebuy_adjustment if isinstance(rebuy_adjustment, dict) else None),
        "short_rebuy_adjustment": (
            short_rebuy_adjustment if isinstance(short_rebuy_adjustment, dict) else None
        ),
        "constants": {name: constants[name] for name in constant_names},
        "programs": programs,
    }


def _validate_nfi_frame_scope(
    frame: pd.DataFrame,
    pair: str,
    manager: dict[str, Any],
    *,
    can_short: bool,
) -> None:
    managed = manager.get("managed_long_routes")
    if not isinstance(managed, list):
        raise StrategyAnalysisError("NFI managed-long routes are invalid")
    routes = [route for route in managed if isinstance(route, dict)]
    routes.extend(
        route
        for name in ("long_grind", "long_btc")
        if isinstance((route := manager.get(name)), dict)
    )
    supported_long: set[str] = set()
    for route in routes:
        entry_tags = route.get("entry_tags")
        if not isinstance(entry_tags, list) or not all(
            isinstance(tag, str) and tag for tag in entry_tags
        ):
            raise StrategyAnalysisError("NFI route tags are invalid")
        supported_long.update(entry_tags)
    if not supported_long:
        raise StrategyAnalysisError("NFI adapter has no executable entry-tag route")
    required = {"nfi_exec_enter_long", "nfi_exec_enter_tag"}
    supported_short: set[str] = set()
    if can_short:
        managed_short = manager.get("managed_short_routes")
        if not isinstance(managed_short, list) or not managed_short:
            raise StrategyAnalysisError("NFI managed-short routes are invalid")
        for route in managed_short:
            entry_tags = route.get("entry_tags") if isinstance(route, dict) else None
            if not isinstance(entry_tags, list) or not all(
                isinstance(tag, str) and tag for tag in entry_tags
            ):
                raise StrategyAnalysisError("NFI short route tags are invalid")
            supported_short.update(entry_tags)
        if not supported_short:
            raise StrategyAnalysisError("NFI adapter has no executable short entry-tag route")
        required.add("nfi_exec_enter_short")
    missing = required - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            "NFI route scope check is missing: " + ", ".join(sorted(missing))
        )
    _validate_signal_tags(
        _series_column(frame, "nfi_exec_enter_long"),
        _series_column(frame, "nfi_exec_enter_tag"),
        supported_long,
        pair=pair,
        side="long",
    )
    if can_short:
        _validate_signal_tags(
            _series_column(frame, "nfi_exec_enter_short"),
            _series_column(frame, "nfi_exec_enter_tag"),
            supported_short,
            pair=pair,
            side="short",
        )


def _series_column(frame: pd.DataFrame, name: str) -> pd.Series:
    column = frame[name]
    if not isinstance(column, pd.Series):
        raise StrategyAnalysisError(f"NFI vector column is duplicated: {name}")
    return column


def _validate_signal_tags(
    signals: pd.Series,
    tags: pd.Series,
    supported: set[str],
    *,
    pair: str,
    side: str,
) -> None:
    for signal, raw_tag in zip(signals, tags, strict=True):
        if not _enabled(signal):
            continue
        entry_tag = _optional_text(raw_tag)
        words = entry_tag.split() if entry_tag is not None else []
        if not words or any(word not in supported for word in words):
            shown = entry_tag if entry_tag is not None else "<none>"
            side_label = "" if side == "long" else f"{side} "
            raise StrategyAnalysisError(
                f"NFI adapter does not support {side_label}entry tag {shown!r} for {pair}"
            )
        # Later signals may occur while the pair already has an open trade and
        # are therefore ignored by Freqtrade. The native chronological loop
        # performs the definitive route check only when a signal can open a
        # trade; rejecting every raw vector signal would reject valid fixtures.
        break


def _x7_signal_candles(
    frame: pd.DataFrame,
    *,
    can_short: bool,
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
    if can_short:
        missing.update(
            {
                "nfi_exec_enter_short",
                "nfi_exec_exit_short",
                "nfi_exec_funding_rate",
                "nfi_exec_funding_mark_price",
            }
            - set(frame.columns)
        )
    if missing:
        raise StrategyAnalysisError(
            f"vector artifact is missing execution columns: {', '.join(sorted(missing))}"
        )
    records = []
    previous_close: float | None = None
    for row in frame.to_dict(orient="records"):
        timestamp = pd.Timestamp(row["date"])
        timestamp = (
            timestamp.tz_localize("UTC")
            if timestamp.tzinfo is None
            else timestamp.tz_convert("UTC")
        )
        enter_tag = _optional_text(row.get("nfi_exec_enter_tag"))
        exit_tag = _optional_text(row.get("nfi_exec_exit_tag"))
        funding_rate = _optional_finite_number(row.get("nfi_exec_funding_rate"))
        funding_mark_price = _optional_finite_number(
            row.get("nfi_exec_funding_mark_price")
        )
        if (funding_rate is None) != (funding_mark_price is None):
            raise StrategyAnalysisError(
                "funding rate and mark price must be present on the same candle"
            )
        if funding_mark_price is not None and funding_mark_price <= 0.0:
            raise StrategyAnalysisError("funding mark price must be positive")
        records.append(
            {
                "timestamp_ms": timestamp.value // 1_000_000,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "previous_close": previous_close,
                "enter_long": (
                    {
                        "tag": enter_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if _enabled(row["nfi_exec_enter_long"])
                    else None
                ),
                "enter_short": (
                    {
                        "tag": enter_tag,
                        "leverage": None,
                        "liquidation_price": None,
                    }
                    if can_short and _enabled(row["nfi_exec_enter_short"])
                    else None
                ),
                "exit_long": (
                    {"reason": exit_tag or "exit_signal"}
                    if _enabled(row["nfi_exec_exit_long"])
                    else None
                ),
                "exit_short": (
                    {"reason": exit_tag or "exit_signal"}
                    if can_short and _enabled(row["nfi_exec_exit_short"])
                    else None
                ),
                "funding_rate": funding_rate,
                "funding_mark_price": funding_mark_price,
                "adjustment": None,
            }
        )
        previous_close = float(row["close"])
    return records


def _required_trade_features(hot_ir: dict[str, Any]) -> list[str]:
    """Return only dataframe columns named by a compiled trade decision.

    Generic callback programs live in ``trade_dependency_ir``. NFI's
    top-coins decisions are reached through a literal function tuple, so their
    source-bound contracts live in ``nfi_trade_manager`` instead. Reading both
    locations prevents the adapter from relying on accidental call-graph
    reachability when the strategy router is refactored.
    """
    columns: set[str] = set()
    dependency_ir = hot_ir.get("trade_dependency_ir")
    if isinstance(dependency_ir, dict):
        compiled = dependency_ir.get("compiled_scalar_methods")
        if isinstance(compiled, dict):
            _collect_indexed_features(columns, compiled.values())
    manager = hot_ir.get("nfi_trade_manager")
    if isinstance(manager, dict):
        proof = manager.get("proof")
        programs = proof.get("programs") if isinstance(proof, dict) else None
        if isinstance(programs, dict):
            _collect_indexed_features(columns, programs.values())
        operation = manager.get("operation")
        routes = operation.get("supported_routes") if isinstance(operation, dict) else None
        if isinstance(routes, dict):
            _collect_indexed_features(columns, routes.values())
        if isinstance(operation, dict):
            adjustment = operation.get("position_adjustment")
            if isinstance(adjustment, dict):
                _collect_indexed_features(columns, [adjustment])
            rebuy_adjustment = operation.get("rebuy_adjustment")
            if isinstance(rebuy_adjustment, dict):
                _collect_indexed_features(columns, [rebuy_adjustment])
    return sorted(columns)


def _collect_indexed_features(
    columns: set[str],
    records: Any,
) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        contract = record.get("input_contract", record.get("stateful_input_contract"))
        indexed_fields = contract.get("indexed_fields") if isinstance(contract, dict) else None
        if not isinstance(indexed_fields, dict):
            continue
        for fields in indexed_fields.values():
            if isinstance(fields, list):
                columns.update(field for field in fields if isinstance(field, str))


def _x7_feature_columns(
    frame: pd.DataFrame,
    required_features: list[str],
) -> dict[str, list[Any]]:
    missing = set(required_features) - set(frame.columns)
    if missing:
        raise StrategyAnalysisError(
            "vector artifact is missing trade-decision features: " + ", ".join(sorted(missing))
        )
    return {
        column: [_scalar_feature_value(value, column) for value in frame[column]]
        for column in required_features
    }


def _scalar_feature_value(value: Any, column: str) -> Any:
    missing = pd.isna(value)
    if isinstance(missing, bool) and missing:
        return {"$float": "nan"}
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if not isinstance(value, numbers.Real):
        raise StrategyAnalysisError(f"trade-decision feature {column} must contain numeric scalars")
    number = float(value)
    if math.isnan(number):
        return {"$float": "nan"}
    if math.isinf(number):
        return {"$float": "infinity" if number > 0 else "-infinity"}
    return number


def _market_has_limits(market: dict[str, Any]) -> bool:
    limits = market.get("limits")
    return (
        isinstance(limits, dict)
        and isinstance(limits.get("amount"), dict)
        and isinstance(limits.get("cost"), dict)
        and ("min" in limits["amount"] or "min" in limits["cost"])
    )


def _enabled(value: Any) -> bool:
    return not pd.isna(value) and float(value) != 0.0


def _optional_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value)
    return text if text and text != EMPTY_TAG_TRANSPORT_SENTINEL else None


def _optional_finite_number(value: Any) -> float | None:
    """Decode a nullable funding scalar without accepting infinities."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise StrategyAnalysisError("funding event values must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise StrategyAnalysisError("funding event values must be finite")
    return result


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
