"""Source inventory used by deterministic strategy differences."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError
from .strategy import STRATEGY_CALLBACKS
from .strategy_diff_features import (
    _GRIND_LEVEL,
    _SIGNAL_TAG,
    _custom_state_keys,
    _dataframe_columns,
    _method_calls,
    _method_opcodes,
    _method_routes,
    _method_tags,
)
from .strategy_diff_reachability import _method_tags_for
from .strategy_ir import analyze_strategy

_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}


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
    method_features: dict[str, dict[str, Any]] = {}
    method_records = strategy.get("methods")
    records = method_records if isinstance(method_records, list) else []
    record_by_name = {
        str(item["name"]): item
        for item in records
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    methods = [
        method
        for method in class_node.body
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    method_names = {method.name for method in methods}
    for method in methods:
        record = record_by_name.get(method.name)
        if record is not None:
            callbacks[method.name] = {
                "source_sha256": record["source_sha256"],
                "location": record["location"],
            }
        method_tags = _method_tags(method)
        method_state_keys = _custom_state_keys(method)
        method_grind_levels = {
            int(match.group(1)) for tag in method_tags for match in _GRIND_LEVEL.finditer(tag)
        }
        method_opcodes = _method_opcodes(method) if method.name in STRATEGY_CALLBACKS else set()
        method_columns = _dataframe_columns(method)
        method_routes = _method_routes(method)
        method_signals = {
            token for tag in method_tags if _SIGNAL_TAG.fullmatch(tag) for token in tag.split()
        }
        method_signals.update(route for route in method_routes if _SIGNAL_TAG.fullmatch(route))
        method_features[method.name] = {
            "signals": method_signals,
            "tags": method_tags,
            "columns": method_columns,
            "state_keys": method_state_keys,
            "grind_levels": method_grind_levels,
            "opcodes": method_opcodes,
            "calls": _method_calls(method, method_names),
            "routes": method_routes,
        }
        tags.update(method_tags)
        columns.update(method_columns)
        state_keys.update(method_state_keys)
        grind_levels.update(method_grind_levels)
        opcodes.update(method_opcodes)
    for method in sorted(_VECTOR_METHODS & method_names):
        signals.update(
            tag
            for tag in _method_tags_for(
                {"method_features": method_features},
                [method],
            )
            if _SIGNAL_TAG.fullmatch(tag)
        )
    return {
        "path": path,
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
        "boolean_mappings": _boolean_mappings(strategy.get("constants")),
        "method_features": method_features,
        "diagnostics": analysis["diagnostics"],
    }


def _boolean_mappings(constants: Any) -> dict[str, dict[str, bool]]:
    if not isinstance(constants, Mapping):
        return {}
    return {
        str(mapping): {
            str(key): value
            for key, value in values.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        for mapping, values in constants.items()
        if isinstance(mapping, str)
        and isinstance(values, Mapping)
        and any(isinstance(value, bool) for value in values.values())
    }


def _boolean_mapping_changes(
    old: Mapping[str, Mapping[str, bool]],
    new: Mapping[str, Mapping[str, bool]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for mapping in sorted(set(old) & set(new)):
        old_values = old[mapping]
        new_values = new[mapping]
        for key in sorted(set(old_values) & set(new_values)):
            before = old_values[key]
            after = new_values[key]
            if before != after:
                changes.append(
                    {
                        "mapping": mapping,
                        "key": key,
                        "old": before,
                        "new": after,
                    }
                )
    return changes
