"""Compile active-system stop and cached-target helpers from their source AST."""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program
from .adjustment_dispatch import _same, active_system_flags, prove_active_system
from .managed_exit_ir import _compile_tag_matcher, _self_aliases

_FLAGS = {
    "is_backtest": True, "is_system_v4": True,
    "is_system_v3": False, "is_system_v3_1": False, "is_system_v3_2": False,
}


def compile_system_exit_programs(
    methods: Mapping[str, ast.FunctionDef], constants: dict[str, Any],
) -> dict[str, Any]:
    system = prove_active_system(methods, constants)
    result: dict[str, Any] = {"system_version": system}
    for side in ("long", "short"):
        source = methods[f"{side}_exit_stoploss"]
        method = copy.deepcopy(source)
        cost = ast.parse(
            "entry_cost = filled_entries[0].safe_filled * filled_entries[0].safe_price"
        ).body[0]
        if sum(_same(statement, cost) for statement in method.body) != 1:
            raise StrategyAnalysisError("NFI common stop first-entry cost changed")
        method.body = [statement for statement in method.body if not _same(statement, cost)]
        lowerer = ActiveSystemLowerer(constants)
        lowerer.visit(method)
        parameters = ["mode_name", "profit_stake", "entry_cost", "trade",
                      "is_futures_mode", "last_candle", "previous_candle_1"]
        _set_parameters(method, parameters)
        result[f"{side}_stop"] = compile_scalar_ast_program(
            ast.fix_missing_locations(method), constants=dict(constants)
        )
    result["profit_target"] = _compile_profit_target(methods["exit_profit_target"], constants)
    return result


def _set_parameters(method: ast.FunctionDef, names: list[str]) -> None:
    method.args = ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=name) for name in names],
        kwonlyargs=[], kw_defaults=[], defaults=[],
    )


class ActiveSystemLowerer(ast.NodeTransformer):
    """Specialize only proven system/run-mode reads; retain dynamic operands."""

    def __init__(self, constants: Mapping[str, Any]) -> None:
        self.constants = constants
        self.flags = {name: value for name, value in active_system_flags(constants).items()
                      if name in _FLAGS}

    def _block(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        result = []
        for statement in statements:
            value = self.visit(statement)
            if value is not None:
                result.extend(value if isinstance(value, list) else [value])
            if result and isinstance(result[-1], ast.Return):
                break
        return result

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.body = self._block(node.body)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
        if len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in _FLAGS:
                expected = ("self.is_backtest_mode()" if target.id == "is_backtest"
                            else f"self.{target.id}(trade)")
                if not _same(node.value, ast.parse(expected, mode="eval").body):
                    raise StrategyAnalysisError("NFI exit system predicate changed")
                return None
            if isinstance(target, ast.Tuple) and any(
                isinstance(item, ast.Name) and item.id in _FLAGS for item in target.elts
            ):
                expected = ast.parse(
                    "is_system_v3, is_system_v3_1, is_system_v3_2 = "
                    "self.get_system_version_flags(trade)"
                ).body[0]
                if not _same(node, expected):
                    raise StrategyAnalysisError("NFI exit system tuple changed")
                return None
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in _FLAGS:
            if not isinstance(node.ctx, ast.Load):
                raise StrategyAnalysisError("NFI exit system predicate is reassigned")
            return ast.copy_location(ast.Constant(value=self.flags[node.id]), node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if (isinstance(node.value, ast.Name) and node.value.id == "self"
                and node.attr == "is_futures_mode"):
            return ast.copy_location(ast.Name(id="is_futures_mode", ctx=ast.Load()), node)
        return self.generic_visit(node)

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
        node.test = self.visit(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return self._block(node.body if node.test.value else node.orelse)
        node.body = self._block(node.body)
        node.orelse = self._block(node.orelse)
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node.test = self.visit(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return self.visit(node.body if node.test.value else node.orelse)
        return self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        values = []
        is_and = isinstance(node.op, ast.And)
        for value in node.values:
            lowered = self.visit(value)
            if isinstance(lowered, ast.Constant) and isinstance(lowered.value, bool):
                if lowered.value != is_and:
                    values.append(lowered)
                    break
            else:
                values.append(lowered)
        if not values:
            return ast.copy_location(ast.Constant(value=is_and), node)
        if len(values) == 1:
            return values[0]
        node.values = values
        return node


def _compile_profit_target(source: ast.FunctionDef, constants: dict[str, Any]) -> dict[str, Any]:
    method = copy.deepcopy(source)
    # The order scan is represented by explicit source-derived runtime operands.
    scan = next((node for node in method.body if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.left, ast.Name)
                 and node.test.left.id == "previous_sell_reason"), None)
    if scan is None:
        raise StrategyAnalysisError("NFI target de-risk scan is missing")
    policy = _derisk_scan_policy(scan)
    method.body.remove(scan)
    aliases = {
        "remove_profit_target": "self._remove_profit_target",
        "select_filled_orders": "trade.select_filled_orders",
        "is_derisk": "False",
    }
    for name, value in aliases.items():
        expected = ast.parse(f"{name} = {value}").body[0]
        if sum(_same(node, expected) for node in method.body) != 1:
            raise StrategyAnalysisError(f"NFI target alias changed: {name}")
        method.body = [node for node in method.body if not _same(node, expected)]
    lowerer = _TargetLowerer(constants, _self_aliases(source))
    lowerer.visit(method)
    method.body.insert(0, ast.parse("remove_target = False").body[0])
    parameters = ["mode_name", "trade", "profit_stake", "profit_ratio", "profit_init_ratio",
                  "profit_current_stake_ratio", "previous_profit", "previous_sell_reason",
                  "last_candle", "previous_candle_1", "is_derisk", "is_futures_mode",
                  *lowerer.predicates]
    _set_parameters(method, parameters)
    return {
        "program": compile_scalar_ast_program(
            ast.fix_missing_locations(method), constants=constants
        ),
        "predicates": lowerer.predicates,
        "derisk": policy,
    }


def _derisk_scan_policy(scan: ast.If) -> dict[str, Any]:
    expected = ast.parse('''
if previous_sell_reason in [f"exit_{mode_name}_stoploss_doom", f"exit_{mode_name}_stoploss",
                           f"exit_{mode_name}_stoploss_u_e"]:
    filled_entries = select_filled_orders(trade.entry_side)
    filled_exits = select_filled_orders(trade.exit_side)
    first_filled_entry = filled_entries[0]
    has_order_tags = hasattr(first_filled_entry, "ft_order_tag")
    for order in filled_exits:
        order_tag = ""
        if has_order_tags:
            if order.ft_order_tag is not None:
                order_tag = order.ft_order_tag.partition(" ")[0]
        if order_tag in ["d", "d1", "derisk_level_1", "derisk_level_2", "derisk_level_3"]:
            is_derisk = True
            break
    if not is_derisk:
        is_derisk = trade.amount < (first_filled_entry.safe_filled * 0.95)
''').body[0]
    # The bounded scan's shape is part of the contract, not its literal policy.
    actual = copy.deepcopy(scan)
    tags = next((node for node in ast.walk(actual) if isinstance(node, ast.Compare)
                 and isinstance(node.left, ast.Name) and node.left.id == "order_tag"), None)
    fallback = actual.body[-1]
    if (tags is None or len(tags.comparators) != 1
            or not isinstance(tags.comparators[0], ast.List)
            or not isinstance(fallback, ast.If) or len(fallback.body) != 1
            or not isinstance(fallback.body[0], ast.Assign)):
        raise StrategyAnalysisError("NFI target de-risk scan changed")
    raw_tags = tags.comparators[0]
    if not all(isinstance(node, ast.Constant) and isinstance(node.value, str)
               and node.value for node in raw_tags.elts):
        raise StrategyAnalysisError("NFI target de-risk tags are not literals")
    threshold = fallback.body[0].value
    if (not isinstance(threshold, ast.Compare) or len(threshold.comparators) != 1
            or not isinstance(threshold.comparators[0], ast.BinOp)
            or not isinstance(threshold.comparators[0].right, ast.Constant)):
        raise StrategyAnalysisError("NFI target de-risk amount policy changed")
    ratio = threshold.comparators[0].right.value
    if isinstance(ratio, bool) or not isinstance(ratio, int | float) or not 0 < ratio <= 1:
        raise StrategyAnalysisError("NFI target de-risk amount ratio is invalid")
    tags.comparators[0] = ast.parse(
        '["d", "d1", "derisk_level_1", "derisk_level_2", "derisk_level_3"]', mode="eval"
    ).body
    threshold.comparators[0].right = ast.Constant(value=0.95)
    if not _same(actual, expected):
        raise StrategyAnalysisError("NFI target de-risk scan cannot be represented")
    return {"order_tags": [node.value for node in raw_tags.elts if isinstance(node, ast.Constant)],
            "amount_ratio": ratio}


class _TargetLowerer(ActiveSystemLowerer):
    def __init__(self, constants: Mapping[str, Any], aliases: dict[str, str]) -> None:
        super().__init__(constants)
        self.aliases = aliases
        self.predicates: dict[str, dict[str, Any]] = {}

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        if isinstance(node.value, ast.Call) and _same(
            node.value, ast.parse("remove_profit_target(pair)", mode="eval").body
        ):
            return ast.copy_location(ast.parse("remove_target = True").body[0], node)
        return self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> ast.AST:
        if not isinstance(node.value, ast.Tuple) or len(node.value.elts) != 2:
            raise StrategyAnalysisError("NFI target return shape changed")
        node.value.elts.append(ast.Name(id="remove_target", ctx=ast.Load()))
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        matcher = _compile_tag_matcher(node, self.aliases, self.constants)
        if matcher is not None:
            name = f"tag_predicate_{len(self.predicates)}"
            self.predicates[name] = matcher
            return ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)
        return self.generic_visit(node)
