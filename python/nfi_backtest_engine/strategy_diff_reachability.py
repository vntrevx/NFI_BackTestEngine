"""Transitive method reachability for deterministic strategy differences."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .strategy import STRATEGY_CALLBACKS

_VECTOR_METHODS = {
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
}
_RUNTIME_METHODS = STRATEGY_CALLBACKS | _VECTOR_METHODS

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


