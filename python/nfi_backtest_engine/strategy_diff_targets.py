"""Behavior target fanout for deterministic strategy differences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .strategy import STRATEGY_CALLBACKS
from .strategy_diff_features import _SIGNAL_TAG, _changed_source_spans
from .strategy_diff_reachability import (
    _feature_methods,
    _method_tags_for,
    _reachable_methods,
)

_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}
_RUNTIME_METHODS = STRATEGY_CALLBACKS | _VECTOR_METHODS


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
            if direction == "changed":
                for tag in tags:
                    targets.append(
                        _target(
                            kind="signal" if _SIGNAL_TAG.fullmatch(tag) else "tag",
                            change="changed",
                            value=tag,
                            methods=[name],
                            tags=[tag],
                            old_inventory=old_inventory,
                            new_inventory=new_inventory,
                        )
                    )
    unique = {target["id"]: target for target in targets}
    return [unique[target_id] for target_id in sorted(unique)]


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
    semantic_callers = _semantic_callers(new_inventory, methods)
    identity = {
        "kind": kind,
        "change": change,
        "value": value,
        "methods": methods,
        "semantic_callers": semantic_callers,
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
            "changed_source_spans": _changed_source_spans(
                old_inventory,
                new_inventory,
                methods,
            ),
        },
        "runtime_observable": bool(
            kind in {"signal", "tag", "grind_level"}
            or tags
            or (kind == "callback" and str(value) in STRATEGY_CALLBACKS)
        ),
    }


def _semantic_callers(
    inventory: Mapping[str, Any],
    methods: list[str],
) -> list[str]:
    method_features = inventory.get("method_features")
    if not isinstance(method_features, Mapping):
        return []
    selected = set(methods)
    return sorted(
        method
        for method in _RUNTIME_METHODS
        if method in method_features
        and selected & _reachable_methods(method_features, [method])
    )


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


