"""Shared structural AST predicates for callback lowerers."""

from __future__ import annotations

import ast


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _is_none_expression(node: ast.AST | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)
