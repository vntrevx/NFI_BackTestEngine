"""Compile source exits which run before managed route dispatch."""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping
from typing import Any

from ..errors import StrategyAnalysisError
from ..trade_ir import compile_scalar_ast_program
from .adjustment_dispatch import _same
from .system_exit_ir import _set_parameters

_PRELUDE = """
trade_is_short = trade.is_short
df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
if len(df) < 2:
    return None
last_candle = df.iloc[-1]
previous_candle_1 = df.iloc[-2]
enter_tag = 'empty'
if hasattr(trade, 'enter_tag'):
    trade_enter_tag = trade.enter_tag
    if trade_enter_tag is not None:
        enter_tag = trade_enter_tag
enter_tags = enter_tag.split()
filled_orders, filled_entries, filled_exits = filled_order_snapshot(trade)
profit_stake = 0.0
profit_ratio = 0.0
profit_current_stake_ratio = 0.0
profit_init_ratio = 0.0
profit_values = calc_total_profit(trade, filled_entries, filled_exits, current_rate)
profit_stake, profit_ratio, profit_current_stake_ratio, profit_init_ratio = profit_values
cache_backtest_profit_snapshot(
    trade, current_time, current_rate, filled_orders, filled_entries, filled_exits, profit_values
)
"""


def compile_custom_exit_prefix(
    methods: Mapping[str, ast.FunctionDef], constants: dict[str, Any],
) -> dict[str, Any]:
    source = methods["custom_exit"]
    start_node = ast.parse('system_version = trade.get_custom_data(key="system_version")').body[0]
    starts = [index for index, node in enumerate(source.body) if _same(node, start_node)]
    if len(starts) != 1:
        raise StrategyAnalysisError("NFI custom-exit system prefix is missing or ambiguous")
    start = starts[0]
    prelude = [node for node in source.body[:start] if not _self_alias(node)]
    if not _same(ast.Module(body=prelude, type_ignores=[]), ast.parse(_PRELUDE)):
        raise StrategyAnalysisError("NFI custom-exit prefix input preparation changed")
    ends = [index for index, node in enumerate(source.body) if index > start
            and _same(node, ast.parse("max_profit = 0.0").body[0])]
    if len(ends) != 1:
        raise StrategyAnalysisError("NFI custom-exit prefix boundary changed")
    end = ends[0]
    # After the prefix all returning branches must dispatch a managed exit.
    for statement in source.body[end:]:
        if not any(isinstance(node, ast.Return) for node in ast.walk(statement)):
            continue
        if (isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant)
                and statement.value.value is None):
            continue
        if not any(
            isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id.startswith(("long_exit_", "short_exit_"))
                or isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith(("long_exit_", "short_exit_"))
            ) for node in ast.walk(statement)
        ):
            raise StrategyAnalysisError("NFI custom_exit has an uncompiled trailing return")
    prefix = ast.parse("def custom_exit_prefix():\n return None").body[0]
    assert isinstance(prefix, ast.FunctionDef)
    prefix.body = copy.deepcopy(source.body[start + 1:end]) + [ast.Return(value=ast.Constant(None))]
    _set_parameters(prefix, ["trade", "current_time", "current_rate", "current_profit",
                             "last_candle", "filled_entries", "enter_tag", "enter_tags",
                             "system_version"])
    reason = copy.deepcopy(methods["_bad_trade_controller_exit_reason"])
    # In this bundle current_time is the elapsed number of seconds. The source
    # has exactly one clock read, the timedelta's total_seconds expression.
    age = _ElapsedTimeLowerer()
    age.visit(reason)
    if age.replacements != 1 or any(
        isinstance(node, ast.Name) and node.id == "current_time"
        for node in ast.walk(reason) if node not in age.replaced_nodes
    ):
        raise StrategyAnalysisError("NFI bad-trade clock inputs changed")
    geometry = copy.deepcopy(methods["_bad_trade_controller_ema_gap_and_adverse_move"])
    if ([ast.unparse(node) for node in geometry.decorator_list] != ["staticmethod"]
            or reason.decorator_list):
        raise StrategyAnalysisError("NFI bad-trade helper binding changed")
    nodes = {node.name: ast.fix_missing_locations(node) for node in (prefix, reason, geometry)}
    columns: set[str] = set()
    for method in nodes.values():
        for node in ast.walk(method):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "last_candle"):
                if (node.func.attr != "get" or len(node.args) != 1 or node.keywords
                        or not isinstance(node.args[0], ast.Constant)
                        or not isinstance(node.args[0].value, str)):
                    raise StrategyAnalysisError("NFI prefix optional candle read changed")
                columns.add(node.args[0].value)
    programs = {name: compile_scalar_ast_program(
        method, constants=constants, available_methods=set(nodes),
        extensions=frozenset({"is-finite", "mapping-get", "fixed-zero"}),
    ) for name, method in nodes.items()}
    return {
        "bundle": {"schema_version": "1.0.0", "entry": prefix.name, "programs": programs},
        "optional_columns": sorted(columns),
    }


class _ElapsedTimeLowerer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.replacements = 0
        self.replaced_nodes: set[ast.AST] = set()

    def visit_Call(self, node: ast.Call) -> ast.AST:
        expected = ast.parse(
            "(current_time - trade.open_date_utc).total_seconds()", mode="eval"
        ).body
        if _same(node, expected):
            replacement = ast.copy_location(ast.Name(id="current_time", ctx=ast.Load()), node)
            self.replacements += 1
            self.replaced_nodes.add(replacement)
            return replacement
        return self.generic_visit(node)


def _self_alias(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"
            and node.targets[0].id == node.value.attr)
