"""Run-mode lifecycle callback lowering and proofs."""

from __future__ import annotations

import ast

from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject


def _lower_backtest_bot_loop_start(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str | None,
) -> JsonObject | None:
    if (
        isinstance(node, ast.AsyncFunctionDef)
        or run_mode not in {"backtest", "hyperopt"}
        or not node.body
    ):
        return None
    first = node.body[0]
    if not isinstance(first, ast.If):
        return None
    excluded_modes = _runmode_not_in_values(first.test)
    if excluded_modes is None or run_mode in excluded_modes:
        return None
    if len(first.body) != 1 or not _is_base_callback_return(
        first.body[0],
        "bot_loop_start",
    ):
        return None
    return {
        "backend": "rust-noop",
        "executable_in_rust": True,
        "operation": {
            "opcode": "noop",
            "reason": "backtest branch delegates directly to the Freqtrade base callback",
        },
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "runmode-not-in-base-delegation-v1",
            "run_mode": run_mode,
            "excluded_modes": sorted(excluded_modes),
            "first_statement_line": first.lineno,
        },
    }


def _runmode_not_in_values(node: ast.AST) -> set[str] | None:
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.NotIn)
        or len(node.comparators) != 1
        or not _is_self_config_runmode_value(node.left)
    ):
        return None
    comparator = node.comparators[0]
    if not isinstance(comparator, ast.Tuple | ast.List | ast.Set):
        return None
    values: set[str] = set()
    for item in comparator.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.add(item.value)
    return values


def _is_self_config_runmode_value(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute) or node.attr != "value":
        return False
    subscription = node.value
    return (
        isinstance(subscription, ast.Subscript)
        and isinstance(subscription.value, ast.Attribute)
        and subscription.value.attr == "config"
        and isinstance(subscription.value.value, ast.Name)
        and subscription.value.value.id == "self"
        and isinstance(subscription.slice, ast.Constant)
        and subscription.slice.value == "runmode"
    )


def _is_base_callback_return(node: ast.AST, callback: str) -> bool:
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == callback
        and isinstance(function.value, ast.Call)
        and isinstance(function.value.func, ast.Name)
        and function.value.func.id == "super"
        and not function.value.args
        and not function.value.keywords
    )
