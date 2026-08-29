"""Emitted callback tag and exit-reason extraction."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence

from .callback_contract import JsonObject
from .callback_source_identity import _location
from .callback_source_routes import _ordered_nodes


def _tag_emissions(
    entrypoint: str,
    closure: Sequence[str],
    methods: Mapping[str, ast.AST],
    path_name: str,
) -> list[JsonObject]:
    roles = {
        "adjust_trade_position": "order_tag",
        "custom_exit": "exit_reason",
    }
    role = roles.get(entrypoint)
    if role is None:
        return []
    result: list[JsonObject] = []
    for method_name in closure:
        node = methods[method_name]
        for candidate, owner in _tag_candidates(node):
            for rendered in _render_tag_expressions(candidate):
                result.append(
                    {
                        "entrypoint": entrypoint,
                        "producer_method": method_name,
                        "role": role,
                        **rendered,
                        "location": _location(owner, path_name),
                    }
                )
    return result


def _tag_candidates(node: ast.AST) -> Iterable[tuple[ast.AST, ast.AST]]:
    returned_names = _return_dependency_names(node)
    for item in _ordered_nodes(node, (ast.Return, ast.Assign, ast.AnnAssign)):
        if isinstance(item, ast.Return):
            if item.value is None:
                continue
            values = item.value.elts if isinstance(item.value, ast.Tuple) else [item.value]
            candidate = values[1] if len(values) == 2 else values[0]
            yield candidate, item
            continue
        if isinstance(item, ast.Assign):
            target = item.targets[0] if len(item.targets) == 1 else None
            value = item.value
        else:  # ast.AnnAssign, constrained by _ordered_nodes
            target = item.target
            value = item.value
        if (
            isinstance(target, ast.Name)
            and target.id in returned_names
            and value is not None
            and any(part in target.id.lower() for part in ("tag", "reason", "signal", "mode"))
        ):
            yield value, item


def _return_dependency_names(node: ast.AST) -> set[str]:
    assignments: dict[str, list[ast.AST]] = {}
    for item in _ordered_nodes(node, (ast.Assign, ast.AnnAssign)):
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(item, ast.Assign):
            target = item.targets[0] if len(item.targets) == 1 else None
            value = item.value
        else:
            target = item.target
            value = item.value
        if isinstance(target, ast.Name) and value is not None:
            assignments.setdefault(target.id, []).append(value)
    required = {
        name.id
        for returned in _ordered_nodes(node, ast.Return)
        if returned.value is not None
        for name in ast.walk(returned.value)
        if isinstance(name, ast.Name)
    }
    pending = list(required)
    while pending:
        name = pending.pop()
        for expression in assignments.get(name, []):
            for dependency in ast.walk(expression):
                if isinstance(dependency, ast.Name) and dependency.id not in required:
                    required.add(dependency.id)
                    pending.append(dependency.id)
    return required


def _render_tag_expressions(node: ast.AST) -> list[dict[str, str]]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [{"kind": "literal", "value": node.value}]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return [{"kind": "template", "value": "".join(parts)}]
    if isinstance(node, ast.IfExp):
        # Both branches remain source data; the executable compiler proves the guard.
        return [
            *_render_tag_expressions(node.body),
            *_render_tag_expressions(node.orelse),
        ]
    return []
