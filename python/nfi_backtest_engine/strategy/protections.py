"""Static protection-property inventory."""

from __future__ import annotations

import ast
from typing import Any

from .expressions import _STATIC_UNKNOWN, _safe_static_value


def _static_property_value(
    strategy: ast.ClassDef,
    name: str,
    constants: dict[str, Any],
    *,
    missing: Any,
) -> tuple[Any, bool]:
    """Evaluate one literal property without importing the strategy module."""
    candidates = [
        item
        for item in strategy.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == name
    ]
    if not candidates:
        return missing, True
    if len(candidates) != 1 or isinstance(candidates[0], ast.AsyncFunctionDef):
        return None, False
    statements = [
        statement
        for statement in candidates[0].body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return None, False
    return_node = statements[0].value
    if return_node is None:
        return None, False
    value = _safe_static_value(return_node, constants)
    if value is _STATIC_UNKNOWN:
        return None, False
    return value, True
