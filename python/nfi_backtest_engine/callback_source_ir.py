"""Compile callback routing metadata from strategy source without executing Python."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError
from .specs import CALLBACK_SOURCE_IR_SCHEMA, validate_schema
from .strategy import STRATEGY_CALLBACKS
from .strategy_ir import analyze_strategy

CALLBACK_SOURCE_IR_VERSION = "callback-source-ir-v1"

_CANDLE_INPUTS = {
    "current_time",
    "current_rate",
    "current_profit",
    "current_entry_rate",
    "current_exit_rate",
    "current_entry_profit",
    "current_exit_profit",
}
_WALLET_INPUTS = {"min_stake", "max_stake", "proposed_stake"}
_TAG_NAME_PARTS = ("tag", "route")


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

    entrypoints: list[dict[str, Any]] = []
    edge_records: list[dict[str, Any]] = []
    tag_records: list[dict[str, Any]] = []
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
                    {"source": read_source, "key": key}
                    for read_source, key in reads
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
    document: dict[str, Any] = {
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


def _route_constants(constants: Any) -> dict[str, list[str]]:
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


def _ordered_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _ordered_strings(key)
            yield from _ordered_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            yield from _ordered_strings(item)


def _constant_locations(
    class_node: ast.ClassDef,
    path_name: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
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
) -> tuple[list[str], list[dict[str, Any]]]:
    ordered: list[str] = []
    edges: list[dict[str, Any]] = []
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


def _tag_emissions(
    entrypoint: str,
    closure: Sequence[str],
    methods: Mapping[str, ast.AST],
    path_name: str,
) -> list[dict[str, Any]]:
    roles = {
        "adjust_trade_position": "order_tag",
        "custom_exit": "exit_reason",
    }
    role = roles.get(entrypoint)
    if role is None:
        return []
    result: list[dict[str, Any]] = []
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
        if isinstance(item, ast.Return) and item.value is not None:
            values = item.value.elts if isinstance(item.value, ast.Tuple) else [item.value]
            candidate = values[1] if len(values) == 2 else values[0]
            yield candidate, item
            continue
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(item, ast.Assign):
            target = item.targets[0] if len(item.targets) == 1 else None
            value = item.value
        elif isinstance(item, ast.AnnAssign):
            target = item.target
            value = item.value
        else:  # pragma: no cover - constrained by _ordered_nodes
            continue
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


def _required_data(
    closure: Sequence[str],
    methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> tuple[list[tuple[str, str]], list[str]]:
    reads: list[tuple[str, str]] = []
    columns: list[str] = []
    for method_name in closure:
        method = methods[method_name]
        candle_aliases = _candle_aliases(method)
        called_attributes = {
            id(call.func)
            for call in _ordered_nodes(method, ast.Call)
            if isinstance(call.func, ast.Attribute)
        }
        for node in _ordered_nodes(method, ast.Attribute):
            if (
                id(node) not in called_attributes
                and isinstance(node.value, ast.Name)
                and node.value.id in {"trade", "order"}
            ):
                _append_unique(reads, (node.value.id, node.attr))
        for node in _ordered_nodes(method, ast.Name):
            if node.id in _CANDLE_INPUTS:
                _append_unique(reads, ("candle", node.id))
            elif node.id in _WALLET_INPUTS:
                _append_unique(reads, ("wallet", node.id))
        for call in _ordered_nodes(method, ast.Call):
            leaf = _call_leaf(call)
            if leaf == "get_custom_data" and call.args and _literal_string(call.args[0]):
                _append_unique(reads, ("custom_state", str(ast.literal_eval(call.args[0]))))
            elif leaf == "select_filled_orders":
                _append_unique(reads, ("orders", "filled"))
        for subscript in _ordered_nodes(method, ast.Subscript):
            if not _literal_string(subscript.slice):
                continue
            root = _root_name(subscript.value)
            if root in candle_aliases or (root is not None and "candle" in root.lower()):
                _append_unique(columns, str(ast.literal_eval(subscript.slice)))
    return reads, columns


def _candle_aliases(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    aliases = {
        argument.arg
        for argument in (*node.args.args, *node.args.kwonlyargs)
        if "candle" in argument.arg.lower()
    }
    for assignment in _ordered_nodes(node, (ast.Assign, ast.AnnAssign)):
        target: ast.AST | None
        value: ast.AST | None
        if isinstance(assignment, ast.Assign):
            target = assignment.targets[0] if len(assignment.targets) == 1 else None
            value = assignment.value
        else:
            target = assignment.target
            value = assignment.value
        if isinstance(target, ast.Name) and value is not None and ".iloc[" in ast.unparse(value):
            aliases.add(target.id)
    return aliases


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [
        argument.arg
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if argument.arg != "self"
    ]


def _ordered_nodes(node: ast.AST, kinds: type[ast.AST] | tuple[type[ast.AST], ...]) -> list[Any]:
    return sorted(
        (item for item in ast.walk(node) if isinstance(item, kinds)),
        key=lambda item: (
            getattr(item, "lineno", 0),
            getattr(item, "col_offset", 0),
            getattr(item, "end_lineno", 0),
            getattr(item, "end_col_offset", 0),
        ),
    )


def _call_leaf(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _literal_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _location(node: ast.AST, path_name: str) -> dict[str, Any]:
    return {
        "path": path_name,
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _fingerprint(document: Mapping[str, Any]) -> str:
    identity = {
        key: _without_diagnostic_locations(value)
        for key, value in document.items()
        if key not in {"fingerprint", "source"}
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _without_diagnostic_locations(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_diagnostic_locations(item)
            for key, item in value.items()
            if key != "location"
        }
    if isinstance(value, list):
        return [_without_diagnostic_locations(item) for item in value]
    return value
