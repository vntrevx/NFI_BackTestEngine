"""Deterministic AST/IR-oriented differences between strategy revisions."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import StrategyAnalysisError
from .strategy import STRATEGY_CALLBACKS
from .strategy_ir import analyze_strategy

STRATEGY_DIFF_VERSION = "1.3.0"
_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}
_RUNTIME_METHODS = STRATEGY_CALLBACKS | _VECTOR_METHODS
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
_ROUTE_SELECTOR = re.compile(
    r"(?i)(?:signal|tag|route|grind|derisk|"
    r"condition.*(?:index|id)|(?:index|id).*condition)"
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
        "boolean_mappings": _boolean_mapping_changes(
            old["boolean_mappings"],
            new["boolean_mappings"],
        ),
    }
    diagnostics = {
        "old": old["diagnostics"],
        "new": new["diagnostics"],
    }
    classification = _classify(changed_callbacks, changes, diagnostics)
    behavior_targets = _behavior_targets(
        changes,
        old_inventory=old,
        new_inventory=new,
    )
    report = {
        "schema_version": STRATEGY_DIFF_VERSION,
        "selected_class": new["class_name"],
        "old": old["source"],
        "new": new["source"],
        "classification": classification,
        "changes": changes,
        "behavior_targets": behavior_targets,
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
        method_signals.update(
            route for route in method_routes if _SIGNAL_TAG.fullmatch(route)
        )
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


def _behavior_targets(
    changes: Mapping[str, Any],
    *,
    old_inventory: Mapping[str, Any],
    new_inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    field_kinds = {
        "signals": "signal",
        "tags": "tag",
        "dataframe_columns": "dataframe_column",
        "custom_state_keys": "custom_state_key",
        "grind_levels": "grind_level",
        "opcodes": "opcode",
    }
    for field, kind in field_kinds.items():
        change = changes[field]
        for direction, inventory in (
            ("added", new_inventory),
            ("removed", old_inventory),
        ):
            for value in change[direction]:
                methods = _feature_methods(inventory, field, value)
                targets.append(
                    _target(
                        kind=kind,
                        change=direction,
                        value=value,
                        methods=methods,
                        tags=_method_tags_for(inventory, methods),
                        old_inventory=old_inventory,
                        new_inventory=new_inventory,
                    )
                )

    callbacks = changes["callbacks"]
    for direction, inventory in (
        ("added", new_inventory),
        ("removed", old_inventory),
        ("changed", new_inventory),
    ):
        for name in callbacks[direction]:
            tags = _changed_method_tags(
                name,
                direction=direction,
                old_inventory=old_inventory,
                new_inventory=new_inventory,
            )
            if not _behavior_method_relevant(inventory, name, tags):
                continue
            targets.append(
                _target(
                    kind="callback",
                    change=direction,
                    value=name,
                    methods=[name],
                    tags=tags,
                    old_inventory=old_inventory,
                    new_inventory=new_inventory,
                )
            )
    return sorted(targets, key=lambda item: item["id"])


def _feature_methods(
    inventory: Mapping[str, Any],
    field: str,
    value: Any,
) -> list[str]:
    feature_name = {
        "dataframe_columns": "columns",
        "custom_state_keys": "state_keys",
    }.get(field, field)
    method_features = inventory.get("method_features")
    if not isinstance(method_features, Mapping):
        return []
    return sorted(
        str(method)
        for method, features in method_features.items()
        if isinstance(features, Mapping) and value in features.get(feature_name, set())
    )


def _method_tags_for(
    inventory: Mapping[str, Any],
    methods: list[str],
) -> list[str]:
    method_features = inventory.get("method_features")
    if not isinstance(method_features, Mapping):
        return []
    reachable = _reachable_methods(method_features, methods)
    runtime_callers = {
        str(method)
        for method in _RUNTIME_METHODS
        if method in method_features
        and set(methods) & _reachable_methods(method_features, [method])
    }
    reachable.update(_reachable_methods(method_features, sorted(runtime_callers)))
    tags: set[str] = set()
    for method in reachable:
        features = method_features.get(method)
        if not isinstance(features, Mapping):
            continue
        tags.update(str(tag) for tag in features.get("tags", set()))
        routes = features.get("routes")
        if isinstance(routes, Mapping):
            tags.update(str(route) for route in routes)
    return sorted(tags)


def _reachable_methods(
    method_features: Mapping[str, Any],
    roots: list[str],
) -> set[str]:
    pending = list(roots)
    reached: set[str] = set()
    while pending:
        method = pending.pop()
        if method in reached:
            continue
        features = method_features.get(method)
        if not isinstance(features, Mapping):
            continue
        reached.add(method)
        pending.extend(
            str(called) for called in features.get("calls", set()) if str(called) not in reached
        )
    return reached


def _changed_method_tags(
    name: str,
    *,
    direction: str,
    old_inventory: Mapping[str, Any],
    new_inventory: Mapping[str, Any],
) -> list[str]:
    if direction == "added":
        return _method_tags_for(new_inventory, [name])
    if direction == "removed":
        return _method_tags_for(old_inventory, [name])
    old_routes = _method_routes_for(old_inventory, name)
    new_routes = _method_routes_for(new_inventory, name)
    changed_routes = sorted(
        route
        for route in set(old_routes) | set(new_routes)
        if old_routes.get(route) != new_routes.get(route)
    )
    return changed_routes or _method_tags_for(new_inventory, [name])


def _method_routes_for(
    inventory: Mapping[str, Any],
    method: str,
) -> dict[str, str]:
    method_features = inventory.get("method_features")
    if not isinstance(method_features, Mapping):
        return {}
    features = method_features.get(method)
    if not isinstance(features, Mapping):
        return {}
    routes = features.get("routes")
    if not isinstance(routes, Mapping):
        return {}
    return {str(route): str(fingerprint) for route, fingerprint in routes.items()}


def _behavior_method_relevant(
    inventory: Mapping[str, Any],
    method: str,
    tags: list[str],
) -> bool:
    if method in _RUNTIME_METHODS or tags:
        return True
    method_features = inventory.get("method_features")
    if not isinstance(method_features, Mapping):
        return False
    return any(
        method in _reachable_methods(method_features, [runtime_method])
        for runtime_method in _RUNTIME_METHODS
        if runtime_method in method_features
    )


def _target(
    *,
    kind: str,
    change: str,
    value: Any,
    methods: list[str],
    tags: list[str],
    old_inventory: Mapping[str, Any],
    new_inventory: Mapping[str, Any],
) -> dict[str, Any]:
    identity = {
        "kind": kind,
        "change": change,
        "value": value,
        "methods": methods,
        "tags": tags,
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "id": hashlib.sha256(payload).hexdigest(),
        **identity,
        "proof": {
            "mode": {
                "added": "presence",
                "removed": "absence",
                "changed": "transition",
            }[change],
            "old_source_spans": _source_spans(old_inventory, methods),
            "new_source_spans": _source_spans(new_inventory, methods),
        },
        "runtime_observable": bool(
            kind in {"signal", "tag", "grind_level"}
            or tags
            or (kind == "callback" and str(value) in STRATEGY_CALLBACKS)
        ),
    }


def _source_spans(
    inventory: Mapping[str, Any],
    methods: list[str],
) -> list[dict[str, Any]]:
    callbacks = inventory.get("callbacks")
    if not isinstance(callbacks, Mapping):
        return []
    return [
        {
            "method": method,
            **dict(record["location"]),
        }
        for method in methods
        if isinstance((record := callbacks.get(method)), Mapping)
        and isinstance(record.get("location"), Mapping)
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
