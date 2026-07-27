"""Bounded static expression lowering for strategy analysis."""

from __future__ import annotations

import ast
from typing import Any, TypeGuard


def _json_literal(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_literal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_literal(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_literal(item) for item in value), key=repr)
    return repr(value)


def _safe_static_value(node: ast.AST, constants: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id, _STATIC_UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        values = [_safe_static_value(item, constants) for item in node.elts]
        if any(value is _STATIC_UNKNOWN for value in values):
            return _STATIC_UNKNOWN
        if isinstance(node, ast.List):
            return values
        if isinstance(node, ast.Tuple):
            return tuple(values)
        return set(values)
    if isinstance(node, ast.Dict):
        if any(item is None for item in node.keys):
            return _STATIC_UNKNOWN
        keys = [_safe_static_value(item, constants) for item in node.keys if item is not None]
        values = [_safe_static_value(item, constants) for item in node.values]
        if any(value is _STATIC_UNKNOWN for value in (*keys, *values)):
            return _STATIC_UNKNOWN
        return dict(zip(keys, values, strict=True))
    if isinstance(node, ast.UnaryOp):
        value = _safe_static_value(node.operand, constants)
        if value is _STATIC_UNKNOWN:
            return _STATIC_UNKNOWN
        if isinstance(node.op, ast.USub) and _is_static_number(value):
            return -value
        if isinstance(node.op, ast.UAdd) and _is_static_number(value):
            return +value
        if isinstance(node.op, ast.Not):
            return not value
        return _STATIC_UNKNOWN
    if isinstance(node, ast.BinOp):
        left = _safe_static_value(node.left, constants)
        right = _safe_static_value(node.right, constants)
        if left is _STATIC_UNKNOWN or right is _STATIC_UNKNOWN:
            return _STATIC_UNKNOWN
        if isinstance(node.op, ast.Add):
            if _is_static_number(left) and _is_static_number(right):
                return left + right
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                return [*left, *right]
            if isinstance(left, tuple) and isinstance(right, tuple):
                return (*left, *right)
        if isinstance(node.op, ast.Sub) and _is_static_number(left) and _is_static_number(right):
            return left - right
        if isinstance(node.op, ast.Mult):
            if _is_static_number(left) and _is_static_number(right):
                return left * right
            if (
                isinstance(left, str)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, list)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, tuple)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and 0 <= right <= 10_000
            ):
                return left * right
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, str)
            ):
                return right * left
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, list)
            ):
                return right * left
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= 10_000
                and isinstance(right, tuple)
            ):
                return right * left
        if (
            isinstance(node.op, ast.Div)
            and isinstance(left, int | float)
            and not isinstance(left, bool)
            and isinstance(right, int | float)
            and not isinstance(right, bool)
        ):
            return left / right
        return _STATIC_UNKNOWN
    if isinstance(node, ast.IfExp):
        condition = _safe_static_value(node.test, constants)
        if not isinstance(condition, bool):
            return _STATIC_UNKNOWN
        return _safe_static_value(node.body if condition else node.orelse, constants)
    return _STATIC_UNKNOWN


def _is_static_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_static_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000


_STATIC_UNKNOWN = object()
