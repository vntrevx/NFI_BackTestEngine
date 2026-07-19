"""Typed capability IR for callbacks that affect backtest semantics."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .callback_lowering import CALLBACK_LOWERING_VERSION, lower_strategy_callbacks
from .errors import StrategyAnalysisError
from .nfi_trade_manager import build_nfi_trade_manager_ir
from .trade_ir import (
    TRADE_IR_VERSION,
    build_trade_dependency_ir,
    summarize_trade_dependency_ir,
)

HOT_IR_VERSION = "1.9.0"

_SIGNATURES: dict[str, dict[str, Any]] = {
    "adjust_entry_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
        ],
        "returns": "price|null",
    },
    "adjust_exit_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
        ],
        "returns": "price|null",
    },
    "adjust_order_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
            "is_entry",
        ],
        "returns": "price|null",
    },
    "adjust_trade_position": {
        "inputs": ["trade", "timestamp", "rate", "profit", "wallet", "orders"],
        "returns": "position_adjustment|null",
    },
    "bot_start": {
        "inputs": [],
        "returns": "none",
    },
    "bot_loop_start": {
        "inputs": ["timestamp"],
        "returns": "none",
    },
    "check_entry_timeout": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "bool",
    },
    "check_exit_timeout": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "bool",
    },
    "confirm_trade_entry": {
        "inputs": ["pair", "order_type", "amount", "rate", "timestamp", "side"],
        "returns": "bool",
    },
    "confirm_trade_exit": {
        "inputs": ["pair", "trade", "order_type", "amount", "rate", "reason", "timestamp"],
        "returns": "bool",
    },
    "custom_entry_price": {
        "inputs": ["pair", "trade|null", "timestamp", "proposed_rate", "entry_tag", "side"],
        "returns": "price",
    },
    "custom_exit": {
        "inputs": ["pair", "trade", "timestamp", "rate", "profit"],
        "returns": "exit_reason|null",
    },
    "custom_exit_price": {
        "inputs": ["pair", "trade", "timestamp", "proposed_rate", "profit", "exit_tag"],
        "returns": "price",
    },
    "custom_roi": {
        "inputs": [
            "pair",
            "trade",
            "timestamp",
            "duration_minutes",
            "entry_tag|null",
            "side",
        ],
        "returns": "roi_ratio",
    },
    "custom_stake_amount": {
        "inputs": [
            "pair",
            "timestamp",
            "rate",
            "proposed_stake",
            "minimum_stake|null",
            "maximum_stake",
            "leverage",
            "entry_tag|null",
            "side",
        ],
        "returns": "stake",
    },
    "custom_stoploss": {
        "inputs": ["pair", "trade", "timestamp", "rate", "profit", "after_fill"],
        "returns": "stoploss_ratio",
    },
    "leverage": {
        "inputs": [
            "pair",
            "timestamp",
            "rate",
            "proposed_leverage",
            "maximum_leverage",
            "entry_tag|null",
            "side",
        ],
        "returns": "leverage",
    },
    "order_filled": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "none",
    },
}

_CALLBACK_KIND = {
    "bot_start": "lifecycle",
    "bot_loop_start": "per-candle",
    "order_filled": "order-event",
    "check_entry_timeout": "open-order",
    "check_exit_timeout": "open-order",
    "adjust_entry_price": "open-order",
    "adjust_exit_price": "open-order",
    "adjust_order_price": "open-order",
    "adjust_trade_position": "per-trade-per-candle",
    "custom_exit": "per-trade-per-candle",
    "custom_stoploss": "per-trade-per-candle",
    "custom_roi": "per-trade-per-candle",
}


def build_hot_callback_ir(
    analysis: dict[str, Any],
    *,
    trading_mode: str | None = None,
    run_mode: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a deterministic typed inventory without pretending to compile Python."""
    strategies = analysis.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise StrategyAnalysisError("typed callback IR requires exactly one selected strategy")
    strategy = strategies[0]
    methods = {
        method["name"]: method
        for method in strategy.get("methods", [])
        if isinstance(method, dict) and isinstance(method.get("name"), str)
    }
    selected_callbacks = strategy.get(
        "strategy_callbacks",
        strategy.get("hot_callbacks", []),
    )
    lowerings = lower_strategy_callbacks(analysis, run_mode=run_mode, config=config)
    trade_report = (
        build_trade_dependency_ir(analysis)
        if {"adjust_trade_position", "custom_exit"} & set(selected_callbacks)
        else None
    )
    trade_dependency_ir = (
        summarize_trade_dependency_ir(trade_report) if trade_report is not None else None
    )
    # The NFI-specific descriptor deliberately remains separate from generic
    # callback lowering. It is exact only for its declared entry-tag scope;
    # vector preflight enforces that scope before the Rust event loop starts.
    nfi_trade_manager = (
        build_nfi_trade_manager_ir(analysis, trade_report) if trade_report is not None else None
    )
    callbacks = []
    for name in selected_callbacks:
        method = methods[name]
        signature = _SIGNATURES.get(
            name,
            {
                "inputs": method.get("parameters", [])[1:],
                "returns": "unknown",
            },
        )
        active = not (name == "leverage" and trading_mode == "spot")
        lowering = lowerings.get(name)
        callback = {
            "name": name,
            "source_sha256": method["source_sha256"],
            "inputs": signature["inputs"],
            "returns": signature["returns"],
            "node_count": method.get("node_count", 0),
            "calls": method.get("calls", []),
            "kind": _CALLBACK_KIND.get(name, "entry-or-exit-event"),
            "active_for_run": active,
            "inactive_reason": (
                "Freqtrade does not call leverage() in spot mode" if not active else None
            ),
            "backend": "uncompiled-python-source",
            "executable_in_rust": False,
            "lowering": None,
        }
        if lowering is not None:
            callback["backend"] = lowering["backend"]
            callback["executable_in_rust"] = lowering["executable_in_rust"]
            callback["lowering"] = lowering
        if name == "custom_exit" and nfi_trade_manager is not None:
            callback["backend"] = nfi_trade_manager["backend"]
            callback["executable_in_rust"] = nfi_trade_manager["executable_in_rust"]
            callback["lowering"] = nfi_trade_manager
        manager_operation = (
            nfi_trade_manager.get("operation") if isinstance(nfi_trade_manager, dict) else None
        )
        if (
            name == "adjust_trade_position"
            and isinstance(nfi_trade_manager, dict)
            and isinstance(manager_operation, dict)
            and isinstance(manager_operation.get("position_adjustment"), dict)
        ):
            callback["backend"] = "rust-nfi-x7-position-adjustment"
            callback["executable_in_rust"] = nfi_trade_manager["executable_in_rust"]
            callback["lowering"] = nfi_trade_manager
        callbacks.append(callback)
    identity = {
        "schema_version": HOT_IR_VERSION,
        "callback_lowering_version": CALLBACK_LOWERING_VERSION,
        "trade_ir_version": TRADE_IR_VERSION,
        "strategy_fingerprint": strategy["capability_fingerprint"],
        "callbacks": callbacks,
        "trade_dependency_ir": trade_dependency_ir,
        "nfi_trade_manager": nfi_trade_manager,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        **identity,
        "fingerprint": fingerprint,
        "hot_loop_ready": not any(
            callback["active_for_run"] and not callback["executable_in_rust"]
            for callback in callbacks
        ),
        "execution_policy": {
            "python_per_candle": False,
            "unsupported_callback_action": "fail-before-simulation",
        },
        "blockers": [
            {
                "code": "STRATEGY_CALLBACK_NOT_COMPILED",
                "callback": callback["name"],
                "message": (
                    f"{callback['name']}() has a typed contract but no exact Rust lowering"
                ),
            }
            for callback in callbacks
            if callback["active_for_run"] and not callback["executable_in_rust"]
        ],
    }
