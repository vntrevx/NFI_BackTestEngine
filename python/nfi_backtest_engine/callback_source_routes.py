"""Route constants and reachable callback call graph extraction."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence

from .callback_contract import JsonObject, JsonValue
from .callback_source_identity import _append_unique, _location

_TAG_NAME_PARTS = ("tag", "route")


def _route_constants(constants: JsonValue) -> dict[str, list[str]]:
    if not isinstance(constants, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for name, value in constants.items():
        if not isinstance(name, str) or not any(part in name.lower() for part in _TAG_NAME_PARTS):
            continue
        values: list[str] = []
        for item in _ordered_strings(value):
            _append_unique(values, item)
        if values:
            result[name] = values
    return result


def _ordered_strings(value: JsonValue) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _ordered_strings(key)
            yield from _ordered_strings(item)
        return
    if isinstance(value, Sequence):
        for item in value:
            yield from _ordered_strings(item)


def _constant_locations(
    class_node: ast.ClassDef,
    path_name: str,
) -> dict[str, JsonObject]:
    result: dict[str, JsonObject] = {}
    for node in class_node.body:
        target: ast.Name | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target
        if target is not None:
            result[target.id] = _location(node, path_name)
    return result


def _reachable_methods(
    root: str,
    methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    path_name: str,
) -> tuple[list[str], list[JsonObject]]:
    ordered: list[str] = []
    edges: list[JsonObject] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            return
        visiting.add(name)
        ordered.append(name)
        node = methods[name]
        for callee, call in _method_calls(node, methods):
            edges.append(
                {
                    "caller": name,
                    "callee": callee,
                    "location": _location(call, path_name),
                }
            )
            if callee not in visiting:
                visit(callee)

    visit(root)
    return ordered, edges


def _method_calls(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    methods: Mapping[str, ast.AST],
) -> list[tuple[str, ast.Call]]:
    aliases: dict[str, str] = {}
    for item in _ordered_nodes(node, (ast.Assign, ast.AnnAssign)):
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(item, ast.Assign):
            target = item.targets[0] if len(item.targets) == 1 else None
            value = item.value
        else:
            target = item.target
            value = item.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr in methods
        ):
            aliases[target.id] = value.attr
    result: list[tuple[str, ast.Call]] = []
    for call in _ordered_nodes(node, ast.Call):
        callee: str | None = None
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        ):
            callee = call.func.attr
        elif isinstance(call.func, ast.Name):
            callee = aliases.get(call.func.id)
        if callee is not None and callee in methods:
            result.append((callee, call))
    return result


def _used_route_keys(
    closure: Sequence[str],
    methods: Mapping[str, ast.AST],
    route_constants: Mapping[str, list[str]],
) -> list[str]:
    result: list[str] = []
    for method_name in closure:
        for node in _ordered_nodes(methods[method_name], ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in route_constants
            ):
                _append_unique(result, node.attr)
    return result


def _ordered_nodes[Node: ast.AST](
    node: ast.AST,
    kinds: type[Node] | tuple[type[Node], ...],
) -> list[Node]:
    return sorted(
        (item for item in ast.walk(node) if isinstance(item, kinds)),
        key=lambda item: (
            getattr(item, "lineno", 0),
            getattr(item, "col_offset", 0),
            getattr(item, "end_lineno", 0),
            getattr(item, "end_col_offset", 0),
        ),
    )
