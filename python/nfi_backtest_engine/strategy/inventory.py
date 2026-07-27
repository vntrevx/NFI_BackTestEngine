"""Strategy class, callback, constant, and timeframe inventory."""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Any

from . import HOT_CALLBACKS, STRATEGY_CALLBACKS
from .entries import ENTRY_SIGNATURES
from .exits import EXIT_SIGNATURES
from .expressions import (
    _STATIC_UNKNOWN,
    _is_static_int,
    _json_literal,
    _safe_static_value,
)
from .identity import _location, _method_record
from .leverage import LEVERAGE_SIGNATURES
from .position_adjustment import POSITION_ADJUSTMENT_SIGNATURES
from .protections import _static_property_value


def _strategy_record(node: ast.ClassDef, source_lines: list[bytes]) -> dict[str, Any]:
    methods = [
        _method_record(item, source_lines)
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    method_names = {method["name"] for method in methods}
    constants: dict[str, Any] = {}
    dynamic_constants: list[str] = []
    for item in node.body:
        assignment = _class_constant_assignment(item)
        if assignment is None:
            continue
        target, expression = assignment
        try:
            value = _safe_static_value(expression, constants)
        except (ArithmeticError, OverflowError, RecursionError, TypeError, ValueError):
            value = _STATIC_UNKNOWN
        if value is _STATIC_UNKNOWN:
            dynamic_constants.append(target.id)
        else:
            constants[target.id] = _json_literal(value)
    protections, protections_static = _static_property_value(
        node,
        "protections",
        constants,
        missing=[],
    )
    record = {
        "name": node.name,
        "bases": [_qualified_name(base) or ast.unparse(base) for base in node.bases],
        "location": _location(node),
        "constants": constants,
        "dynamic_constants": sorted(dynamic_constants),
        "protections": _json_literal(protections) if protections_static else None,
        "protections_static": protections_static,
        "literal_condition_indices": _literal_condition_indices(node),
        "required_timeframes": _required_timeframes(node, constants),
        "methods": methods,
        "hot_callbacks": sorted(method_names & HOT_CALLBACKS),
        "strategy_callbacks": sorted(method_names & STRATEGY_CALLBACKS),
        "vector_methods": sorted(
            method_names
            & {
                "populate_indicators",
                "populate_entry_trend",
                "populate_exit_trend",
            }
        ),
    }
    fingerprint_identity = {
        "name": record["name"],
        "bases": record["bases"],
            "constants": record["constants"],
            "dynamic_constants": record["dynamic_constants"],
            "protections": record["protections"],
            "protections_static": record["protections_static"],
        "literal_condition_indices": record["literal_condition_indices"],
        "methods": [
            {
                "name": method["name"],
                "source_sha256": method["source_sha256"],
            }
            for method in methods
        ],
    }
    record["capability_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return record

def _class_constant_assignment(item: ast.stmt) -> tuple[ast.Name, ast.expr] | None:
    """Return one simple class assignment without executing annotations.

    Modern Freqtrade strategies commonly spell configuration as
    ``startup_candle_count: int = 800``. The annotation carries no runtime
    value for this inventory; only the literal right-hand side participates in
    the same bounded evaluator used for unannotated assignments.
    """
    if isinstance(item, ast.Assign):
        if len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            return item.targets[0], item.value
        return None
    if (
        isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.value is not None
    ):
        return item.target, item.value
    return None


def _literal_condition_indices(node: ast.ClassDef) -> dict[str, dict[str, list[int]]]:
    """Inventory source branches selected through a literal condition index.

    NFI's large vector methods iterate enabled signal parameters, derive an
    integer such as ``long_entry_condition_index``, and then dispatch through
    independent ``if index == 120`` branches. Mode-tag constants alone do not
    prove that a strategy can emit a tag. Recording the literal branches keeps
    that reachability boundary visible without executing trusted strategy code.
    """
    result: dict[str, dict[str, list[int]]] = {}
    for method in node.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        indices: dict[str, set[int]] = {}
        for item in ast.walk(method):
            if not isinstance(item, ast.Compare) or len(item.ops) != 1:
                continue
            if not isinstance(item.ops[0], ast.Eq) or len(item.comparators) != 1:
                continue
            pair = _literal_index_comparison(item.left, item.comparators[0])
            if pair is None:
                pair = _literal_index_comparison(item.comparators[0], item.left)
            if pair is not None:
                name, value = pair
                indices.setdefault(name, set()).add(value)
        if indices:
            result[method.name] = {name: sorted(values) for name, values in sorted(indices.items())}
    return result


def _literal_index_comparison(name_node: ast.AST, value_node: ast.AST) -> tuple[str, int] | None:
    if not isinstance(name_node, ast.Name) or not name_node.id.endswith("_condition_index"):
        return None
    if not isinstance(value_node, ast.Constant) or not _is_static_int(value_node.value):
        return None
    return name_node.id, value_node.value

def _required_timeframes(node: ast.ClassDef, constants: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    base = constants.get("timeframe")
    if isinstance(base, str):
        values.add(base)
    for name, value in constants.items():
        if "timeframe" in name.lower():
            values.update(_literal_timeframes(value))
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            name = _qualified_name(item.func)
            if name and name.split(".")[-1] in {"informative", "merge_informative_pair"}:
                for argument in item.args:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and _looks_like_timeframe(argument.value)
                    ):
                        values.add(argument.value)
    return sorted(values, key=_timeframe_sort_key)

def _is_strategy_class(node: ast.ClassDef) -> bool:
    return any((_qualified_name(base) or "").split(".")[-1] == "IStrategy" for base in node.bases)


def _imports(tree: ast.Module) -> list[str]:
    result: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return sorted(result)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _looks_like_timeframe(value: str) -> bool:
    return len(value) >= 2 and value[:-1].isdigit() and value[-1] in "smhdwM"


def _timeframe_sort_key(value: str) -> tuple[int, str]:
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}
    return int(value[:-1]) * multipliers[value[-1]], value


def _literal_timeframes(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if _looks_like_timeframe(value) else set()
    if isinstance(value, dict):
        return {
            timeframe
            for key, item in value.items()
            for timeframe in (*_literal_timeframes(key), *_literal_timeframes(item))
        }
    if isinstance(value, list):
        return {timeframe for item in value for timeframe in _literal_timeframes(item)}
    return set()

_LIFECYCLE_SIGNATURES: dict[str, dict[str, Any]] = {
    "bot_start": {"inputs": [], "returns": "none"},
    "bot_loop_start": {"inputs": ["timestamp"], "returns": "none"},
    "check_entry_timeout": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "bool",
    },
    "check_exit_timeout": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "bool",
    },
    "order_filled": {
        "inputs": ["pair", "trade", "order", "timestamp"],
        "returns": "none",
    },
}

CALLBACK_SIGNATURES = {
    **POSITION_ADJUSTMENT_SIGNATURES,
    **_LIFECYCLE_SIGNATURES,
    **ENTRY_SIGNATURES,
    **EXIT_SIGNATURES,
    **LEVERAGE_SIGNATURES,
}

CALLBACK_KINDS = {
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
