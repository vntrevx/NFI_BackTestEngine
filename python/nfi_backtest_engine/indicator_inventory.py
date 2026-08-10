"""Static, deterministic inventory of strategy indicator operations."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import StrategyAnalysisError
from .specs import INDICATOR_INVENTORY_SCHEMA, validate_schema
from .strategy_ir import analyze_strategy

INDICATOR_INVENTORY_VERSION = "indicator-operation-inventory-v1"

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda

_ROOT_ROLES = {
    "populate_indicators": "indicator-program",
    "informative_pairs": "informative-dependency-registration",
}

_REQUIRED_COVERAGE_FAMILIES = (
    "talib",
    "qtpylib",
    "pandas",
    "rolling",
    "informative",
)

_PANDAS_METHODS = {
    "agg",
    "apply",
    "astype",
    "bfill",
    "clip",
    "cummax",
    "cummin",
    "cumprod",
    "cumsum",
    "diff",
    "drop",
    "dropna",
    "eq",
    "expanding",
    "ffill",
    "fillna",
    "floor",
    "groupby",
    "interpolate",
    "isna",
    "map",
    "max",
    "mean",
    "min",
    "notna",
    "pct_change",
    "quantile",
    "rank",
    "resample",
    "shift",
    "sort_values",
    "std",
    "sum",
    "tail",
    "to_numpy",
    "to_string",
    "transform",
    "where",
}

_ROLLING_METHODS = {
    "ewm",
    "expanding",
    "rolling",
}

_PYTHON_BUILTINS = {
    "RuntimeError",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "range",
    "reversed",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
}

_PYTHON_CONTAINER_METHODS = {
    "add",
    "append",
    "discard",
    "extend",
    "get",
    "items",
    "keys",
    "pop",
    "setdefault",
    "update",
    "values",
}

_PYTHON_STRING_METHODS = {
    "endswith",
    "partition",
    "removeprefix",
    "removesuffix",
    "replace",
    "split",
    "startswith",
    "strip",
}

_LOOKBACK_KEYWORDS = {
    "alpha",
    "com",
    "fastd_period",
    "fastk_period",
    "fastperiod",
    "halflife",
    "min_periods",
    "period",
    "periods",
    "signalperiod",
    "slowperiod",
    "span",
    "timeperiod",
    "window",
}


@dataclass(frozen=True)
class _FunctionRecord:
    identifier: str
    name: str
    kind: str
    node: _FunctionNode
    parameters: tuple[str, ...]
    source_sha256: str


def build_indicator_inventory(
    source: str | Path,
    *,
    class_name: str | None = None,
    upstream_repository: str | None = None,
    upstream_commit: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inventory the indicator lane without importing or executing strategy code."""
    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    strategy = _selected_strategy(analysis)
    source_bytes = path.read_bytes()
    try:
        text = source_bytes.decode("utf-8")
        tree = ast.parse(text, filename=str(path), type_comments=True)
    except (UnicodeDecodeError, SyntaxError) as exc:  # pragma: no cover - analyze_strategy owns it
        raise StrategyAnalysisError(f"cannot parse indicator source: {path}") from exc

    selected_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if selected_class is None:  # pragma: no cover - guarded by analyze_strategy
        raise StrategyAnalysisError("selected strategy class disappeared during inventory")

    functions, lambda_targets = _function_records(selected_class, tree, text)
    aliases = {
        identifier: _callable_aliases(record.node, lambda_targets.get(identifier, {}))
        for identifier, record in functions.items()
    }
    preliminary = _preliminary_calls(functions, aliases, selected_class.name)
    incoming_bindings = _incoming_callable_bindings(preliminary, functions, aliases)
    call_sites, edges = _classify_call_sites(
        preliminary,
        functions,
        aliases,
        incoming_bindings,
    )
    roots = _roots(functions, selected_class.name)
    reachable = _reachable_nodes(roots, edges)
    reachable_sites = [item for item in call_sites if item["caller"] in reachable]
    reachable_edges = [item for item in edges if item["caller"] in reachable]

    nodes = _call_graph_nodes(functions, roots, reachable)
    operations = _operation_inventory(reachable_sites, functions, reachable)
    dataframe_access = _dataframe_access_inventory(functions, reachable)
    informative = _informative_dependencies(strategy, reachable_sites, roots)
    coverage = _coverage_matrix(operations)
    summary = _summary(nodes, reachable_edges, reachable_sites, operations, informative)

    report: dict[str, Any] = {
        "schema_version": INDICATOR_INVENTORY_VERSION,
        "source": {
            "path": str(path),
            "bytes": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "upstream": {
            "repository": upstream_repository,
            "commit": upstream_commit,
        },
        "selected_class": selected_class.name,
        "roots": roots,
        "required_timeframes": list(strategy.get("required_timeframes", [])),
        "call_graph": {
            "nodes": nodes,
            "edges": reachable_edges,
        },
        "call_sites": reachable_sites,
        "operations": operations,
        "coverage_matrix": coverage,
        "dataframe_access": dataframe_access,
        "informative_dependencies": informative,
        "summary": summary,
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, INDICATOR_INVENTORY_SCHEMA)
    if output_path is not None:
        write_json(output_path, report)
    return report


def _selected_strategy(analysis: dict[str, Any]) -> dict[str, Any]:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise StrategyAnalysisError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise StrategyAnalysisError("indicator inventory requires exactly one selected strategy")
    return analysis["strategies"][0]


def _function_records(
    selected_class: ast.ClassDef,
    tree: ast.Module,
    text: str,
) -> tuple[dict[str, _FunctionRecord], dict[str, dict[str, str]]]:
    records: dict[str, _FunctionRecord] = {}
    for node in selected_class.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            identifier = f"class:{selected_class.name}.{node.name}"
            records[identifier] = _function_record(
                identifier,
                "strategy-method",
                node,
                text,
                name=node.name,
            )
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            identifier = f"module:{node.name}"
            records[identifier] = _function_record(
                identifier,
                "module-helper",
                node,
                text,
                name=node.name,
            )

    lambda_targets: dict[str, dict[str, str]] = defaultdict(dict)
    parents = list(records.items())
    for parent_identifier, parent in parents:
        for item in _walk_body(parent.node):
            if not isinstance(item, ast.Assign) or len(item.targets) != 1:
                continue
            target = item.targets[0]
            if not isinstance(target, ast.Name) or not isinstance(item.value, ast.Lambda):
                continue
            lambda_node = item.value
            identifier = (
                f"lambda:{parent_identifier}.{target.id}"
                f"@{lambda_node.lineno}:{lambda_node.col_offset}"
            )
            records[identifier] = _function_record(
                identifier,
                "lambda-helper",
                lambda_node,
                text,
                name=target.id,
            )
            lambda_targets[parent_identifier][target.id] = identifier
    return records, lambda_targets


def _function_record(
    identifier: str,
    kind: str,
    node: _FunctionNode,
    text: str,
    *,
    name: str,
) -> _FunctionRecord:
    segment = ast.get_source_segment(text, node) or ast.unparse(node)
    normalized = segment.replace("\r\n", "\n").replace("\r", "\n")
    parameters = tuple(
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    )
    return _FunctionRecord(
        identifier=identifier,
        name=name,
        kind=kind,
        node=node,
        parameters=parameters,
        source_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
    )


def _callable_aliases(
    node: _FunctionNode,
    lambda_targets: dict[str, str],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for item in _walk_body(node):
        value: ast.expr | None = None
        target: ast.expr | None = None
        if isinstance(item, ast.Assign) and len(item.targets) == 1:
            target, value = item.targets[0], item.value
        elif isinstance(item, ast.AnnAssign) and item.value is not None:
            target, value = item.target, item.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, ast.Lambda) and target.id in lambda_targets:
            aliases[target.id] = f"@local:{lambda_targets[target.id]}"
            continue
        name = _qualified_name(value)
        if name is not None:
            aliases[target.id] = _resolve_alias(name, aliases)
    return aliases


def _preliminary_calls(
    functions: dict[str, _FunctionRecord],
    aliases: dict[str, dict[str, str]],
    class_name: str,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for identifier, record in functions.items():
        for node in _walk_body(record.node):
            if not isinstance(node, ast.Call):
                continue
            raw = _callable_text(node.func)
            resolved = _resolve_alias(raw, aliases[identifier])
            local = _local_target(resolved, functions, class_name)
            calls.append(
                {
                    "id": _call_site_id(identifier, node),
                    "caller": identifier,
                    "node": node,
                    "raw": raw,
                    "resolved": resolved,
                    "local_target": local,
                }
            )
    return calls


def _incoming_callable_bindings(
    preliminary: list[dict[str, Any]],
    functions: dict[str, _FunctionRecord],
    aliases: dict[str, dict[str, str]],
) -> dict[str, dict[str, set[str]]]:
    bindings: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for item in preliminary:
        target = item["local_target"]
        if target is None:
            continue
        node: ast.Call = item["node"]
        parameters = functions[target].parameters
        caller_aliases = aliases[item["caller"]]
        for index, argument in enumerate(node.args):
            if index >= len(parameters):
                break
            value = _qualified_name(argument)
            if value is not None:
                bindings[target][parameters[index]].add(_resolve_alias(value, caller_aliases))
        for keyword in node.keywords:
            if keyword.arg not in parameters:
                continue
            value = _qualified_name(keyword.value)
            if value is not None:
                bindings[target][keyword.arg].add(_resolve_alias(value, caller_aliases))
    return bindings


def _classify_call_sites(
    preliminary: list[dict[str, Any]],
    functions: dict[str, _FunctionRecord],
    aliases: dict[str, dict[str, str]],
    incoming_bindings: dict[str, dict[str, set[str]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    call_sites: list[dict[str, Any]] = []
    edge_locations: dict[tuple[str, str], list[dict[str, int]]] = defaultdict(list)
    for item in preliminary:
        node: ast.Call = item["node"]
        local = item["local_target"]
        caller = item["caller"]
        targets: list[dict[str, str]] = []
        resolution = "external"
        if local is not None:
            resolution = "local-helper"
            targets.append({"kind": "local", "name": local})
            edge_locations[(caller, local)].append(_location(node))
        else:
            resolved_values = {item["resolved"]}
            raw_name = item["raw"]
            parameter_values = incoming_bindings.get(caller, {}).get(raw_name, set())
            if parameter_values:
                resolved_values = parameter_values
                resolution = "bound-callable-parameter"
            for value in sorted(resolved_values):
                family, canonical, native_requirement = _classify_external(value, node.func)
                targets.append(
                    {
                        "kind": "external",
                        "name": canonical,
                        "family": family,
                        "native_requirement": native_requirement,
                    }
                )
            if any(target["family"] == "unresolved" for target in targets):
                resolution = "unresolved"
        call_sites.append(
            {
                "id": item["id"],
                "caller": caller,
                "location": _location(node),
                "expression": _expression_text(node.func),
                "resolution": resolution,
                "targets": targets,
                "arguments": _argument_records(node),
            }
        )
    edges = [
        {
            "caller": caller,
            "callee": callee,
            "locations": sorted(locations, key=_location_key),
        }
        for (caller, callee), locations in sorted(edge_locations.items())
    ]
    call_sites.sort(key=lambda item: (item["caller"], _location_key(item["location"])))
    return call_sites, edges


def _roots(functions: dict[str, _FunctionRecord], class_name: str) -> list[dict[str, str]]:
    roots = []
    for name, role in _ROOT_ROLES.items():
        identifier = f"class:{class_name}.{name}"
        if identifier in functions:
            roots.append({"node": identifier, "role": role})
    if not any(item["role"] == "indicator-program" for item in roots):
        raise StrategyAnalysisError("strategy does not define populate_indicators")
    return roots


def _reachable_nodes(roots: list[dict[str, str]], edges: list[dict[str, Any]]) -> set[str]:
    children: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        children[edge["caller"]].add(edge["callee"])
    reachable: set[str] = set()
    queue = deque(item["node"] for item in roots)
    while queue:
        identifier = queue.popleft()
        if identifier in reachable:
            continue
        reachable.add(identifier)
        queue.extend(sorted(children.get(identifier, set())))
    return reachable


def _call_graph_nodes(
    functions: dict[str, _FunctionRecord],
    roots: list[dict[str, str]],
    reachable: set[str],
) -> list[dict[str, Any]]:
    roles = {item["node"]: item["role"] for item in roots}
    return [
        {
            "id": identifier,
            "name": functions[identifier].name,
            "kind": functions[identifier].kind,
            "root_role": roles.get(identifier),
            "location": _location(functions[identifier].node),
            "parameters": list(functions[identifier].parameters),
            "source_sha256": functions[identifier].source_sha256,
        }
        for identifier in sorted(reachable)
    ]


def _operation_inventory(
    call_sites: list[dict[str, Any]],
    functions: dict[str, _FunctionRecord],
    reachable: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for site in call_sites:
        for target in site["targets"]:
            if target["kind"] != "external":
                continue
            key = (target["family"], target["name"], target["native_requirement"])
            grouped[key].append(
                {
                    "call_site": site["id"],
                    "caller": site["caller"],
                    "location": site["location"],
                    "arguments": site["arguments"],
                    "lookback": _lookback_contract(target["name"], site["arguments"]),
                }
            )
    for identifier in sorted(reachable):
        for node_index, node in enumerate(_walk_body(functions[identifier].node)):
            for operator_index, operator_name in enumerate(_operator_names(node)):
                location = _location(node)
                call_site = (
                    f"{identifier}@{location['line']}:{location['column']}:"
                    f"{node_index}:{operator_index}:{operator_name}"
                )
                grouped[("operator", operator_name, "required")].append(
                    {
                        "call_site": call_site,
                        "caller": identifier,
                        "location": location,
                        "arguments": [],
                        "lookback": {
                            "kind": "none-recorded",
                            "parameters": [],
                            "causal": True,
                        },
                    }
                )
    result = []
    for (family, callable_name, native_requirement), occurrences in sorted(grouped.items()):
        result.append(
            {
                "id": f"{family}:{callable_name}",
                "family": family,
                "callable": callable_name,
                "current_lane": "python-vector-worker",
                "native_requirement": native_requirement,
                "nan_contract": _nan_contract(family, callable_name),
                "occurrences": sorted(
                    occurrences,
                    key=lambda item: (item["caller"], _location_key(item["location"])),
                ),
            }
        )
    return result


def _operator_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.BinOp):
        return [f"operator.{type(node.op).__name__.lower()}"]
    if isinstance(node, ast.BoolOp):
        return [f"operator.{type(node.op).__name__.lower()}"]
    if isinstance(node, ast.UnaryOp):
        return [f"operator.{type(node.op).__name__.lower()}"]
    if isinstance(node, ast.Compare):
        return [f"operator.{type(operator).__name__.lower()}" for operator in node.ops]
    if isinstance(node, ast.IfExp):
        return ["operator.if-expression"]
    return []


def _dataframe_access_inventory(
    functions: dict[str, _FunctionRecord],
    reachable: set[str],
) -> list[dict[str, Any]]:
    result = []
    for identifier in sorted(reachable):
        reads: set[str] = set()
        writes: set[str] = set()
        dynamic_reads = 0
        dynamic_writes = 0
        for node in _walk_body(functions[identifier].node):
            if not isinstance(node, ast.Subscript):
                continue
            literal = _literal_column(node.slice)
            is_write = isinstance(node.ctx, ast.Store)
            if literal is None:
                if is_write:
                    dynamic_writes += 1
                else:
                    dynamic_reads += 1
            elif is_write:
                writes.add(literal)
            else:
                reads.add(literal)
        result.append(
            {
                "node": identifier,
                "literal_reads": sorted(reads),
                "literal_writes": sorted(writes),
                "dynamic_read_count": dynamic_reads,
                "dynamic_write_count": dynamic_writes,
            }
        )
    return result


def _informative_dependencies(
    strategy: dict[str, Any],
    call_sites: list[dict[str, Any]],
    roots: list[dict[str, str]],
) -> dict[str, Any]:
    constants = strategy.get("constants", {})
    timeframe_constants = []
    for name, value in sorted(constants.items()):
        values = sorted(_literal_timeframes(value), key=_timeframe_sort_key)
        if values:
            timeframe_constants.append({"name": name, "values": values})

    requests = []
    merges = []
    fills = []
    for site in call_sites:
        external_names = {
            target["name"] for target in site["targets"] if target["kind"] == "external"
        }
        record = {
            "call_site": site["id"],
            "caller": site["caller"],
            "location": site["location"],
            "arguments": site["arguments"],
        }
        if "freqtrade.informative.get_pair_dataframe" in external_names:
            requests.append(record)
        if "freqtrade.informative.merge_informative_pair" in external_names:
            merges.append(record)
        if external_names & {"pandas.ffill", "pandas.fillna", "pandas.bfill"}:
            fills.append(record)

    root_roles = {item["role"] for item in roots}
    required = list(strategy.get("required_timeframes", []))
    base = constants.get("timeframe") if isinstance(constants.get("timeframe"), str) else None
    informative_timeframes = [item for item in required if item != base]
    registration_present = "informative-dependency-registration" in root_roles
    return {
        "base_timeframe": base,
        "required_timeframes": required,
        "informative_timeframes": informative_timeframes,
        "timeframe_constants": timeframe_constants,
        "dependency_registration_present": registration_present,
        "dataframe_requests": requests,
        "merge_operations": merges,
        "fill_operations": fills,
        "complete": bool(
            not informative_timeframes
            or (registration_present and requests and merges)
        ),
    }


def _coverage_matrix(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families = set(_REQUIRED_COVERAGE_FAMILIES)
    families.update(operation["family"] for operation in operations)
    rows = []
    for family in sorted(families):
        selected = [operation for operation in operations if operation["family"] == family]
        call_sites = {
            occurrence["call_site"]
            for operation in selected
            for occurrence in operation["occurrences"]
        }
        native_required = any(
            operation["native_requirement"] != "not-required" for operation in selected
        )
        rows.append(
            {
                "family": family,
                "present": bool(selected),
                "operation_count": len(selected),
                "call_site_count": len(call_sites),
                "native_required": native_required,
                "native_status": "inventory-only" if native_required else "not-applicable",
            }
        )
    return rows


def _summary(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    call_sites: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    informative: dict[str, Any],
) -> dict[str, Any]:
    unresolved = sum(site["resolution"] == "unresolved" for site in call_sites)
    external = sum(
        any(target["kind"] == "external" for target in site["targets"])
        for site in call_sites
    )
    local = sum(site["resolution"] == "local-helper" for site in call_sites)
    return {
        "reachable_node_count": len(nodes),
        "call_graph_edge_count": len(edges),
        "call_site_count": len(call_sites),
        "local_call_site_count": local,
        "external_call_site_count": external,
        "unresolved_call_site_count": unresolved,
        "operation_count": len(operations),
        "informative_dependency_complete": informative["complete"],
        "inventory_complete": unresolved == 0 and informative["complete"],
    }


def _classify_external(
    value: str,
    function: ast.expr,
) -> tuple[str, str, str]:
    leaf = function.attr if isinstance(function, ast.Attribute) else value.rsplit(".", 1)[-1]
    if value.startswith("ta."):
        return "talib", f"talib.{value[3:]}", "required"
    if value.startswith("qtpylib."):
        return "qtpylib", value, "required"
    if value.startswith("np."):
        return "numpy", f"numpy.{value[3:]}", "required"
    if value.startswith("pd."):
        return "pandas", f"pandas.{value[3:]}", "required"
    if value in {"merge_informative_pair", "freqtrade.strategy.merge_informative_pair"}:
        return "informative", "freqtrade.informative.merge_informative_pair", "required"
    if value.endswith(".get_pair_dataframe"):
        return "informative", "freqtrade.informative.get_pair_dataframe", "required"
    if value.endswith(".current_whitelist"):
        return "informative", "freqtrade.informative.current_whitelist", "required"
    if leaf in _ROLLING_METHODS:
        return "rolling", f"pandas.{leaf}", "required"
    if leaf in _PANDAS_METHODS:
        return "pandas", f"pandas.{leaf}", "required"
    if leaf in _PYTHON_CONTAINER_METHODS:
        return "python-container", f"python.container.{leaf}", "required"
    if leaf in _PYTHON_STRING_METHODS:
        return "python-string", f"python.string.{leaf}", "required"
    if value in _PYTHON_BUILTINS:
        return "python-builtin", f"python.{value}", "required"
    if value.startswith("log.") or value == "time.perf_counter":
        return "instrumentation", value, "not-required"
    if value.startswith("self.dp.") or value.startswith("dp."):
        return "runtime-service", value, "review"
    if value.startswith("self."):
        return "unresolved", value, "review"
    if value.startswith("<dynamic:") or value == "<dynamic>":
        return "unresolved", value, "review"
    return "other", value, "review"


def _lookback_contract(callable_name: str, arguments: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [
        argument
        for argument in arguments
        if argument["name"] in _LOOKBACK_KEYWORDS
    ]
    if callable_name.startswith("talib.") and len(arguments) > 1 and not selected:
        selected = [*selected, {**arguments[1], "name": "#period"}]
    if callable_name == "pandas.rolling" and arguments:
        selected = [*selected, {**arguments[0], "name": "window"}]
    if callable_name in {"pandas.shift", "pandas.diff", "pandas.pct_change"}:
        selected = [*selected, {**arguments[0], "name": "periods"}] if arguments else [
            {"name": "periods", "expression": "1", "literal": 1}
        ]
    if callable_name == "numpy.roll" and len(arguments) > 1:
        selected = [*selected, {**arguments[1], "name": "shift"}]
    unique = {
        (item["name"], item["expression"]): item
        for item in selected
    }
    parameters = [unique[key] for key in sorted(unique)]
    causal: bool | None = None
    if callable_name == "numpy.roll":
        causal = False
    elif callable_name.startswith("talib.") or callable_name in {
        "pandas.diff",
        "pandas.ewm",
        "pandas.expanding",
        "pandas.pct_change",
        "pandas.rolling",
        "pandas.shift",
    }:
        causal = not any(
            item["name"] == "center" and item["literal"] is True
            for item in parameters
        )
        if callable_name == "pandas.shift":
            literal_periods = [
                item["literal"] for item in parameters if item["name"] == "periods"
            ]
            if any(isinstance(value, int) and value < 0 for value in literal_periods):
                causal = False
    return {
        "kind": "bounded-parameters" if parameters else "none-recorded",
        "parameters": parameters,
        "causal": causal,
    }


def _nan_contract(family: str, callable_name: str) -> dict[str, Any]:
    if family == "talib":
        behavior = "TA-Lib warmup and input propagation"
        source = "pinned-ta-lib"
    elif callable_name == "pandas.rolling":
        behavior = "window and min_periods determine leading and sparse NaN output"
        source = "pinned-pandas"
    elif callable_name == "pandas.shift":
        behavior = "displaced boundary rows become NaN"
        source = "pinned-pandas"
    elif callable_name == "pandas.ffill":
        behavior = "only prior non-null values propagate forward"
        source = "pinned-pandas"
    elif callable_name == "pandas.bfill":
        behavior = "later non-null values propagate backward"
        source = "pinned-pandas"
    elif family == "informative":
        behavior = "missing informative visibility remains explicit until source-ordered fill"
        source = "pinned-freqtrade"
    else:
        behavior = "operation and argument dependent"
        source = "pinned-python-environment"
    return {
        "semantic_source": source,
        "behavior": behavior,
        "exact_capture_required": family not in {"instrumentation"},
    }


def _argument_records(node: ast.Call) -> list[dict[str, Any]]:
    records = [
        _argument_record(f"#{index}", argument)
        for index, argument in enumerate(node.args)
    ]
    records.extend(
        _argument_record(keyword.arg or "**", keyword.value)
        for keyword in node.keywords
    )
    return records


def _argument_record(name: str, node: ast.expr) -> dict[str, Any]:
    expression = _expression_text(node)
    try:
        literal = ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        literal = None
    if not _is_json_literal(literal):
        literal = None
    return {
        "name": name,
        "expression": expression,
        "literal": literal,
    }


def _is_json_literal(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_literal(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_literal(item) for key, item in value.items())
    return False


def _expression_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except RecursionError:
        return f"<ast-sha256:{_iterative_ast_sha256(node)}>"


def _iterative_ast_sha256(node: ast.AST) -> str:
    """Hash pathological expression trees without recursive serialization."""
    digest = hashlib.sha256()
    stack: list[tuple[str, Any]] = [("node", node)]
    while stack:
        kind, value = stack.pop()
        digest.update(kind.encode())
        digest.update(b"\0")
        if isinstance(value, ast.AST):
            digest.update(type(value).__name__.encode())
            fields = list(ast.iter_fields(value))
            for name, child in reversed(fields):
                stack.append(("field", name))
                stack.append(("value", child))
        elif isinstance(value, list):
            digest.update(str(len(value)).encode())
            for child in reversed(value):
                stack.append(("value", child))
        else:
            digest.update(repr(value).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _literal_column(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_timeframes(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if _looks_like_timeframe(value) else set()
    if isinstance(value, list):
        return {item for value_item in value for item in _literal_timeframes(value_item)}
    if isinstance(value, dict):
        return {
            item
            for key, value_item in value.items()
            for item in (*_literal_timeframes(key), *_literal_timeframes(value_item))
        }
    return set()


def _looks_like_timeframe(value: str) -> bool:
    return len(value) >= 2 and value[:-1].isdigit() and value[-1] in "smhdwM"


def _timeframe_sort_key(value: str) -> tuple[int, str]:
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}
    return int(value[:-1]) * multipliers[value[-1]], value


def _local_target(
    value: str,
    functions: dict[str, _FunctionRecord],
    class_name: str,
) -> str | None:
    if value.startswith("@local:"):
        candidate = value.removeprefix("@local:")
        return candidate if candidate in functions else None
    if value.startswith("self."):
        candidate = f"class:{class_name}.{value.removeprefix('self.')}"
        return candidate if candidate in functions else None
    if value.startswith(f"{class_name}."):
        candidate = f"class:{value}"
        return candidate if candidate in functions else None
    candidate = f"module:{value}"
    return candidate if candidate in functions else None


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _callable_text(node: ast.expr) -> str:
    qualified = _qualified_name(node)
    if qualified is not None:
        return qualified
    if isinstance(node, ast.Attribute):
        return f"<dynamic:{node.attr}>"
    return "<dynamic>"


def _resolve_alias(value: str, aliases: dict[str, str]) -> str:
    current = value
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        head, separator, tail = current.partition(".")
        replacement = aliases.get(head)
        if replacement is None:
            break
        current = replacement if not separator else f"{replacement}.{tail}"
    return current


def _walk_body(node: _FunctionNode) -> list[ast.AST]:
    result: list[ast.AST] = []
    body = [node.body] if isinstance(node, ast.Lambda) else node.body
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        item = stack.pop()
        result.append(item)
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            children: list[ast.AST] = []
        else:
            children = list(ast.iter_child_nodes(item))
        stack.extend(reversed(children))
    return result


def _call_site_id(caller: str, node: ast.Call) -> str:
    return f"{caller}@{node.lineno}:{node.col_offset}"


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _location_key(location: dict[str, int]) -> tuple[int, int, int, int]:
    return (
        location["line"],
        location["column"],
        location["end_line"],
        location["end_column"],
    )


def _fingerprint(report: dict[str, Any]) -> str:
    identity = copy.deepcopy(report)
    identity["source"].pop("path", None)
    identity["upstream"].pop("repository", None)
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
