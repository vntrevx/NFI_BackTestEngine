"""Exact order-tag bypass parsing for timeout callbacks."""

from __future__ import annotations

import ast


def _timeout_skip_order_tag(statement: ast.stmt) -> str | None:
    """Extract an exact order-tag bypass without binding a strategy-specific tag."""
    if (
        not isinstance(statement, ast.If)
        or statement.orelse
        or len(statement.body) != 1
        or not _is_boolean_return(statement.body[0], value=False)
        or not isinstance(statement.test, ast.BoolOp)
        or not isinstance(statement.test.op, ast.And)
        or len(statement.test.values) != 2
    ):
        return None
    present = False
    tag: str | None = None
    for condition in statement.test.values:
        if _is_order_tag_present(condition):
            if present:
                return None
            present = True
            continue
        extracted = _order_tag_equality(condition)
        if extracted is None or tag is not None:
            return None
        tag = extracted
    return tag if present else None


def _is_order_tag_present(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Compare)
        and _is_order_tag_attribute(expression.left)
        and len(expression.ops) == 1
        and isinstance(expression.ops[0], ast.IsNot)
        and len(expression.comparators) == 1
        and isinstance(expression.comparators[0], ast.Constant)
        and expression.comparators[0].value is None
    )


def _order_tag_equality(expression: ast.expr) -> str | None:
    if (
        not isinstance(expression, ast.Compare)
        or not _is_order_tag_attribute(expression.left)
        or len(expression.ops) != 1
        or not isinstance(expression.ops[0], ast.Eq)
        or len(expression.comparators) != 1
        or not isinstance(expression.comparators[0], ast.Constant)
        or not isinstance(expression.comparators[0].value, str)
        or not expression.comparators[0].value
    ):
        return None
    return expression.comparators[0].value


def _is_order_tag_attribute(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Attribute)
        and expression.attr == "ft_order_tag"
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "order"
    )


def _is_boolean_return(statement: ast.stmt, *, value: bool) -> bool:
    return (
        isinstance(statement, ast.Return)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is value
    )
