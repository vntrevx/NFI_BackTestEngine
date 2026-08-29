"""Fail-closed structural lowering facade for strategy callbacks."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from .callback_confirm import _lower_confirm_trade_entry
from .callback_confirm_expression import (
    _compile_confirm_expression as _compile_confirm_expression_impl,
)
from .callback_contract import CALLBACK_LOWERING_VERSION as _CALLBACK_LOWERING_VERSION
from .callback_execution_contract import (
    CALLBACK_EXECUTION_IR_VERSION,
    compile_callback_execution_ir,
)
from .callback_exit_confirm import _lower_confirm_trade_exit
from .callback_leverage import _lower_x7_leverage
from .callback_lifecycle import _lower_backtest_bot_loop_start
from .callback_order_state import _lower_x7_order_filled
from .callback_scalar import _lower_scalar_trade_callback
from .callback_stake import _lower_custom_stake_amount
from .callback_timeout import _lower_immediate_fill_timeout
from .errors import StrategyAnalysisError

__all__ = [
    "CALLBACK_EXECUTION_IR_VERSION",
    "CALLBACK_LOWERING_VERSION",
    "compile_callback_execution_ir",
    "lower_strategy_callbacks",
]

CALLBACK_LOWERING_VERSION = _CALLBACK_LOWERING_VERSION
_compile_confirm_expression = _compile_confirm_expression_impl


def lower_strategy_callbacks(
    analysis: dict[str, Any],
    *,
    run_mode: str | None,
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return exact callback lowerings keyed by callback name."""
    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1 or not isinstance(source, dict):
        raise StrategyAnalysisError("callback lowering requires one selected strategy")
    source_path = source.get("path")
    source_sha256 = source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise StrategyAnalysisError("callback lowering requires a hash-bound source")
    path = Path(source_path).resolve()
    try:
        source_bytes = path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(f"callback lowering source cannot be read: {path}") from exc
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise StrategyAnalysisError("callback lowering source hash differs from analysis")
    try:
        tree = ast.parse(source_text, filename=str(path), type_comments=True)
    except SyntaxError as exc:  # pragma: no cover - analysis already parsed this source
        raise StrategyAnalysisError("callback lowering source no longer parses") from exc

    strategy_name = strategies[0].get("name")
    strategy_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_name
        ),
        None,
    )
    if strategy_node is None:
        raise StrategyAnalysisError("callback lowering strategy class disappeared")

    lowered: dict[str, dict[str, Any]] = {}
    constants = strategies[0].get("constants", {})
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("callback lowering strategy constants are invalid")
    effective_constants = dict(constants)
    if config is not None:
        # NFI's safe configuration wrapper copies these top-level values onto
        # the strategy instance before a backtest starts. Freeze the same
        # effective values into callback IR so Rust never observes stale class
        # defaults. Only fields consumed by reviewed lowerers belong here.
        for name in (
            "exit_profit_only",
            "exit_profit_offset",
            "futures_mode_leverage",
            "futures_mode_leverage_rebuy_mode",
            "futures_mode_leverage_grind_mode",
        ):
            value = config.get(name)
            if isinstance(value, bool | int | float):
                effective_constants[name] = value
    method_nodes = {
        node.name: node
        for node in strategy_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for node in method_nodes.values():
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        descriptor = _lower_callback(
            node,
            run_mode=run_mode,
            constants=effective_constants,
            method_nodes=method_nodes,
        )
        if descriptor is not None:
            lowered[node.name] = descriptor
    scalar_callbacks = {
        "adjust_trade_position": (
            "rust-adjustment-vm",
            "adjust-trade-position-scalar-bundle-v1",
        ),
        "custom_exit": (
            "rust-custom-exit-vm",
            "custom-exit-scalar-bundle-v1",
        ),
    }
    for callback_name, (backend, opcode) in scalar_callbacks.items():
        if callback_name not in method_nodes or callback_name in lowered:
            continue
        descriptor = _lower_scalar_trade_callback(
            analysis,
            callback_name=callback_name,
            backend=backend,
            opcode=opcode,
        )
        if descriptor is not None:
            lowered[callback_name] = descriptor
    return lowered


def _lower_callback(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str | None,
    constants: dict[str, Any],
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> dict[str, Any] | None:
    if node.name == "bot_loop_start":
        return _lower_backtest_bot_loop_start(node, run_mode=run_mode)
    if node.name == "order_filled":
        return _lower_x7_order_filled(node, constants=constants)
    if node.name == "custom_stake_amount":
        return _lower_custom_stake_amount(node, constants=constants)
    if node.name == "leverage":
        return _lower_x7_leverage(node, constants=constants)
    if node.name == "confirm_trade_entry":
        return _lower_confirm_trade_entry(
            node,
            constants=constants,
            method_nodes=method_nodes,
        )
    if node.name == "confirm_trade_exit":
        return _lower_confirm_trade_exit(
            node,
            constants=constants,
            method_nodes=method_nodes,
            run_mode=run_mode,
        )
    if node.name in {"check_entry_timeout", "check_exit_timeout"}:
        return _lower_immediate_fill_timeout(
            node,
            run_mode=run_mode,
            method_nodes=method_nodes,
        )
    return None
