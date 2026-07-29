"""Deterministic AST/IR-oriented differences between strategy revisions."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import StrategyAnalysisError
from .strategy import STRATEGY_CALLBACKS
from .strategy_ir import analyze_strategy

STRATEGY_DIFF_VERSION = "1.0.0"
_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}
_STATEFUL_CALLBACKS = {
    "order_filled",
    "adjust_trade_position",
    "custom_exit",
}
_SIGNAL_TAG = re.compile(r"^\s*\d+(?:\s+\d+)*\s*$")
_GRIND_LEVEL = re.compile(
    r"(?i)(?:grind|derisk|buyback|rebuy|(?:sg|gd|gm|gmd|dd|ddl|g|d))"
    r"(?:[_ -]*(?:level)?[_ -]*)?(\d+)"
)


def diff_strategies(
    old_source: str | Path,
    new_source: str | Path,
    *,
    class_name: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Describe behavior-relevant source changes without executing either file."""

    old = _inventory(Path(old_source).resolve(), class_name=class_name)
    new = _inventory(Path(new_source).resolve(), class_name=class_name)
    changed_callbacks = _changed_callbacks(old["callbacks"], new["callbacks"])
    changes = {
        "signals": _set_change(old["signals"], new["signals"]),
        "tags": _set_change(old["tags"], new["tags"]),
        "callbacks": {
            "added": sorted(set(new["callbacks"]) - set(old["callbacks"])),
            "removed": sorted(set(old["callbacks"]) - set(new["callbacks"])),
            "changed": changed_callbacks,
            "locations": {
                name: new["callbacks"][name]["location"]
                for name in changed_callbacks
                if name in new["callbacks"]
            },
        },
        "dataframe_columns": _set_change(old["columns"], new["columns"]),
        "custom_state_keys": _set_change(old["state_keys"], new["state_keys"]),
        "grind_levels": _set_change(old["grind_levels"], new["grind_levels"]),
        "opcodes": _set_change(old["opcodes"], new["opcodes"]),
    }
    diagnostics = {
        "old": old["diagnostics"],
        "new": new["diagnostics"],
    }
    classification = _classify(changed_callbacks, changes, diagnostics)
    report = {
        "schema_version": STRATEGY_DIFF_VERSION,
        "selected_class": new["class_name"],
        "old": old["source"],
        "new": new["source"],
        "classification": classification,
        "changes": changes,
        "diagnostics": diagnostics,
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _inventory(path: Path, *, class_name: str | None) -> dict[str, Any]:
    analysis = analyze_strategy(path, class_name=class_name)
    strategies = analysis.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise StrategyAnalysisError(
            f"strategy diff requires exactly one selected strategy class: {path}"
        )
    strategy = strategies[0]
    if not isinstance(strategy, Mapping):
        raise StrategyAnalysisError(f"strategy inventory is invalid: {path}")
    selected_class = str(strategy["name"])
    tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == selected_class
        ),
        None,
    )
    if class_node is None:
        raise StrategyAnalysisError(
            f"selected strategy class disappeared during diff: {selected_class}"
        )
    callbacks: dict[str, dict[str, Any]] = {}
    tags: set[str] = set()
    signals: set[str] = set()
    columns: set[str] = set()
    state_keys: set[str] = set()
    grind_levels: set[int] = set()
    opcodes: set[str] = set()
    method_records = strategy.get("methods")
    records = method_records if isinstance(method_records, list) else []
    record_by_name = {
        str(item["name"]): item
        for item in records
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    for method in class_node.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        record = record_by_name.get(method.name)
        if record is not None:
            callbacks[method.name] = {
                "source_sha256": record["source_sha256"],
                "location": record["location"],
            }
        method_tags = _method_tags(method)
        tags.update(method_tags)
        if method.name in _VECTOR_METHODS:
            signals.update(
                token
                for tag in method_tags
                if _SIGNAL_TAG.fullmatch(tag)
                for token in tag.split()
            )
        columns.update(_dataframe_columns(method))
        state_keys.update(_custom_state_keys(method))
        grind_levels.update(
            int(match.group(1))
            for tag in method_tags
            for match in _GRIND_LEVEL.finditer(tag)
        )
        if method.name in STRATEGY_CALLBACKS:
            opcodes.update(_method_opcodes(method))
    return {
        "source": {
            "name": path.name,
            "bytes": analysis["source"]["bytes"],
            "sha256": analysis["source"]["sha256"],
        },
        "class_name": selected_class,
        "callbacks": callbacks,
        "signals": signals,
        "tags": tags,
        "columns": columns,
        "state_keys": state_keys,
        "grind_levels": grind_levels,
        "opcodes": opcodes,
        "diagnostics": analysis["diagnostics"],
    }


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
                value
                for value in _string_literals(node.value)
                if _looks_like_action_tag(value)
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


def _changed_callbacks(
    old: Mapping[str, Mapping[str, Any]],
    new: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return sorted(
        name
        for name in set(old) & set(new)
        if old[name].get("source_sha256") != new[name].get("source_sha256")
    )


def _set_change(old: set[Any], new: set[Any]) -> dict[str, list[Any]]:
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
    }


def _classify(
    changed_callbacks: list[str],
    changes: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> str:
    if any(
        item.get("severity") == "error"
        for side in ("old", "new")
        for item in diagnostics[side]
        if isinstance(item, Mapping)
    ):
        return "stateful-review"
    changed = set(changed_callbacks)
    if changed and changed <= _VECTOR_METHODS:
        return "vector-only"
    state_change = changes["custom_state_keys"]
    grind_change = changes["grind_levels"]
    if (
        changed & _STATEFUL_CALLBACKS
        or state_change["added"]
        or state_change["removed"]
        or grind_change["added"]
        or grind_change["removed"]
    ):
        return "stateful-review"
    return "ir-compatible"
