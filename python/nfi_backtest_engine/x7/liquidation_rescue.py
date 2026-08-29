"""Source-bound legacy Grind liquidation-rescue policy."""

from __future__ import annotations

import ast
import re
from typing import NotRequired, TypedDict

from ..errors import StrategyAnalysisError

_RESCUE_NAME = re.compile(r"^gd(?P<level>[1-9][0-9]*)_liquidation_rescue_eligible$")


class LegacyLiquidationRescuePolicy(TypedDict):
    """Typed literals, comparisons, and state identity from one side's branch."""

    side: NotRequired[str]
    cluster_level: int
    loss_threshold: float
    profit_comparison: NotRequired[str]
    liquidation_multiplier: float
    liquidation_comparison: NotRequired[str]
    used_state_key: str


def _legacy_liquidation_rescue_policy(
    method: ast.FunctionDef,
) -> LegacyLiquidationRescuePolicy | None:
    """Compile the one-shot long liquidation rescue without a tag lookup."""

    assignments = [
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and _RESCUE_NAME.fullmatch(statement.targets[0].id) is not None
    ]
    if not assignments:
        if any(
            isinstance(node, ast.Name) and _RESCUE_NAME.fullmatch(node.id) is not None
            for node in ast.walk(method)
        ):
            raise StrategyAnalysisError("legacy liquidation rescue assignment changed")
        return None
    if len(assignments) != 1:
        raise StrategyAnalysisError("legacy liquidation rescue assignment changed")
    assignment = assignments[0]
    target = assignment.targets[0]
    assert isinstance(target, ast.Name)
    match = _RESCUE_NAME.fullmatch(target.id)
    assert match is not None
    if not isinstance(assignment.value, ast.BoolOp) or not isinstance(assignment.value.op, ast.And):
        raise StrategyAnalysisError("legacy liquidation rescue predicate changed")

    terms = assignment.value.values
    if len(terms) != 5:
        raise StrategyAnalysisError("legacy liquidation rescue predicate changed")
    if not isinstance(terms[0], ast.Name) or terms[0].id != "is_futures":
        raise StrategyAnalysisError("legacy liquidation rescue market predicate changed")
    match method.name:
        case "long_grind_adjust_trade_position":
            side = "long"
            comparison = ast.Lt
            comparison_name = "less-than"
        case "short_grind_adjust_trade_position":
            side = "short"
            comparison = ast.Gt
            comparison_name = "greater-than"
        case _:
            raise StrategyAnalysisError("legacy liquidation rescue callback side changed")
    loss_threshold = _comparison_literal(
        [terms[1]],
        left_name="slice_profit_entry",
        operator=comparison,
    )
    if (side == "long" and loss_threshold >= 0.0) or (
        side == "short" and loss_threshold <= 0.0
    ):
        raise StrategyAnalysisError("legacy liquidation rescue profit predicate changed")
    if not _has_liquidation_presence([terms[2]]):
        raise StrategyAnalysisError("legacy liquidation rescue presence predicate changed")
    liquidation_multiplier = _liquidation_multiplier(
        [terms[3]], operator=comparison
    )
    if (side == "long" and liquidation_multiplier <= 1.0) or (
        side == "short" and not 0.0 < liquidation_multiplier < 1.0
    ):
        raise StrategyAnalysisError("legacy liquidation rescue proximity predicate changed")
    state_key = _unused_state_key([terms[4]])
    if not _has_one_shot_update(
        method,
        target.id,
        state_key,
        signal_name=f"is_{side}_grind_entry",
    ):
        raise StrategyAnalysisError("legacy liquidation rescue one-shot update changed")
    policy: LegacyLiquidationRescuePolicy = {
        "cluster_level": int(match.group("level")),
        "loss_threshold": loss_threshold,
        "liquidation_multiplier": liquidation_multiplier,
        "used_state_key": state_key,
    }
    if side == "short":
        policy["side"] = side
        policy["profit_comparison"] = comparison_name
        policy["liquidation_comparison"] = comparison_name
    return policy


def _comparison_literal(
    terms: list[ast.expr],
    *,
    left_name: str,
    operator: type[ast.cmpop],
) -> float:
    for term in terms:
        if (
            isinstance(term, ast.Compare)
            and isinstance(term.left, ast.Name)
            and term.left.id == left_name
            and len(term.ops) == 1
            and isinstance(term.ops[0], operator)
            and len(term.comparators) == 1
            and (value := _number(term.comparators[0])) is not None
        ):
            return value
    raise StrategyAnalysisError("legacy liquidation rescue profit predicate changed")


def _liquidation_multiplier(
    terms: list[ast.expr], *, operator: type[ast.cmpop]
) -> float:
    for term in terms:
        if (
            isinstance(term, ast.Compare)
            and isinstance(term.left, ast.Name)
            and term.left.id == "current_rate"
            and len(term.ops) == 1
            and isinstance(term.ops[0], operator)
            and len(term.comparators) == 1
            and isinstance(term.comparators[0], ast.BinOp)
            and isinstance(term.comparators[0].op, ast.Mult)
            and _is_trade_liquidation(term.comparators[0].left)
            and (value := _number(term.comparators[0].right)) is not None
        ):
            return value
    raise StrategyAnalysisError("legacy liquidation rescue proximity predicate changed")


def _has_liquidation_presence(terms: list[ast.expr]) -> bool:
    return any(
        isinstance(term, ast.Compare)
        and _is_trade_liquidation(term.left)
        and len(term.ops) == 1
        and isinstance(term.ops[0], ast.IsNot)
        and len(term.comparators) == 1
        and isinstance(term.comparators[0], ast.Constant)
        and term.comparators[0].value is None
        for term in terms
    )


def _unused_state_key(terms: list[ast.expr]) -> str:
    for term in terms:
        if (
            isinstance(term, ast.Compare)
            and isinstance(term.left, ast.Call)
            and _call_attribute(term.left, "get_custom_data")
            and len(term.ops) == 1
            and isinstance(term.ops[0], ast.Is)
            and len(term.comparators) == 1
            and isinstance(term.comparators[0], ast.Constant)
            and term.comparators[0].value is None
            and (key := _keyword_string(term.left, "key")) is not None
        ):
            return key
    raise StrategyAnalysisError("legacy liquidation rescue state predicate changed")


def _has_one_shot_update(
    method: ast.FunctionDef,
    predicate_name: str,
    state_key: str,
    *,
    signal_name: str,
) -> bool:
    for branch in (node for node in ast.walk(method) if isinstance(node, ast.If)):
        names = {node.id for node in ast.walk(branch.test) if isinstance(node, ast.Name)}
        if {predicate_name, signal_name} - names:
            continue
        if not branch.body or not isinstance(branch.body[0], ast.If):
            return False
        update_branch = branch.body[0]
        if (
            not isinstance(update_branch.test, ast.Name)
            or update_branch.test.id != predicate_name
            or len(update_branch.body) != 1
            or not isinstance(update_branch.body[0], ast.Expr)
            or not isinstance(update_branch.body[0].value, ast.Call)
        ):
            return False
        call = update_branch.body[0].value
        return (
            _call_attribute(call, "set_custom_data")
            and _keyword_string(call, "key") == state_key
            and _keyword_bool(call, "value") is True
        )
    return False


def _call_attribute(call: ast.Call, name: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == name
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "trade"
    )


def _keyword_string(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if (
            keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _keyword_bool(call: ast.Call, name: str) -> bool | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, bool) else None
    return None


def _is_trade_liquidation(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "liquidation_price"
        and isinstance(node.value, ast.Name)
        and node.value.id == "trade"
    )


def _number(node: ast.expr) -> float | None:
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return -float(node.operand.value)
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    return None
