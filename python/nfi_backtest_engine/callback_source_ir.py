"""Compile callback routing metadata from strategy source without executing Python."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from .callback_contract import JsonObject
from .callback_source_identity import _append_unique, _fingerprint, _location
from .callback_source_reads import _parameters, _required_data
from .callback_source_routes import (
    _constant_locations,
    _reachable_methods,
    _route_constants,
    _used_route_keys,
)
from .callback_source_tags import _tag_emissions
from .errors import StrategyAnalysisError
from .specs import CALLBACK_SOURCE_IR_SCHEMA, validate_schema
from .strategy import STRATEGY_CALLBACKS
from .strategy_ir import analyze_strategy

CALLBACK_SOURCE_IR_VERSION = "callback-source-ir-v1"


def compile_callback_source_ir(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str = "all",
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe callback routes, tags, reads, and source order from one strategy.

    This IR is intentionally descriptive.  Executable state-machine opcodes are
    compiled separately, so learning a new tag or route cannot silently widen
    Native behavior.
    """
    if trading_mode not in {"all", "spot", "futures"}:
        raise StrategyAnalysisError("callback source IR mode must be all, spot, or futures")
    path = Path(source).resolve()
    analysis = analysis or analyze_strategy(path, class_name=class_name)
    _require_static_strategy(analysis)
    strategy = analysis["strategies"][0]
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != analysis["source"]["sha256"]:
        raise StrategyAnalysisError("callback source changed after static analysis")
    try:
        tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - analyzed above
        raise StrategyAnalysisError("callback source no longer parses") from exc
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if class_node is None:  # pragma: no cover - analyze_strategy selected it
        raise StrategyAnalysisError("selected strategy class disappeared")

    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    callback_nodes = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in STRATEGY_CALLBACKS
    ]
    route_constants = _route_constants(strategy.get("constants", {}))
    constant_locations = _constant_locations(class_node, path.name)

    entrypoints: list[JsonObject] = []
    edge_records: list[JsonObject] = []
    tag_records: list[JsonObject] = []
    route_consumers: dict[str, list[str]] = {key: [] for key in route_constants}
    read_consumers: dict[tuple[str, str], list[str]] = {}
    column_consumers: dict[str, list[str]] = {}

    for source_order, callback in enumerate(callback_nodes):
        active = not (trading_mode == "spot" and callback.name == "leverage")
        closure, edges = _reachable_methods(callback.name, methods, path.name)
        route_keys = _used_route_keys(closure, methods, route_constants)
        emissions = _tag_emissions(callback.name, closure, methods, path.name)
        reads, columns = _required_data(closure, methods)
        for key in route_keys:
            _append_unique(route_consumers[key], callback.name)
        for read in reads:
            _append_unique(read_consumers.setdefault(read, []), callback.name)
        for column in columns:
            _append_unique(column_consumers.setdefault(column, []), callback.name)
        emitted_ids = []
        for emission in emissions:
            identifier = f"t{len(tag_records) + 1}"
            tag_records.append({"id": identifier, **emission})
            emitted_ids.append(identifier)
        for edge_order, edge in enumerate(edges):
            edge_records.append(
                {
                    "entrypoint": callback.name,
                    "source_order": edge_order,
                    **edge,
                }
            )
        entrypoints.append(
            {
                "name": callback.name,
                "source_order": source_order,
                "active_for_mode": active,
                "parameters": _parameters(callback),
                "reachable_methods": closure,
                "route_keys": route_keys,
                "emitted_tag_ids": emitted_ids,
                "required_reads": [
                    {"source": read_source, "key": key} for read_source, key in reads
                ],
                "required_columns": columns,
                "location": _location(callback, path.name),
            }
        )

    route_records = [
        {
            "key": key,
            "values": values,
            "entrypoints": route_consumers[key],
            "location": constant_locations.get(key, _location(class_node, path.name)),
        }
        for key, values in route_constants.items()
        if route_consumers[key]
    ]
    document: JsonObject = {
        "schema_version": CALLBACK_SOURCE_IR_VERSION,
        "source": {
            "path": str(path),
            "sha256": analysis["source"]["sha256"],
        },
        "selected_class": strategy["name"],
        "trading_mode": trading_mode,
        "entrypoints": entrypoints,
        "call_edges": edge_records,
        "route_keys": route_records,
        "emitted_tags": tag_records,
        "required_reads": [
            {
                "source": source_name,
                "key": key,
                "entrypoints": consumers,
            }
            for (source_name, key), consumers in sorted(read_consumers.items())
        ],
        "required_columns": [
            {"name": name, "entrypoints": consumers}
            for name, consumers in sorted(column_consumers.items())
        ],
    }
    document["fingerprint"] = _fingerprint(document)
    validate_schema(document, CALLBACK_SOURCE_IR_SCHEMA)
    return document


def _require_static_strategy(analysis: dict[str, Any]) -> None:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise StrategyAnalysisError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise StrategyAnalysisError("callback source IR requires one selected strategy")
