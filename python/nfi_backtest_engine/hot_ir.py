"""Typed capability IR for callbacks that affect backtest semantics."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .callback_lowering import CALLBACK_LOWERING_VERSION, lower_strategy_callbacks
from .errors import StrategyAnalysisError
from .nfi_trade_manager import build_nfi_trade_manager_ir
from .strategy.inventory import (
    CALLBACK_KINDS as _CALLBACK_KIND,
)
from .strategy.inventory import (
    CALLBACK_SIGNATURES as _SIGNATURES,
)
from .trade_ir import (
    TRADE_IR_VERSION,
    build_trade_dependency_ir,
    summarize_trade_dependency_ir,
)

HOT_IR_VERSION = "1.10.0"


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
