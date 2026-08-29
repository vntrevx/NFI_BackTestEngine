"""AST feature extraction shared by deterministic strategy source differences."""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SIGNAL_TAG = re.compile(r"^\s*\d+(?:\s+\d+)*\s*$")
_GRIND_LEVEL = re.compile(
    r"(?i)(?:grind|derisk|buyback|rebuy|(?:sg|gd|gm|gmd|dd|ddl|g|d))"
    r"(?:[_ -]*(?:level)?[_ -]*)?(\d+)"
)
_ROUTE_SELECTOR = re.compile(
    r"(?i)(?:signal|tag|route|grind|derisk|"
    r"condition.*(?:index|id)|(?:index|id).*condition)"
)


def _changed_source_spans(
    _old_inventory: Mapping[str, Any],
    new_inventory: Mapping[str, Any],
    methods: list[str],
) -> list[dict[str, int | str]]:
    callbacks = new_inventory.get("callbacks")
    changed = new_inventory.get("changed_source_spans")
    if not isinstance(callbacks, Mapping) or not isinstance(changed, list):
        return []
    result = []
    for method in methods:
        record = callbacks.get(method)
        location = record.get("location") if isinstance(record, Mapping) else None
        if not isinstance(location, Mapping):
            continue
        for span in changed:
            if (
                isinstance(span, Mapping)
                and int(location["line"]) <= int(span["line"])
                and int(span["end_line"]) <= int(location["end_line"])
            ):
                result.append({"method": method, **span})
    return result


def _changed_line_spans(old_path: Path, new_path: Path) -> list[dict[str, int]]:
    old_lines = old_path.read_text(encoding="utf-8").splitlines()
    new_lines = new_path.read_text(encoding="utf-8").splitlines()
    return [
        {
            "line": new_start + 1,
            "column": 0,
            "end_line": max(new_start + 1, new_end),
            "end_column": len(new_lines[new_end - 1]) if new_end > new_start else 0,
        }
        for operation, _old_start, _old_end, new_start, new_end in difflib.SequenceMatcher(
            None,
            old_lines,
            new_lines,
            autojunk=False,
        ).get_opcodes()
        if operation != "equal"
    ]


def _method_tags(method: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(method):
        value: ast.AST | None = None
        target: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AugAssign):
            target, value = node.target, node.value
        if target is not None and value is not None:
            target_text = ast.unparse(target)
            if "tag" in target_text.lower():
                result.update(_string_literals(value))
        if isinstance(node, ast.Return) and node.value is not None:
            result.update(
                value for value in _string_literals(node.value) if _looks_like_action_tag(value)
            )
    return {value.strip() for value in result if value.strip()}


def _looks_like_action_tag(value: str) -> bool:
    lowered = value.lower()
    return bool(
        _SIGNAL_TAG.fullmatch(value)
        or _GRIND_LEVEL.search(value)
        or any(token in lowered for token in ("signal", "entry", "exit", "stop"))
    )


def _string_literals(node: ast.AST) -> set[str]:
    return {
        str(item.value)
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _method_calls(method: ast.AST, method_names: set[str]) -> set[str]:
    return {
        name
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        if (name := ast.unparse(node.func).split(".")[-1]) in method_names
    }


def _method_routes(method: ast.AST) -> dict[str, str]:
    route_nodes: dict[str, list[str]] = {}
    for node in ast.walk(method):
        if not isinstance(node, ast.If) or not _route_selector(node.test):
            continue
        fingerprint_source = ast.dump(
            node,
            annotate_fields=True,
            include_attributes=False,
        )
        for value in _route_values(node.test):
            route_nodes.setdefault(value, []).append(fingerprint_source)
    return {
        route: hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()
        for route, nodes in route_nodes.items()
    }


def _route_selector(test: ast.AST) -> bool:
    names = [node.id for node in ast.walk(test) if isinstance(node, ast.Name)]
    names.extend(node.attr for node in ast.walk(test) if isinstance(node, ast.Attribute))
    return any(_ROUTE_SELECTOR.search(name) for name in names)


def _route_values(test: ast.AST) -> set[str]:
    return {
        str(node.value).strip()
        for node in ast.walk(test)
        if isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and isinstance(node.value, str | int)
        and str(node.value).strip()
    }


def _dataframe_columns(method: ast.AST) -> set[str]:
    result = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Subscript):
            continue
        receiver = ast.unparse(node.value).lower()
        if "dataframe" not in receiver and not receiver.endswith("df"):
            continue
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            result.add(node.slice.value)
        elif isinstance(node.slice, ast.Tuple):
            result.update(
                str(item.value)
                for item in node.slice.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return result


def _custom_state_keys(method: ast.AST) -> set[str]:
    result = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func).split(".")[-1]
        if name not in {"get_custom_data", "set_custom_data", "delete_custom_data"}:
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            key = node.args[0].value
            if isinstance(key, str):
                result.add(key)
    return result


def _method_opcodes(method: ast.AST) -> set[str]:
    result = set()
    for node in ast.walk(method):
        if isinstance(node, ast.If):
            result.add("if")
        elif isinstance(node, ast.For):
            result.add("for")
        elif isinstance(node, ast.While):
            result.add("while")
        elif isinstance(node, ast.Return):
            result.add("return")
        elif isinstance(node, ast.Delete):
            result.add("delete")
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            result.add("assign")
        elif isinstance(node, ast.Call):
            result.add(f"call:{ast.unparse(node.func).split('.')[-1]}")
    return result
