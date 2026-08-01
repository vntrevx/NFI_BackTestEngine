"""Compile the reached legacy Grind prefix into strategy-neutral transition data."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import StrategyAnalysisError

LEGACY_GRIND_PROGRAM_VERSION = "grind-transition-program-v1"


def compile_legacy_grind_base_ir(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile first-entry recovery and the first two ordinary Grind clusters.

    The compiler deliberately publishes only the branch surface reached by the
    sealed tag-120 fixture.  The remaining callback stays behind the legacy
    shadow until later roadmap tasks compile and prove those branches.
    """

    clusters = constants.get("clusters")
    if not isinstance(clusters, Sequence) or isinstance(clusters, str | bytes):
        raise StrategyAnalysisError("legacy Grind IR has no cluster descriptors")
    cluster_records = [_cluster_record(record) for record in clusters]
    ordinary = [record for record in cluster_records if not record["post_derisk"]]
    if len(ordinary) < 2:
        raise StrategyAnalysisError("legacy Grind base requires two ordinary clusters")

    loop = _reverse_order_loop(method)
    entry_side, exit_side, entry_branch, exit_branch = _order_sides(loop)
    entry_memberships = _string_memberships(ast.Module(body=entry_branch.body, type_ignores=[]))
    exit_memberships = _string_memberships(ast.Module(body=exit_branch.body, type_ignores=[]))
    close_all = _unique_membership(
        exit_memberships,
        required={"partial_exit", "force_exit"},
    )
    first_entry_closed = _first_entry_closed_tags(method, loop)
    entry_excluded = _largest_membership(entry_memberships, contains=first_entry_closed)
    exit_excluded = _largest_membership(
        exit_memberships,
        contains=first_entry_closed,
        reject=set(close_all),
    )

    known_tags = {
        tag
        for record in cluster_records
        for tag in (record["entry_tag"], record["stop_tag"])
    }
    loop_literals = {
        str(node.value)
        for node in ast.walk(loop)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # The callback intentionally reconstructs the first ordinary cluster as
    # the fallthrough bucket. Its entry/stop tags are absent from the exclusion
    # lists; every later cluster must be named explicitly.
    first_fallback_tags = {ordinary[0]["entry_tag"], ordinary[0]["stop_tag"]}
    missing = sorted(known_tags - loop_literals - first_fallback_tags)
    if missing:
        raise StrategyAnalysisError(
            "legacy Grind order reconstruction is missing source tags: " + ", ".join(missing)
        )

    source_tags = _assigned_order_tags(method)
    base_clusters = ordinary[:2]
    required_actions = {first_entry_closed[0], *(record["entry_tag"] for record in base_clusters)}
    if missing_actions := sorted(required_actions - source_tags):
        raise StrategyAnalysisError(
            "legacy Grind base transition changed: " + ", ".join(missing_actions)
        )

    policy = _literal_policy(method)
    source_order: list[dict[str, Any]] = [
        {
            "kind": "first-entry-profit",
            "tag": first_entry_closed[0],
            "append_entry_ids_from": base_clusters[0]["entry_tag"],
            "profit_threshold": _number(constants, "first_entry_profit_threshold_spot"),
            "location": _tag_location(method, first_entry_closed[0]),
        }
    ]
    source_order.extend(
        {
            "kind": "cluster",
            "entry_tag": record["entry_tag"],
            "stop_tag": record["stop_tag"],
            "append_entry_ids": True,
            "location": _tag_location(method, record["entry_tag"]),
        }
        for record in base_clusters
    )
    program: dict[str, Any] = {
        "schema_version": LEGACY_GRIND_PROGRAM_VERSION,
        "execution_mode": "primary-with-legacy-shadow",
        "source_callback": method.name,
        "source_order": source_order,
        "order_scan": {
            "sequence": "reverse",
            "entry_order_side": entry_side,
            "exit_order_side": exit_side,
            "exclude_first_entry": True,
            "known_clusters": cluster_records,
            "level_one_entry_excluded_tags": entry_excluded,
            "level_one_exit_excluded_tags": exit_excluded,
            "close_all_exit_tags": close_all,
            "first_entry_closed_tags": first_entry_closed,
            "derisk_entry_tag": _derisk_tag(loop),
            "partial_fill_policy": "filled-orders-have-zero-remaining",
        },
        "policy": policy,
        "location": _location(method),
    }
    encoded = json.dumps(
        program,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    program["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return program


def _cluster_record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StrategyAnalysisError("legacy Grind cluster descriptor must be an object")
    entry_tag = value.get("entry_tag")
    stop_tag = value.get("stop_tag")
    if (
        not isinstance(entry_tag, str)
        or not entry_tag
        or not isinstance(stop_tag, str)
        or not stop_tag
    ):
        raise StrategyAnalysisError("legacy Grind cluster tags must be non-empty strings")
    return {
        "entry_tag": entry_tag,
        "stop_tag": stop_tag,
        "post_derisk": _boolean(value, "post_derisk"),
    }


def _reverse_order_loop(method: ast.FunctionDef) -> ast.For:
    loops = [
        node
        for node in method.body
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "order"
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "reversed"
        and len(node.iter.args) == 1
        and isinstance(node.iter.args[0], ast.Name)
        and node.iter.args[0].id == "filled_orders"
    ]
    if len(loops) != 1:
        raise StrategyAnalysisError("legacy Grind reverse filled-order scan changed")
    return loops[0]


def _order_sides(loop: ast.For) -> tuple[str, str, ast.If, ast.If]:
    branches = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.If)
        and any(
            isinstance(comparison, ast.Compare)
            and isinstance(comparison.left, ast.Name)
            and comparison.left.id == "order_side"
            and len(comparison.ops) == 1
            and isinstance(comparison.ops[0], ast.Eq)
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
            and comparison.comparators[0].value in {"buy", "sell"}
            for comparison in ast.walk(node.test)
        )
    ]
    sides = []
    for branch in branches:
        comparison = next(
            comparison
            for comparison in ast.walk(branch.test)
            if isinstance(comparison, ast.Compare)
            and isinstance(comparison.left, ast.Name)
            and comparison.left.id == "order_side"
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
            and comparison.comparators[0].value in {"buy", "sell"}
        )
        comparator = comparison.comparators[0]
        if not isinstance(comparator, ast.Constant):
            raise StrategyAnalysisError("legacy Grind order-side comparison changed")
        sides.append(str(comparator.value))
    unique = list(dict.fromkeys(sides))
    if unique != ["buy", "sell"]:
        raise StrategyAnalysisError("legacy Grind order directions changed")
    if len(branches) != 2:
        raise StrategyAnalysisError("legacy Grind order-side routing changed")
    return unique[0], unique[1], branches[0], branches[1]


def _string_memberships(root: ast.AST) -> list[list[str]]:
    result: list[list[str]] = []
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], (ast.List, ast.Tuple))
            and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.comparators[0].elts
            )
        ):
            container = node.comparators[0]
            values = []
            for item in container.elts:
                if not isinstance(item, ast.Constant):
                    raise StrategyAnalysisError("legacy Grind tag membership changed")
                values.append(str(item.value))
            result.append(values)
    return result


def _unique_membership(memberships: Sequence[list[str]], *, required: set[str]) -> list[str]:
    matches = [values for values in memberships if required.issubset(values)]
    if len(matches) != 1:
        raise StrategyAnalysisError("legacy Grind close-all order classification changed")
    return matches[0]


def _largest_membership(
    memberships: Sequence[list[str]],
    *,
    contains: Sequence[str],
    reject: set[str] | None = None,
) -> list[str]:
    candidates = [
        values
        for values in memberships
        if set(contains).issubset(values) and not (reject or set()).intersection(values)
    ]
    if not candidates:
        raise StrategyAnalysisError("legacy Grind fallback order classification changed")
    candidates.sort(key=len, reverse=True)
    if len(candidates) > 1 and len(candidates[0]) == len(candidates[1]):
        raise StrategyAnalysisError("legacy Grind fallback order classification is ambiguous")
    return candidates[0]


def _first_entry_closed_tags(method: ast.FunctionDef, reverse_loop: ast.For) -> list[str]:
    candidates: list[list[str]] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.For) or node is reverse_loop:
            continue
        if not isinstance(node.iter, ast.Name) or node.iter.id != "filled_orders":
            continue
        candidates.extend(values for values in _string_memberships(node) if len(values) >= 2)
    if len(candidates) != 1:
        raise StrategyAnalysisError("legacy Grind first-entry recovery scan changed")
    return candidates[0]


def _assigned_order_tags(method: ast.FunctionDef) -> set[str]:
    return {
        str(node.value.value)
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "order_tag"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value
    }


def _derisk_tag(loop: ast.For) -> str:
    matches: set[str] = set()
    for branch in (node for node in ast.walk(loop) if isinstance(node, ast.If)):
        stored = {
            target.id
            for node in ast.walk(branch)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if "is_derisk_1" not in stored:
            continue
        for comparison in (node for node in ast.walk(branch.test) if isinstance(node, ast.Compare)):
            if (
                isinstance(comparison.left, ast.Name)
                and comparison.left.id == "order_tag"
                and len(comparison.ops) == 1
                and isinstance(comparison.ops[0], ast.Eq)
                and len(comparison.comparators) == 1
                and isinstance(comparison.comparators[0], ast.Constant)
                and isinstance(comparison.comparators[0].value, str)
            ):
                matches.add(str(comparison.comparators[0].value))
    if len(matches) != 1:
        raise StrategyAnalysisError("legacy Grind de-risk order tag changed")
    return next(iter(matches))


def _literal_policy(method: ast.FunctionDef) -> dict[str, int | float]:
    durations: dict[str, int] = {}
    for statement in method.body:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.BinOp)
            or not isinstance(statement.value.op, ast.Sub)
            or not isinstance(statement.value.right, ast.Call)
            or not isinstance(statement.value.right.func, ast.Name)
            or statement.value.right.func.id != "timedelta"
        ):
            continue
        milliseconds = _timedelta_ms(statement.value.right)
        durations[statement.targets[0].id] = milliseconds
    duration_names = {
        "entry_retry_ms": "grind_entry_retry_time",
        "order_age_ms": "grind_order_age_time",
        "force_order_age_ms": "grind_force_order_age_time",
    }
    if any(name not in durations for name in duration_names.values()):
        raise StrategyAnalysisError("legacy Grind retry policy changed")

    loss_gates = [
        value
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("slice_profit_lt_neg_")
        and isinstance(node.value, ast.Compare)
        and len(node.value.comparators) == 1
        and (value := _ast_number(node.value.comparators[0])) is not None
    ]
    if len(set(loss_gates)) != 1:
        raise StrategyAnalysisError("legacy Grind forced-entry loss gate changed")

    minimums = sorted(
        {
            value
            for node in ast.walk(method)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Name)
            and node.left.id == "min_stake"
            and (value := _ast_number(node.right)) is not None
            and value > 1.0
        }
    )
    if len(minimums) != 2:
        raise StrategyAnalysisError("legacy Grind minimum-stake policy changed")

    derisk_ratios = [
        value
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "is_derisk"
        and isinstance(node.value, ast.Compare)
        for expression in node.value.comparators
        for factor in ast.walk(expression)
        if (value := _ast_number(factor)) is not None and 0.0 < value < 1.0
    ]
    if len(set(derisk_ratios)) != 1:
        raise StrategyAnalysisError("legacy Grind de-risk amount ratio changed")
    return {
        **{key: durations[name] for key, name in duration_names.items()},
        "forced_entry_loss_gate": loss_gates[0],
        "minimum_entry_multiplier": minimums[0],
        "minimum_remaining_multiplier": minimums[1],
        "derisk_amount_ratio": derisk_ratios[0],
    }


def _timedelta_ms(call: ast.Call) -> int:
    values = {keyword.arg: _ast_number(keyword.value) for keyword in call.keywords}
    allowed = {"minutes": 60_000, "hours": 3_600_000}
    populated = [
        (name, value)
        for name, value in values.items()
        if name in allowed and value is not None
    ]
    if len(populated) != 1 or populated[0][1] <= 0.0:
        raise StrategyAnalysisError("legacy Grind timedelta changed")
    name, value = populated[0]
    return int(value * allowed[name])


def _number(constants: Mapping[str, Any], name: str) -> float:
    value = constants.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StrategyAnalysisError(f"legacy Grind IR constant {name} must be numeric")
    return float(value)


def _boolean(constants: Mapping[str, Any], name: str) -> bool:
    value = constants.get(name)
    if not isinstance(value, bool):
        raise StrategyAnalysisError(f"legacy Grind IR field {name} must be boolean")
    return value


def _ast_number(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _ast_number(node.operand)
        return -value if value is not None else None
    return None


def _tag_location(method: ast.FunctionDef, tag: str) -> dict[str, int]:
    matches = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "order_tag"
        and isinstance(node.value, ast.Constant)
        and node.value.value == tag
    ]
    if not matches:
        raise StrategyAnalysisError(f"legacy Grind action {tag} has no source location")
    return _location(matches[0])


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    }
