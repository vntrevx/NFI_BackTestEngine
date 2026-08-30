"""Compile X7 rebuy callbacks into a data-driven adjustment program."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program

REBUY_TRANSITION_PROGRAM_VERSION = "adjustment-transition-program-v1"


def compile_rebuy_transition_ir(
    method: ast.FunctionDef,
    constants: Mapping[str, Any],
    *,
    delegate_retry_ms: int,
) -> dict[str, Any]:
    """Lower one long/short rebuy callback without method-identity gates."""

    if delegate_retry_ms <= 0:
        raise StrategyAnalysisError("rebuy delegate retry window must be positive")
    order_scan = _compile_order_scan(method)
    delegate = _compile_delegate(method, retry_ms=delegate_retry_ms)
    fragment = _decision_fragment(method)
    decision_program = compile_scalar_ast_program(fragment, constants=dict(constants))
    input_contract = _input_contract(fragment)
    program: dict[str, Any] = {
        "schema_version": REBUY_TRANSITION_PROGRAM_VERSION,
        "execution_mode": "primary",
        "source_order": ["delegate", "decision"],
        "order_scan": order_scan,
        "delegate": delegate,
        "decision_program": decision_program,
        "input_contract": input_contract,
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


def _compile_order_scan(method: ast.FunctionDef) -> dict[str, Any]:
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
        and not node.iter.keywords
    ]
    if len(loops) != 1 or len(loops[0].body) != 1 or not isinstance(loops[0].body[0], ast.If):
        raise StrategyAnalysisError("rebuy filled-order scan changed")
    branch = loops[0].body[0]
    assert isinstance(branch, ast.If)
    cluster_side = _order_side_comparison(branch.test)
    boundary = branch.orelse
    if len(boundary) != 1 or not isinstance(boundary[0], ast.If):
        raise StrategyAnalysisError("rebuy filled-order boundary changed")
    boundary_side = _order_side_comparison(boundary[0].test)
    if (
        cluster_side is None
        or boundary_side is None
        or cluster_side == boundary_side
        or not any(
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "sub_grind_count"
            for node in ast.walk(branch)
        )
        or not any(isinstance(node, ast.Break) for node in ast.walk(boundary[0]))
    ):
        raise StrategyAnalysisError("rebuy filled-order cluster semantics changed")
    excludes_first = any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "order"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "first_filled_order"
        for node in ast.walk(branch.test)
    )
    if not excludes_first:
        raise StrategyAnalysisError("rebuy first-order exclusion changed")
    return {
        "sequence": "reverse",
        "cluster_order_side": cluster_side,
        "boundary_order_side": boundary_side,
        "exclude_first_order": True,
        "partial_fill_policy": "filled-orders-have-zero-remaining",
    }


def _order_side_comparison(node: ast.AST) -> str | None:
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Compare)
            and isinstance(item.left, ast.Attribute)
            and isinstance(item.left.value, ast.Name)
            and item.left.value.id == "order"
            and item.left.attr == "ft_order_side"
            and len(item.ops) == 1
            and isinstance(item.ops[0], ast.Eq)
            and len(item.comparators) == 1
            and isinstance(item.comparators[0], ast.Constant)
            and item.comparators[0].value in {"buy", "sell"}
        ):
            return str(item.comparators[0].value)
    return None


def _compile_delegate(method: ast.FunctionDef, *, retry_ms: int) -> dict[str, Any]:
    candidates: list[tuple[ast.If, str, str]] = []
    for statement in method.body:
        if not isinstance(statement, ast.If):
            continue
        tag = next(
            (
                str(node.comparators[0].value)
                for node in ast.walk(statement.test)
                if isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "ft_order_tag"
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and len(node.comparators) == 1
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)
            ),
            None,
        )
        targets = [
            node.value.func.attr
            for node in ast.walk(statement)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "self"
        ]
        if tag is not None and len(targets) == 1:
            candidates.append((statement, tag, targets[0]))
    if len(candidates) != 1:
        raise StrategyAnalysisError("rebuy post-de-risk transition changed")
    statement, tag, target = candidates[0]
    if not any(
        isinstance(node, ast.Name) and node.id == "count_of_exits"
        for node in ast.walk(statement.test)
    ):
        raise StrategyAnalysisError("rebuy post-de-risk exit guard changed")
    return {
        "selector": "first-exit",
        "tag_operator": "equal",
        "tag": tag,
        "target": "position-adjustment",
        "source_target": target,
        "target_entry_retry_ms": retry_ms,
        "location": _location(statement),
    }


_LADDER_ASSIGNMENTS = {
    "rebuy_mode_stakes",
    "max_sub_grinds",
    "rebuy_mode_sub_thresholds",
}


def _decision_fragment(method: ast.FunctionDef) -> ast.FunctionDef:
    assignments: dict[str, ast.Assign] = {}
    for statement in method.body:
        candidates = _conditional_ladder_assignments(statement)
        if isinstance(statement, ast.Assign):
            candidates.extend([statement])
        for candidate in candidates:
            if (
                len(candidate.targets) != 1
                or not isinstance(candidate.targets[0], ast.Name)
                or candidate.targets[0].id not in _LADDER_ASSIGNMENTS
            ):
                continue
            name = candidate.targets[0].id
            if name in assignments:
                raise StrategyAnalysisError("rebuy ladder assignments changed")
            assignments[name] = candidate
    if set(assignments) != _LADDER_ASSIGNMENTS:
        raise StrategyAnalysisError("rebuy ladder assignments changed")
    entry_candidates = [
        statement
        for statement in method.body
        if isinstance(statement, ast.If)
        and any(
            isinstance(node, ast.Name) and node.id == "sub_grind_count"
            for node in ast.walk(statement.test)
        )
        and any(
            isinstance(node, ast.Name) and node.id == "buy_amount" for node in ast.walk(statement)
        )
    ]
    derisk_candidates = [
        statement
        for statement in method.body
        if isinstance(statement, ast.If)
        and any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "derisk_enable"
            for node in ast.walk(statement.test)
        )
        and any(
            isinstance(node, ast.Name) and node.id == "ft_sell_amount"
            for node in ast.walk(statement)
        )
    ]
    if len(entry_candidates) != 1 or len(derisk_candidates) != 1:
        raise StrategyAnalysisError("rebuy decision branches changed")
    body = [
        copy.deepcopy(assignments["rebuy_mode_stakes"]),
        copy.deepcopy(assignments["max_sub_grinds"]),
        copy.deepcopy(assignments["rebuy_mode_sub_thresholds"]),
        copy.deepcopy(entry_candidates[0]),
        copy.deepcopy(derisk_candidates[0]),
        ast.Return(value=ast.Constant(value=None)),
    ]
    lowerer = _DecisionLowerer()
    lowered = [node for statement in body if (node := lowerer.visit(statement)) is not None]
    if lowerer.action_tags != 2:
        raise StrategyAnalysisError("rebuy adjustment results changed")
    fragment = ast.FunctionDef(
        name=f"__{method.name}_transition",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg=name)
                for name in (
                    "partial_sell",
                    "sub_grind_count",
                    "slice_profit_entry",
                    "slice_amount",
                    "is_futures_mode",
                    "trade_leverage",
                    "min_stake",
                    "max_stake",
                    "profit_stake",
                    "trade_amount",
                    "exit_rate",
                    "trade",
                    "last_candle",
                )
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=lowered,
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    ast.copy_location(fragment, method)
    ast.fix_missing_locations(fragment)
    return fragment


def _conditional_ladder_assignments(statement: ast.stmt) -> list[ast.Assign]:
    if not isinstance(statement, ast.If) or not statement.orelse:
        return []
    branches: list[dict[str, ast.Assign]] = []
    for body in (statement.body, statement.orelse):
        if not all(
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and item.targets[0].id in _LADDER_ASSIGNMENTS
            for item in body
        ):
            return []
        branch_map = {
            item.targets[0].id: item
            for item in body
            if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
        }
        if len(branch_map) != len(body):
            raise StrategyAnalysisError("rebuy conditional ladder assignments changed")
        branches.append(branch_map)
    if not branches[0] or branches[0].keys() != branches[1].keys():
        raise StrategyAnalysisError("rebuy conditional ladder assignments changed")
    result = []
    for name, primary in branches[0].items():
        alternate = branches[1][name]
        assignment = ast.Assign(
            targets=[copy.deepcopy(primary.targets[0])],
            value=ast.IfExp(
                test=copy.deepcopy(statement.test),
                body=copy.deepcopy(primary.value),
                orelse=copy.deepcopy(alternate.value),
            ),
        )
        result.append(ast.copy_location(assignment, statement))
    return result


class _DecisionLowerer(ast.NodeTransformer):
    """Remove observability-only calls and normalize tagged backtest returns."""

    def __init__(self) -> None:
        self.action_tags = 0

    def visit_Expr(self, node: ast.Expr) -> ast.stmt | None:
        if isinstance(node.value, ast.Call) and _call_path(node.value.func) in {
            "dp.send_msg",
            "log.info",
        }:
            return None
        raise StrategyAnalysisError("rebuy decision contains an uncompiled expression statement")

    def visit_Assign(self, node: ast.Assign) -> ast.stmt | None:
        if _is_observability_assignment(node):
            return None
        if _is_dataframe_loader(node) or _is_latest_candle_projection(node):
            return None
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "grind_profit"
        ):
            return None
        visited = self.generic_visit(node)
        if not isinstance(visited, ast.stmt):
            raise StrategyAnalysisError("rebuy assignment lowering failed")
        return visited

    def visit_If(self, node: ast.If) -> ast.stmt | None:
        if _is_empty_dataframe_guard(node) or _is_notification_branch(node):
            return None
        if isinstance(node.test, ast.Name) and node.test.id == "has_order_tags":
            if (
                len(node.body) != 1
                or not isinstance(node.body[0], ast.Return)
                or not isinstance(node.body[0].value, ast.Tuple)
                or len(node.body[0].value.elts) != 2
                or not isinstance(node.body[0].value.elts[1], ast.Constant)
                or not isinstance(node.body[0].value.elts[1].value, str)
                or len(node.orelse) != 1
                or not isinstance(node.orelse[0], ast.Return)
                or node.orelse[0].value is None
                or ast.dump(node.body[0].value.elts[0]) != ast.dump(node.orelse[0].value)
            ):
                raise StrategyAnalysisError("rebuy tagged return contract changed")
            self.action_tags += 1
            return ast.copy_location(copy.deepcopy(node.body[0]), node)
        visited = self.generic_visit(node)
        if not isinstance(visited, ast.stmt):
            raise StrategyAnalysisError("rebuy branch lowering failed")
        return visited


def _is_dataframe_loader(node: ast.Assign) -> bool:
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Tuple)
        and len(node.targets[0].elts) == 2
        and all(isinstance(item, ast.Name) for item in node.targets[0].elts)
        and [item.id for item in node.targets[0].elts if isinstance(item, ast.Name)] == ["df", "_"]
        and isinstance(node.value, ast.Call)
        and _call_path(node.value.func) == "dp.get_analyzed_dataframe"
    )


def _is_latest_candle_projection(node: ast.Assign) -> bool:
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "last_candle"
        and isinstance(node.value, ast.Subscript)
        and isinstance(node.value.value, ast.Attribute)
        and isinstance(node.value.value.value, ast.Name)
        and node.value.value.value.id == "df"
        and node.value.value.attr == "iloc"
        and isinstance(node.value.slice, ast.UnaryOp)
        and isinstance(node.value.slice.op, ast.USub)
        and isinstance(node.value.slice.operand, ast.Constant)
        and node.value.slice.operand.value == 1
    )


def _is_observability_assignment(node: ast.Assign) -> bool:
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return False
    name = node.targets[0].id
    if name == "stake_currency":
        return (
            isinstance(node.value, ast.Subscript)
            and (
                (isinstance(node.value.value, ast.Name) and node.value.value.id == "config")
                or (
                    isinstance(node.value.value, ast.Attribute)
                    and isinstance(node.value.value.value, ast.Name)
                    and node.value.value.value.id == "self"
                    and node.value.value.attr == "config"
                )
            )
            and isinstance(node.value.slice, ast.Constant)
            and node.value.slice.value == "stake_currency"
        )
    if name == "stake_fmt":
        names = [item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)]
        return (
            isinstance(node.value, ast.IfExp)
            and bool(names)
            and all(item == "stake_currency" for item in names)
        )
    if name == "send_notifications":
        return (
            isinstance(node.value, ast.UnaryOp)
            and isinstance(node.value.op, ast.Not)
            and isinstance(node.value.operand, ast.Call)
            and _call_path(node.value.operand.func) == "self.is_backtest_mode"
        )
    return False


def _is_notification_branch(node: ast.If) -> bool:
    return (
        isinstance(node.test, ast.Name)
        and node.test.id == "send_notifications"
        and not node.orelse
        and bool(node.body)
        and all(
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _call_path(statement.value.func) in {"dp.send_msg", "log.info"}
            for statement in node.body
        )
    )


def _is_empty_dataframe_guard(node: ast.If) -> bool:
    test = node.test
    return (
        not node.orelse
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return)
        and (
            node.body[0].value is None
            or (isinstance(node.body[0].value, ast.Constant) and node.body[0].value.value is None)
        )
        and isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Call)
        and isinstance(test.left.func, ast.Name)
        and test.left.func.id == "len"
        and len(test.left.args) == 1
        and isinstance(test.left.args[0], ast.Name)
        and test.left.args[0].id == "df"
        and not test.left.keywords
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Lt)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == 1
    )


def _call_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _input_contract(fragment: ast.FunctionDef) -> dict[str, Any]:
    indexed: dict[str, set[str]] = {}
    for node in ast.walk(fragment):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            indexed.setdefault(node.value.id, set()).add(node.slice.value)
    return {"indexed_fields": {name: sorted(fields) for name, fields in sorted(indexed.items())}}


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": int(getattr(node, "lineno", 0)),
        "column": int(getattr(node, "col_offset", 0)),
        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
        "end_column": int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0))),
    }
