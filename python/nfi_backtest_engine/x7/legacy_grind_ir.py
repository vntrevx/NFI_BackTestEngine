"""Compile X7's legacy Grind callback into strategy-neutral transition data."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import StrategyAnalysisError

LEGACY_GRIND_PROGRAM_VERSION = "grind-transition-program-v3"


def compile_legacy_grind_ir(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile every source-defined Grind cluster and its ordered actions."""

    clusters = constants.get("clusters")
    if not isinstance(clusters, Sequence) or isinstance(clusters, str | bytes):
        raise StrategyAnalysisError("legacy Grind IR has no cluster descriptors")
    cluster_records = [_cluster_record(record) for record in clusters]
    ordinary = [record for record in cluster_records if not record["post_derisk"]]
    if not ordinary:
        raise StrategyAnalysisError("legacy Grind requires an ordinary cluster")

    loop = _reverse_order_loop(method)
    entry_side, exit_side, entry_branch, exit_branch = _order_sides(loop)
    entry_memberships = _string_memberships(ast.Module(body=entry_branch.body, type_ignores=[]))
    exit_memberships = _string_memberships(ast.Module(body=exit_branch.body, type_ignores=[]))
    close_all = _unique_membership(
        exit_memberships,
        required={"partial_exit", "force_exit"},
    )
    first_entry_closed = _first_entry_closed_tags(method, loop)
    if len(first_entry_closed) != 2:
        raise StrategyAnalysisError("legacy Grind first-entry action set changed")
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
    required_actions = {
        *first_entry_closed,
        *(record["entry_tag"] for record in cluster_records),
        *(record["stop_tag"] for record in cluster_records),
    }
    if missing_actions := sorted(required_actions - source_tags):
        raise StrategyAnalysisError(
            "legacy Grind transition changed: " + ", ".join(missing_actions)
        )

    policy = _literal_policy(method)
    fallback = extract_legacy_futures_fallback(method)
    if fallback["entry_tag"] not in {record["entry_tag"] for record in ordinary}:
        raise StrategyAnalysisError("legacy Grind Futures fallback targets an unknown cluster")
    ordered_clusters = sorted(
        cluster_records,
        key=lambda record: _location_key(_tag_location(method, record["entry_tag"])),
    )
    source_order: list[dict[str, Any]] = [
        {
            "kind": "first-entry",
            "profit_tag": first_entry_closed[0],
            "stop_tag": first_entry_closed[1],
            "append_entry_ids_from": ordinary[0]["entry_tag"],
            "profit_threshold": _number(constants, "first_entry_profit_threshold_spot"),
            "stop_threshold": _number(constants, "first_entry_stop_threshold_spot"),
            "location": _covering_location(
                _tag_location(method, first_entry_closed[0]),
                _tag_location(method, first_entry_closed[1]),
            ),
        }
    ]
    source_order.extend(
        {
            "kind": "cluster",
            "entry_tag": record["entry_tag"],
            "stop_tag": record["stop_tag"],
            "post_derisk": record["post_derisk"],
            "append_entry_ids": True,
            "futures_fallback_loss_threshold": (
                fallback["loss_threshold"]
                if record["entry_tag"] == fallback["entry_tag"]
                else None
            ),
            "location": _tag_location(method, record["entry_tag"]),
        }
        for record in ordered_clusters
    )
    source_order.append(
        _derisk_buyback_transition(
            method,
            loop,
            constants,
            policy,
        )
    )
    program: dict[str, Any] = {
        "schema_version": LEGACY_GRIND_PROGRAM_VERSION,
        "execution_mode": "primary",
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


def _derisk_buyback_transition(
    method: ast.FunctionDef,
    reverse_loop: ast.For,
    constants: Mapping[str, Any],
    policy: Mapping[str, int | float],
) -> dict[str, Any]:
    """Compile the bounded de-risk restoration cycle from callback structure."""

    tag = _derisk_tag(reverse_loop)
    threshold_names = {
        "regular_mode_derisk_1_reentry_futures",
        "regular_mode_derisk_1_reentry_spot",
    }
    entry_required_names = {
        "is_derisk_1",
        "derisk_1_reentry_found",
        "derisk_1_order",
        "grind_entry_retry_time",
        "grind_order_age_time",
        "grind_force_order_age_time",
        "is_long_grind_entry",
        "max_stake",
    }
    entry_candidates: list[ast.If] = []
    exit_candidates: list[ast.If] = []
    for branch in (node for node in method.body if isinstance(node, ast.If)):
        names = {node.id for node in ast.walk(branch) if isinstance(node, ast.Name)}
        attributes = {
            node.attr for node in ast.walk(branch) if isinstance(node, ast.Attribute)
        }
        assigned_tags = _assigned_tags(branch)
        returned_tags = _returned_string_tags(branch)
        if (
            assigned_tags == {tag}
            and entry_required_names.issubset(names)
            and threshold_names.issubset(attributes)
        ):
            entry_candidates.append(branch)
        if (
            tag in returned_tags
            and {"derisk_1_reentry_found", "derisk_1_reentry_order"}.issubset(names)
            and threshold_names.issubset(attributes)
            and "stake_scale_leverage" in names
        ):
            exit_candidates.append(branch)
    if len(entry_candidates) != 1 or len(exit_candidates) != 1:
        raise StrategyAnalysisError(
            "NFI legacy de-risk Buyback routing changed; exact lowering requires review"
        )
    entry_branch = entry_candidates[0]
    exit_branch = exit_candidates[0]
    if _location_key(_location(entry_branch)) >= _location_key(_location(exit_branch)):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback source order changed")

    feature_columns = sorted(
        {
            str(node.slice.value)
            for node in ast.walk(entry_branch)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "last_candle"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        }
    )
    if not feature_columns:
        raise StrategyAnalysisError("NFI legacy de-risk Buyback has no feature guards")
    if not _has_strict_retry_guards(entry_branch):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback retry guard changed")
    if not _has_derisk_entry_stake_assignment(entry_branch):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback entry stake expression changed")
    if not _has_wallet_return_none(entry_branch):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback wallet guard changed")
    if not _has_derisk_exit_stake_assignment(exit_branch):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback exit stake expression changed")
    if not _has_mode_leverage_threshold(exit_branch):
        raise StrategyAnalysisError("NFI legacy de-risk Buyback exit threshold changed")

    return {
        "kind": "derisk-buyback",
        "tag": tag,
        "entry_threshold_futures": _number(constants, "derisk_1_reentry_futures"),
        "entry_threshold_spot": _number(constants, "derisk_1_reentry_spot"),
        "entry_feature_columns": feature_columns,
        "entry_retry_policy": "bounded-grind-policy",
        "entry_stake_basis": "derisk-exit-cost",
        "entry_minimum_multiplier": policy["minimum_entry_multiplier"],
        "entry_wallet_guard": "return-none",
        "exit_threshold_divisor": "mode-leverage",
        "exit_stake_basis": "reentry-amount-at-current-rate",
        "exit_minimum_remaining_multiplier": policy["minimum_remaining_multiplier"],
        "location": _covering_location(_location(entry_branch), _location(exit_branch)),
    }


def _assigned_tags(root: ast.AST) -> set[str]:
    return {
        str(node.value.value)
        for node in ast.walk(root)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "order_tag"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.value.value
    }


def _returned_string_tags(root: ast.AST) -> set[str]:
    return {
        str(item.value)
        for node in ast.walk(root)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
        for item in node.value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value
    }


def _has_wallet_return_none(root: ast.AST) -> bool:
    for branch in (node for node in ast.walk(root) if isinstance(node, ast.If)):
        if not _is_name_comparison(branch.test, "buy_amount", ast.Gt, "max_stake"):
            continue
        if any(
            isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is None
            for statement in branch.body
        ):
            return True
    return False


def _has_strict_retry_guards(root: ast.AST) -> bool:
    required = {
        ("grind_entry_retry_time", "last_filled_entry", "order_filled_utc"),
        ("grind_force_order_age_time", "last_filled_order", "order_filled_utc"),
        ("grind_order_age_time", "last_filled_order", "order_filled_utc"),
    }
    observed = {
        (comparison.left.id, comparator.value.id, comparator.attr)
        for comparison in ast.walk(root)
        if isinstance(comparison, ast.Compare)
        and isinstance(comparison.left, ast.Name)
        and len(comparison.ops) == 1
        and isinstance(comparison.ops[0], ast.Gt)
        and len(comparison.comparators) == 1
        and isinstance((comparator := comparison.comparators[0]), ast.Attribute)
        and isinstance(comparator.value, ast.Name)
    }
    return required.issubset(observed)


def _has_derisk_entry_stake_assignment(root: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and _assigns_name(node, "buy_amount")
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Mult)
        and _is_attribute(node.value.left, "derisk_1_order", "safe_filled")
        and _is_attribute(node.value.right, "derisk_1_order", "safe_price")
        for node in ast.walk(root)
    )


def _has_derisk_exit_stake_assignment(root: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and _assigns_name(node, "sell_amount")
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Div)
        and isinstance(node.value.left, ast.BinOp)
        and isinstance(node.value.left.op, ast.Mult)
        and _is_attribute(
            node.value.left.left,
            "derisk_1_reentry_order",
            "safe_filled",
        )
        and isinstance(node.value.left.right, ast.Name)
        and node.value.left.right.id == "exit_rate"
        and isinstance(node.value.right, ast.Name)
        and node.value.right.id == "trade_leverage"
        for node in ast.walk(root)
    )


def _has_mode_leverage_threshold(root: ast.AST) -> bool:
    return any(
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.IfExp)
        and isinstance(node.right, ast.Name)
        and node.right.id == "stake_scale_leverage"
        for node in ast.walk(root)
    )


def _assigns_name(node: ast.Assign, name: str) -> bool:
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    )


def _is_attribute(node: ast.AST, owner: str, attribute: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
    )


def _is_name_comparison(
    node: ast.AST,
    left: str,
    operator: type[ast.cmpop],
    right: str,
) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == left
        and len(node.ops) == 1
        and isinstance(node.ops[0], operator)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == right
    )


def extract_legacy_futures_fallback(method: ast.FunctionDef) -> dict[str, Any]:
    """Extract the source-ordered Futures drawdown entry action."""

    candidates: list[dict[str, Any]] = []
    required_names = {
        "is_futures",
        "has_order_tags",
        "partial_sell",
        "slice_profit",
        "trade_leverage",
        "is_derisk",
        "is_derisk_calc",
        "is_grind_mode",
    }
    for branch in method.body:
        if not isinstance(branch, ast.If) or not isinstance(branch.test, ast.BoolOp):
            continue
        names = {node.id for node in ast.walk(branch.test) if isinstance(node, ast.Name)}
        if not required_names.issubset(names):
            continue
        assigned_tags = {
            str(node.value.value)
            for statement in branch.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "order_tag"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value
        }
        comparisons = [
            node
            for node in ast.walk(branch.test)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "slice_profit"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Lt)
            and len(node.comparators) == 1
        ]
        level_bounds = [
            node
            for node in ast.walk(branch.test)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id.endswith("_sub_grind_count")
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Lt)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id.endswith("_max_sub_grinds")
            and node.left.id.removesuffix("_sub_grind_count")
            == node.comparators[0].id.removesuffix("_max_sub_grinds")
        ]
        if len(assigned_tags) != 1 or len(comparisons) != 1 or len(level_bounds) != 1:
            continue
        threshold = comparisons[0].comparators[0]
        if (
            not isinstance(threshold, ast.BinOp)
            or not isinstance(threshold.op, ast.Div)
            or not isinstance(threshold.right, ast.Name)
            or threshold.right.id != "trade_leverage"
            or (value := _ast_number(threshold.left)) is None
            or not math.isfinite(value)
            or value >= 0.0
        ):
            continue
        candidates.append(
            {
                "entry_tag": next(iter(assigned_tags)),
                "loss_threshold": value,
                "location": _location(branch),
            }
        )
    if len(candidates) != 1:
        raise StrategyAnalysisError(
            "NFI legacy futures drawdown fallback changed; exact lowering requires review"
        )
    return candidates[0]


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


def _location_key(location: Mapping[str, int]) -> tuple[int, int]:
    return location["line"], location["column"]


def _covering_location(
    first: Mapping[str, int],
    second: Mapping[str, int],
) -> dict[str, int]:
    start = min((first, second), key=_location_key)
    end = max((first, second), key=lambda value: (value["end_line"], value["end_column"]))
    return {
        "line": start["line"],
        "column": start["column"],
        "end_line": end["end_line"],
        "end_column": end["end_column"],
    }


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    }
