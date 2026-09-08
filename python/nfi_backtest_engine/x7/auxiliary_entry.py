"""Prove the source operands of the additional system rebuy entry action."""

from __future__ import annotations

import ast
import copy

from ..errors import StrategyAnalysisError
from .auxiliary_adjustment import same, stores


def _assignment(method: ast.FunctionDef, name: str, expression: str) -> ast.expr:
    found = [
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    if len(found) != 1 or len(stores(method, name)) != 1 or not same(found[0].value, expression):
        raise StrategyAnalysisError(f"auxiliary entry source operand changed: {name}")
    return copy.deepcopy(found[0].value)


def _mode_value(method: ast.FunctionDef, name: str, field: str) -> ast.expr:
    expression = f"self.{field}_futures if is_futures_mode else self.{field}_spot"
    writes = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    if len(writes) == 1:
        return _assignment(method, name, expression)
    if len(writes) != 2 or len(stores(method, name)) != 2:
        raise StrategyAnalysisError(f"auxiliary entry mode assignment changed: {name}")
    matching = [
        node
        for node in method.body
        if isinstance(node, ast.If)
        and same(node.test, "is_futures_mode")
        and writes[0] in node.body
        and writes[1] in node.orelse
    ]
    if (
        len(matching) != 1
        or not same(writes[0].value, f"self.{field}_futures")
        or not same(writes[1].value, f"self.{field}_spot")
    ):
        raise StrategyAnalysisError(f"auxiliary entry mode branches changed: {name}")
    return ast.parse(expression, mode="eval").body


def prove_auxiliary_entry_inputs(
    method: ast.FunctionDef, side: str
) -> tuple[dict[str, ast.expr], str]:
    helper = f"{side}_rebuy_entry_v3"
    _assignment(
        method,
        f"is_{side}_rebuy_entry",
        f"self.{helper}(last_candle, previous_candle, slice_profit, True)",
    )
    maximum = _assignment(
        method,
        "is_not_trade_max_stake_v3_1",
        "current_stake_amount < (slice_amount * self.system_v3_1_max_stake)",
    )
    _assignment(method, "rebuy_max_sub_grinds", "len(rebuy_stakes)")
    stakes = _mode_value(method, "rebuy_stakes", "system_v3_1_rebuy_stakes")
    thresholds = _mode_value(method, "rebuy_sub_thresholds", "system_v3_1_rebuy_thresholds")
    return {
        "is_not_trade_max_stake_v3_1": maximum,
        "rebuy_stakes": stakes,
        "rebuy_sub_thresholds": thresholds,
        "rebuy_max_sub_grinds": ast.Call(
            func=ast.Name(id="len", ctx=ast.Load()), args=[copy.deepcopy(stakes)], keywords=[]
        ),
    }, helper


def prove_buyback_entry_helper(method: ast.FunctionDef, constant_prefix: str) -> str:
    """Bind the actual source helper and its explicit de-risk argument."""
    family = constant_prefix.removeprefix("system_").removesuffix("_")
    helper = f"long_buyback_entry_{family}"
    _assignment(
        method,
        "is_long_buyback_entry",
        f"self.{helper}(last_candle, previous_candle, slice_profit, True)",
    )
    assignment = next(
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "is_long_buyback_entry"
    )
    if any(
        method.body.index(node) <= method.body.index(assignment)
        for node in method.body
        if any(
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Load)
            and child.id == "is_long_buyback_entry"
            for child in ast.walk(node)
        )
    ):
        raise StrategyAnalysisError("buyback helper evaluation order changed")
    return helper


class AuxiliaryOperandLowerer(ast.NodeTransformer):
    def __init__(self, aliases: dict[str, ast.expr]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id not in self.aliases:
            return node
        if not isinstance(node.ctx, ast.Load):
            raise StrategyAnalysisError("auxiliary action changes a source operand")
        return ast.copy_location(copy.deepcopy(self.aliases[node.id]), node)
