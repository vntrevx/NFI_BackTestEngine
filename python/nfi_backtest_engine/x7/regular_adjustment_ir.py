"""Compile X7's regular-mode adjustment prelude into strategy-neutral data."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError

REGULAR_ADJUSTMENT_PROGRAM_VERSION = "regular-transition-program-v1"


def compile_regular_adjustment_ir(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile source-ordered rebuy, Grind, de-risk, and continuation routing."""

    levels = _constant_levels(constants)
    reverse_loop = _reverse_order_loop(method)
    entry_branch, exit_branch = _order_branches(reverse_loop)
    grinds = _grind_records(entry_branch, exit_branch, levels)
    futures_fallback = _futures_fallback(method, grinds)
    for record in grinds:
        record["location"] = _grind_action_location(
            method,
            level=record["level"],
            tag=record["entry_tag"],
        )
        record["futures_fallback_loss_threshold"] = (
            futures_fallback["loss_threshold"]
            if record["level"] == futures_fallback["level"]
            else None
        )
    rebuy_entry_excluded = _rebuy_excluded_tags(
        entry_branch,
        counter="rebuy_sub_grind_count",
    )
    rebuy_exit_excluded = _rebuy_excluded_tags(
        exit_branch,
        counter="rebuy_is_sell_found",
    )
    derisk_tags, level_one_tag = _derisk_tags(exit_branch)
    rebuy_tag = _action_tag(method, required_name="rebuy_sub_grind_count")
    partial_fill_tag = _action_tag(method, required_name="partial_sell")
    derisk_actions = _derisk_action_locations(method, derisk_tags, level_one_tag)
    continuation = _continuation(method, reverse_loop)

    actions: list[dict[str, Any]] = [
        {
            "kind": "rebuy",
            "tag": rebuy_tag["tag"],
            "location": rebuy_tag["location"],
        },
        *(
            {
                "kind": "grind",
                "level": record["level"],
                "entry_tag": record["entry_tag"],
                "stop_tag": record["stop_tag"],
                "futures_fallback_loss_threshold": record[
                    "futures_fallback_loss_threshold"
                ],
                "location": record["location"],
            }
            for record in grinds
        ),
        *(
            {
                "kind": "derisk",
                "tag": tag,
                "level_one": tag == level_one_tag,
                "location": derisk_actions[tag],
            }
            for tag in derisk_actions
        ),
    ]
    actions.sort(key=lambda action: _location_key(action["location"]))
    expected_kinds = ["rebuy", *("grind" for _ in levels), "derisk", "derisk"]
    if [action["kind"] for action in actions] != expected_kinds:
        raise StrategyAnalysisError("NFI regular adjustment source order changed")
    if _location_key(continuation["location"]) >= _location_key(actions[0]["location"]):
        raise StrategyAnalysisError("NFI regular-to-Grind continuation order changed")

    program: dict[str, Any] = {
        "schema_version": REGULAR_ADJUSTMENT_PROGRAM_VERSION,
        "execution_mode": "primary-with-legacy-shadow",
        "source_callback": method.name,
        "source_order": actions,
        "order_scan": {
            "sequence": "reverse",
            "entry_order_side": "buy",
            "exit_order_side": "sell",
            "exclude_first_entry": True,
            "rebuy_entry_excluded_tags": rebuy_entry_excluded,
            "rebuy_exit_excluded_tags": rebuy_exit_excluded,
            "derisk_exit_tags": derisk_tags,
            "derisk_level_one_tag": level_one_tag,
            "partial_fill_tag": partial_fill_tag["tag"],
        },
        "continuation": continuation,
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


def _constant_levels(constants: Mapping[str, Any]) -> list[int]:
    levels = sorted(
        {
            int(match.group(1))
            for name in constants
            if (match := re.fullmatch(r"regular_mode_grind_(\d+)_stakes_futures", name))
        }
    )
    if not levels or levels != list(range(1, len(levels) + 1)):
        raise StrategyAnalysisError("NFI regular adjustment Grind levels changed")
    return levels


def _reverse_order_loop(method: ast.FunctionDef) -> ast.For:
    matches = [
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
    if len(matches) != 1:
        raise StrategyAnalysisError("NFI regular adjustment reverse order scan changed")
    return matches[0]


def _order_branches(loop: ast.For) -> tuple[ast.If, ast.If]:
    result: dict[str, ast.If] = {}
    for branch in (node for node in ast.walk(loop) if isinstance(node, ast.If)):
        for comparison in (node for node in ast.walk(branch.test) if isinstance(node, ast.Compare)):
            if (
                isinstance(comparison.left, ast.Name)
                and comparison.left.id == "order_side"
                and len(comparison.ops) == 1
                and isinstance(comparison.ops[0], ast.Eq)
                and len(comparison.comparators) == 1
                and isinstance(comparison.comparators[0], ast.Constant)
                and comparison.comparators[0].value in {"buy", "sell"}
            ):
                result[str(comparison.comparators[0].value)] = branch
    if set(result) != {"buy", "sell"}:
        raise StrategyAnalysisError("NFI regular adjustment order-side routing changed")
    return result["buy"], result["sell"]


def _grind_records(
    entry_branch: ast.If,
    exit_branch: ast.If,
    levels: list[int],
) -> list[dict[str, Any]]:
    entry_tags: dict[int, tuple[str, dict[str, int]]] = {}
    for branch in (node for node in ast.walk(entry_branch) if isinstance(node, ast.If)):
        level = _mutated_level(branch, suffix="_sub_grind_count")
        tag = _compared_string(branch.test, membership=False)
        if level is not None and tag is not None:
            entry_tags[level] = (tag, _location(branch))

    stop_tags: dict[int, str] = {}
    for branch in (node for node in ast.walk(exit_branch) if isinstance(node, ast.If)):
        level = _mutated_level(branch, suffix="_is_sell_found")
        tags = _compared_strings(branch.test)
        if level is not None and len(tags) == 2:
            entry = entry_tags.get(level)
            if entry is not None and entry[0] in tags:
                stop_tags[level] = next(tag for tag in tags if tag != entry[0])
    if set(entry_tags) != set(levels) or set(stop_tags) != set(levels):
        raise StrategyAnalysisError("NFI regular adjustment Grind tag routing changed")
    return [
        {
            "level": level,
            "entry_tag": entry_tags[level][0],
            "stop_tag": stop_tags[level],
            "location": entry_tags[level][1],
        }
        for level in levels
    ]


def _mutated_level(root: ast.AST, *, suffix: str) -> int | None:
    scan_root = ast.Module(body=root.body, type_ignores=[]) if isinstance(root, ast.If) else root
    levels = {
        int(match.group(1))
        for node in ast.walk(scan_root)
        if isinstance(node, (ast.Assign, ast.AugAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
        and (match := re.fullmatch(rf"grind_(\d+){re.escape(suffix)}", target.id))
    }
    return next(iter(levels)) if len(levels) == 1 else None


def _rebuy_excluded_tags(root: ast.AST, *, counter: str) -> list[str]:
    matches: list[list[str]] = []
    for branch in (node for node in ast.walk(root) if isinstance(node, ast.If)):
        stored = {
            target.id
            for node in ast.walk(branch)
            if isinstance(node, (ast.Assign, ast.AugAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        if counter not in stored:
            continue
        values = _not_in_strings(branch.test)
        if values:
            matches.append(values)
    if len(matches) != 1:
        raise StrategyAnalysisError("NFI regular adjustment rebuy order classification changed")
    return matches[0]


def _derisk_tags(root: ast.AST) -> tuple[list[str], str]:
    candidates: list[tuple[list[str], ast.If]] = []
    for branch in (node for node in ast.walk(root) if isinstance(node, ast.If)):
        if _assigns_boolean(branch, "is_derisk", True):
            tags = _compared_strings(branch.test)
            if len(tags) >= 2:
                candidates.append((tags, branch))
    if len(candidates) != 1:
        raise StrategyAnalysisError("NFI regular adjustment de-risk classification changed")
    tags, branch = candidates[0]
    # The nested branch that sets is_derisk_1 is the exact source discriminator.
    level_one = {
        value
        for nested in (node for node in ast.walk(branch) if isinstance(node, ast.If))
        if _assigns_boolean(nested, "is_derisk_1", True)
        for value in [_equality_string(nested.test, "order_tag")]
        if value is not None
    }
    if len(level_one) != 1 or not level_one.issubset(tags):
        raise StrategyAnalysisError("NFI regular adjustment level-one de-risk tag changed")
    return tags, next(iter(level_one))


def _action_tag(method: ast.FunctionDef, *, required_name: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for branch in (node for node in method.body if isinstance(node, ast.If)):
        names = {node.id for node in ast.walk(branch.test) if isinstance(node, ast.Name)}
        if required_name not in names:
            continue
        if required_name == "partial_sell" and not (
            isinstance(branch.test, ast.Name) and branch.test.id == required_name
        ):
            continue
        tags = _assigned_order_tags(branch)
        if len(tags) == 1 and any(isinstance(node, ast.Return) for node in ast.walk(branch)):
            matches.append({"tag": next(iter(tags)), "location": _location(branch)})
    if len(matches) != 1:
        raise StrategyAnalysisError(
            f"NFI regular adjustment {required_name} action routing changed"
        )
    return matches[0]


def _grind_action_location(
    method: ast.FunctionDef,
    *,
    level: int,
    tag: str,
) -> dict[str, int]:
    counter = f"grind_{level}_sub_grind_count"
    matches = [
        branch
        for branch in method.body
        if isinstance(branch, ast.If)
        and counter in {node.id for node in ast.walk(branch.test) if isinstance(node, ast.Name)}
        and tag in _assigned_order_tags(branch)
    ]
    if not matches:
        raise StrategyAnalysisError(f"NFI regular adjustment Grind level {level} action changed")
    return _location(min(matches, key=lambda branch: (branch.lineno, branch.col_offset)))


def _futures_fallback(
    method: ast.FunctionDef,
    grinds: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract the leverage-scaled Futures entry that bypasses vector guards."""

    by_tag = {str(record["entry_tag"]): int(record["level"]) for record in grinds}
    candidates: list[dict[str, Any]] = []
    required_names = {
        "is_futures",
        "has_order_tags",
        "partial_sell",
        "slice_profit",
        "trade_leverage",
    }
    for branch in (node for node in method.body if isinstance(node, ast.If)):
        names = {node.id for node in ast.walk(branch.test) if isinstance(node, ast.Name)}
        if not required_names.issubset(names):
            continue
        tags = _assigned_order_tags(branch)
        if len(tags) != 1 or (tag := next(iter(tags))) not in by_tag:
            continue
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
        counter = f"grind_{by_tag[tag]}_sub_grind_count"
        limit = f"max_grind_{by_tag[tag]}_sub_grinds"
        has_level_bound = any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == counter
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Lt)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == limit
            for node in ast.walk(branch.test)
        )
        if len(comparisons) != 1 or not has_level_bound:
            continue
        threshold = comparisons[0].comparators[0]
        if (
            not isinstance(threshold, ast.BinOp)
            or not isinstance(threshold.op, ast.Div)
            or not isinstance(threshold.right, ast.Name)
            or threshold.right.id != "trade_leverage"
            or (value := _number(threshold.left)) is None
            or not math.isfinite(value)
            or value >= 0.0
        ):
            continue
        candidates.append(
            {
                "level": by_tag[tag],
                "loss_threshold": value,
                "location": _location(branch),
            }
        )
    if len(candidates) != 1:
        raise StrategyAnalysisError(
            "NFI regular adjustment Futures drawdown fallback changed"
        )
    return candidates[0]


def _derisk_action_locations(
    method: ast.FunctionDef,
    tags: list[str],
    level_one_tag: str,
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for branch in (node for node in method.body if isinstance(node, ast.If)):
        returned = _returned_string_tags(branch)
        for tag in set(tags).intersection(returned):
            result[tag] = _location(branch)
    if (
        len(result) != 2
        or level_one_tag not in result
        or not set(result).issubset(tags)
    ):
        raise StrategyAnalysisError("NFI regular adjustment de-risk actions changed")
    return result


def _continuation(method: ast.FunctionDef, reverse_loop: ast.For) -> dict[str, Any]:
    candidates = [
        branch
        for branch in method.body
        if isinstance(branch, ast.If)
        and isinstance(branch.test, ast.Name)
        and branch.test.id == "is_derisk"
        and any(isinstance(node, ast.Return) for node in ast.walk(branch))
    ]
    if len(candidates) != 1:
        raise StrategyAnalysisError("NFI regular-to-Grind continuation changed")
    ratios = {
        value
        for node in ast.walk(reverse_loop)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "current_amount"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Lt)
        and len(node.comparators) == 1
        for value in [_multiplication_constant(node.comparators[0], "start_amount")]
        if value is not None
    }
    if len(ratios) != 1:
        raise StrategyAnalysisError("NFI regular-to-Grind amount guard changed")
    ratio = next(iter(ratios))
    if not math.isfinite(ratio) or not 0.0 < ratio < 1.0:
        raise StrategyAnalysisError("NFI regular-to-Grind amount ratio is invalid")
    return {
        "kind": "legacy-grind",
        "guard": "position-amount-below-first-entry-ratio",
        "amount_ratio": ratio,
        "location": _location(candidates[0]),
    }


def _compared_string(root: ast.AST, *, membership: bool) -> str | None:
    matches = {
        value
        for node in ast.walk(root)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "order_tag"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In if membership else ast.Eq)
        for value in [_equality_string(node, "order_tag")]
        if value is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _compared_strings(root: ast.AST) -> list[str]:
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "order_tag"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], (ast.List, ast.Tuple))
            and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.comparators[0].elts
            )
        ):
            return [
                str(item.value)
                for item in node.comparators[0].elts
                if isinstance(item, ast.Constant)
            ]
    return []


def _not_in_strings(root: ast.AST) -> list[str]:
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "order_tag"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.NotIn)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], (ast.List, ast.Tuple))
            and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.comparators[0].elts
            )
        ):
            return [
                str(item.value)
                for item in node.comparators[0].elts
                if isinstance(item, ast.Constant)
            ]
    return []


def _equality_string(root: ast.AST, name: str) -> str | None:
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == name
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            return str(node.comparators[0].value)
    return None


def _assigns_boolean(root: ast.AST, name: str, value: bool) -> bool:
    nodes = root.body if isinstance(root, ast.If) else ast.walk(root)
    return any(
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and isinstance(node.value, ast.Constant)
        and node.value.value is value
        for node in nodes
    )


def _assigned_order_tags(root: ast.AST) -> set[str]:
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


def _multiplication_constant(root: ast.AST, name: str) -> float | None:
    if not isinstance(root, ast.BinOp) or not isinstance(root.op, ast.Mult):
        return None
    operands = (root.left, root.right)
    if not any(isinstance(node, ast.Name) and node.id == name for node in operands):
        return None
    numeric = next(
        (
            float(node.value)
            for node in operands
            if isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ),
        None,
    )
    return numeric


def _number(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return -float(node.operand.value)
    return None


def _location_key(location: Mapping[str, int]) -> tuple[int, int]:
    return location["line"], location["column"]


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    }
