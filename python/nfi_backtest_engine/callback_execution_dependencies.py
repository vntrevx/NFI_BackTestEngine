"""Lexical callback dependency extraction for execution-contract IR."""

from __future__ import annotations

import ast
from collections.abc import Mapping

from .callback_source_routes import _ordered_nodes


def _execution_closure(
    root: str,
    methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[str]:
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        ordered.append(name)
        for callee in _lexical_callees(methods[name], methods):
            visit(callee)

    visit(root)
    return ordered


def _lexical_callees(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    methods: Mapping[str, ast.AST],
) -> list[str]:
    bindings = _delegate_bindings(node, methods)
    result: list[str] = []
    for call in _ordered_nodes(node, ast.Call):
        callees: tuple[str, ...] = ()
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr in methods
        ):
            callees = (call.func.attr,)
        elif isinstance(call.func, ast.Name):
            candidates = [
                value
                for line, name, value in bindings
                if name == call.func.id and line < call.lineno
            ]
            if candidates:
                callees = candidates[-1]
        result.extend(callees)
    return result


def _delegate_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    methods: Mapping[str, ast.AST],
) -> list[tuple[int, str, tuple[str, ...]]]:
    bindings: list[tuple[int, str, tuple[str, ...]]] = []
    for item in _ordered_nodes(node, (ast.Assign, ast.AnnAssign, ast.For)):
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(item, ast.Assign):
            target = item.targets[0] if len(item.targets) == 1 else None
            value = item.value
        elif isinstance(item, ast.AnnAssign):
            target, value = item.target, item.value
        else:
            target, value = item.target, item.iter
        if not isinstance(target, ast.Name) or value is None:
            continue
        delegates = _delegate_values(value, methods, bindings, item.lineno)
        if delegates:
            bindings.append((item.lineno, target.id, delegates))
    return bindings


def _delegate_values(
    node: ast.AST,
    methods: Mapping[str, ast.AST],
    bindings: list[tuple[int, str, tuple[str, ...]]],
    line: int,
) -> tuple[str, ...]:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in methods
    ):
        return (node.attr,)
    if isinstance(node, ast.Tuple | ast.List):
        return tuple(
            callee
            for item in node.elts
            for callee in _delegate_values(item, methods, bindings, line)
        )
    if isinstance(node, ast.Name):
        candidates = [
            value for bound_line, name, value in bindings if name == node.id and bound_line < line
        ]
        return candidates[-1] if candidates else ()
    return ()
