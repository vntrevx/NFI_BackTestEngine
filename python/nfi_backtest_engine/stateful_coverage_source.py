"""Source and AST reachability proofs for stateful callback coverage."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import StrategyAnalysisError
from .stateful_coverage_contracts import STATEFUL_CALLBACKS

_ENTRY_PARAMETER_PATTERN = re.compile(
    r"^(?P<side>long|short)_entry_condition_(?P<tag>[^_]+)_enable$"
)
_FILLED_REMAINDER_POLICY = "filled-orders-have-zero-remaining"


def compile_entry_signals(path: Path, strategy: dict[str, Any]) -> list[dict[str, Any]]:
    """Prove and extract the enabled-parameter to emitted-tag mapping."""
    constants = strategy.get("constants")
    if not isinstance(constants, Mapping):
        raise StrategyAnalysisError("entry signal constants are unavailable")
    source_bytes = path.read_bytes()
    tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if class_node is None:
        raise StrategyAnalysisError("selected strategy class disappeared")
    entry_method = next(
        (
            node
            for node in class_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "populate_entry_trend"
        ),
        None,
    )
    if entry_method is None:
        raise StrategyAnalysisError("populate_entry_trend is required for entry-tag proof")

    records: list[dict[str, Any]] = []
    for side in ("long", "short"):
        constant_name = f"{side}_entry_signal_params"
        parameters = constants.get(constant_name)
        if not isinstance(parameters, Mapping) or not parameters:
            raise StrategyAnalysisError(f"{constant_name} must be a non-empty static mapping")
        enabled: list[str] = []
        disabled: list[str] = []
        parameter_names: list[str] = []
        for name, value in parameters.items():
            if not isinstance(name, str) or not isinstance(value, bool):
                raise StrategyAnalysisError(f"{constant_name} entries must be boolean parameters")
            match = _ENTRY_PARAMETER_PATTERN.fullmatch(name)
            if match is None or match.group("side") != side:
                raise StrategyAnalysisError(
                    f"{constant_name} contains an unrecognized entry parameter {name!r}"
                )
            tag = match.group("tag")
            parameter_names.append(name)
            (enabled if value else disabled).append(tag)
        records.append(
            {
                "side": side,
                "parameter_constant": constant_name,
                "parameters": parameter_names,
                "enabled_tags": enabled,
                "disabled_tags": disabled,
                "proof": _entry_loop_proof(entry_method, side, constant_name),
            }
        )
    return records


def empty_entry_signals() -> list[dict[str, Any]]:
    """Return a schema-valid empty inventory after a fail-closed proof error."""
    return [
        {
            "side": side,
            "parameter_constant": f"{side}_entry_signal_params",
            "parameters": [],
            "enabled_tags": [],
            "disabled_tags": [],
            "proof": None,
        }
        for side in ("long", "short")
    ]


def active_entry_tags(
    entry_signals: list[dict[str, Any]],
    trading_mode: str,
) -> dict[str, list[str]]:
    """Apply Freqtrade mode reachability to source-enabled entry tags."""
    by_side = {item["side"]: list(item["enabled_tags"]) for item in entry_signals}
    if trading_mode == "spot":
        by_side["short"] = []
    return {side: by_side.get(side, []) for side in ("long", "short")}


def source_routes(
    callback_source: dict[str, Any],
    *,
    active_tags: dict[str, list[str]],
    exit_tags: dict[str, set[str]],
    adjustment_tags: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Intersect callback route constants with mode-reachable source tags."""
    records: list[dict[str, Any]] = []
    for route in callback_source["route_keys"]:
        entrypoints = [
            name for name in route["entrypoints"] if name in STATEFUL_CALLBACKS
        ]
        if not entrypoints:
            continue
        key = route["key"]
        side = (
            "long"
            if key.startswith("long_")
            else "short"
            if key.startswith("short_")
            else None
        )
        declared = list(route["values"])
        reachable = (
            [tag for tag in declared if tag in set(active_tags[side])]
            if side is not None
            else []
        )
        records.append(
            {
                "key": key,
                "side": side,
                "entrypoints": entrypoints,
                "declared_tags": declared,
                "reachable_tags": reachable,
                "dormant_tags": [tag for tag in declared if tag not in set(reachable)],
                "native_exit_covered_tags": (
                    [tag for tag in declared if tag in exit_tags[side]]
                    if side is not None
                    else []
                ),
                "native_adjustment_covered_tags": (
                    [tag for tag in declared if tag in adjustment_tags[side]]
                    if side is not None
                    else []
                ),
                "unsupported_reachable_tags": (
                    [
                        tag
                        for tag in reachable
                        if tag not in exit_tags[side] or tag not in adjustment_tags[side]
                    ]
                    if side is not None
                    else reachable
                ),
                "location": _without_path(route["location"]),
            }
        )
    return records


def dormant_unsupported_routes(
    route_records: list[dict[str, Any]],
    *,
    entry_signals: list[dict[str, Any]],
    trading_mode: str,
) -> list[dict[str, Any]]:
    """Keep unsupported but unreachable routes visible without calling them covered."""
    disabled = {item["side"]: set(item["disabled_tags"]) for item in entry_signals}
    grouped: dict[tuple[str, str], list[str]] = {}
    for route in route_records:
        side = route["side"]
        if side is None:
            continue
        native = set(route["native_exit_covered_tags"]) | set(
            route["native_adjustment_covered_tags"]
        )
        for tag in route["dormant_tags"]:
            if tag not in native:
                grouped.setdefault((side, tag), []).append(route["key"])
    result = []
    for (side, tag), route_keys in sorted(grouped.items()):
        if side == "short" and trading_mode == "spot":
            reason = "side-inactive-in-spot"
        elif tag in disabled.get(side, set()):
            reason = "source-signal-disabled"
        else:
            reason = "not-emitted-by-entry-compiler"
        result.append(
            {
                "side": side,
                "tag": tag,
                "route_keys": route_keys,
                "reason": reason,
                "qualifies_as_native_coverage": False,
            }
        )
    return result


def live_only_exclusions(
    path: Path,
    *,
    strategy_name: str,
    callback_source: dict[str, Any],
    operation: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prove source partial-fill branches unreachable under the backtest contract."""
    source_bytes = path.read_bytes()
    tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_name
        ),
        None,
    )
    if class_node is None:  # pragma: no cover - selected by analysis before this call
        raise StrategyAnalysisError("selected strategy class disappeared")
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
    }
    adjustment_entry = next(
        (
            item
            for item in callback_source["entrypoints"]
            if item["name"] == "adjust_trade_position"
        ),
        None,
    )
    reachable = (
        set(adjustment_entry["reachable_methods"])
        if isinstance(adjustment_entry, Mapping)
        else set()
    )
    safe_reads: list[tuple[str, ast.Attribute]] = []
    guards: list[dict[str, Any]] = []
    for method_name in sorted(reachable):
        method = methods.get(method_name)
        if method is None:
            continue
        safe_reads.extend(
            (method_name, node)
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute) and node.attr == "safe_remaining"
        )
        for node in ast.walk(method):
            if isinstance(node, ast.Compare) and _is_filled_remainder_guard(node):
                guards.append(
                    {
                        "method": method_name,
                        "expression": ast.unparse(node),
                        "location": _location(node),
                    }
                )
    guards.sort(key=lambda item: (item["location"]["line"], item["location"]["column"]))
    policy_paths = _find_policy_paths(operation, _FILLED_REMAINDER_POLICY)
    exclusions: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    if guards and policy_paths:
        exclusions.append(
            {
                "code": "FILLED_ORDER_PARTIAL_REMAINDER",
                "runtime_scope": "live-only",
                "source_predicates": guards,
                "native_policy": _FILLED_REMAINDER_POLICY,
                "native_contract_paths": policy_paths,
                "backtest_invariant": "filled exit orders expose safe_remaining == 0",
                "qualifies_as_backtest_coverage": False,
            }
        )
    elif guards:
        gaps.append(
            {
                "code": "LIVE_ONLY_BRANCH_POLICY_MISSING",
                "callback": "adjust_trade_position",
                "side": None,
                "tags": [],
                "message": "safe_remaining branch lacks the serialized backtest exclusion policy",
            }
        )
    elif safe_reads:
        gaps.append(
            {
                "code": "SAFE_REMAINING_BRANCH_NOT_PROVEN_LIVE_ONLY",
                "callback": "adjust_trade_position",
                "side": None,
                "tags": [],
                "message": "safe_remaining reads no longer have a proven positive-minimum guard",
            }
        )
    return exclusions, gaps


def _entry_loop_proof(
    method: ast.FunctionDef,
    side: str,
    constant_name: str,
) -> dict[str, Any]:
    item_name = f"enabled_{side}_entry_signal"
    index_name = f"{side}_entry_condition_index"
    loops = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == item_name
        and isinstance(node.iter, ast.Name)
        and node.iter.id == constant_name
    ]
    if len(loops) != 1:
        raise StrategyAnalysisError(
            f"{constant_name} must drive exactly one source-ordered entry loop"
        )
    loop = loops[0]
    index_assignments = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == index_name
        and _contains_name(node.value, item_name)
        and _contains_call_leaf(node.value, "rsplit")
    ]
    guards = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.If)
        and any(
            isinstance(candidate, ast.Subscript)
            and isinstance(candidate.value, ast.Name)
            and candidate.value.id == constant_name
            and _contains_name(candidate.slice, item_name)
            for candidate in ast.walk(node.test)
        )
    ]
    emissions = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and _call_leaf(node) == "_append_entry_tag"
        and len(node.args) >= 3
        and isinstance(node.args[2], ast.JoinedStr)
        and _contains_name(node.args[2], index_name)
    ]
    if len(index_assignments) != 1 or len(guards) != 1 or len(emissions) != 1:
        raise StrategyAnalysisError(
            f"{side} entry loop no longer proves enabled parameter to emitted tag identity"
        )
    return {
        "compiler": "enabled-entry-parameter-loop-v1",
        "loop": _location(loop),
        "index_extraction": _location(index_assignments[0]),
        "enabled_guard": _location(guards[0]),
        "tag_emission": _location(emissions[0]),
    }


def _is_filled_remainder_guard(node: ast.Compare) -> bool:
    if not any(isinstance(op, ast.Gt) for op in node.ops):
        return False
    nodes = [node.left, *node.comparators]
    has_remaining = any(
        isinstance(item, ast.Attribute) and item.attr == "safe_remaining"
        for expression in nodes
        for item in ast.walk(expression)
    )
    has_minimum = any(
        isinstance(item, ast.Name) and item.id == "min_stake"
        for expression in nodes
        for item in ast.walk(expression)
    )
    return has_remaining and has_minimum


def _find_policy_paths(value: Any, policy: str, path: str = "operation") -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "partial_fill_policy" and item == policy:
                result.append(child)
            result.extend(_find_policy_paths(item, policy, child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, item in enumerate(value):
            result.extend(_find_policy_paths(item, policy, f"{path}[{index}]"))
    return result


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _contains_call_leaf(node: ast.AST, leaf: str) -> bool:
    return any(
        isinstance(item, ast.Call) and _call_leaf(item) == leaf
        for item in ast.walk(node)
    )


def _call_leaf(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _without_path(location: Mapping[str, Any]) -> dict[str, int]:
    return {
        "line": int(location["line"]),
        "column": int(location["column"]),
        "end_line": int(location["end_line"]),
        "end_column": int(location["end_column"]),
    }
