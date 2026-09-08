"""Source-proven scalar writes around NFI's configurable parameter loops."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from .errors import StrategyAnalysisError

MANAGED_SCALARS = frozenset({"exit_profit_only", "startup_candle_count"})
_PROFIT = """
if ("exit_profit_only" in strategy_config and strategy_config["exit_profit_only"]) or (
    "sell_profit_only" in strategy_config and strategy_config["sell_profit_only"]
):
    self.exit_profit_only = True
"""
_STARTUP = """
if exchange_name in ["okx", "okex"]:
    self.startup_candle_count = 480
elif exchange_name == "kraken":
    self.startup_candle_count = 710
elif exchange_name == "bybit":
    self.startup_candle_count = 199
elif exchange_name == "bitget":
    self.startup_candle_count = 499
elif exchange_name == "bingx":
    self.startup_candle_count = 499
"""


def constructor_scalar_stages(
    initializer: ast.FunctionDef,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = [ast.parse(template).body[0] for template in (_PROFIT, _STARTUP)]
    matches = [
        [
            node
            for node in initializer.body
            if ast.dump(node, include_attributes=False)
            == ast.dump(template, include_attributes=False)
        ]
        for template in expected
    ]
    if any(len(nodes) != 1 for nodes in matches):
        raise StrategyAnalysisError("source constructor scalar stages changed")
    before, after = (nodes[0] for nodes in matches)
    parameter_assignment = next(
        (
            node
            for node in initializer.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "nfi_parameters"
        ),
        None,
    )
    if parameter_assignment is None or not (
        initializer.body.index(before)
        < initializer.body.index(parameter_assignment)
        < initializer.body.index(after)
    ):
        raise StrategyAnalysisError("source constructor scalar precedence changed")
    covered = {id(node) for block in (before, after) for node in ast.walk(block)}
    for node in ast.walk(initializer):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in MANAGED_SCALARS
            and id(node) not in covered
        ):
            raise StrategyAnalysisError("source constructor has an additional scalar dependency")
    exchange_expression = ast.parse('strategy_config["exchange"]["name"]', mode="eval").body
    assignments = [
        node
        for node in ast.walk(initializer)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "exchange_name"
    ]
    writes = [
        node
        for node in ast.walk(initializer)
        if isinstance(node, ast.Name)
        and node.id == "exchange_name"
        and not isinstance(node.ctx, ast.Load)
    ]
    if (
        len(assignments) != 2
        or len(writes) != 2
        or any(
            ast.dump(node.value, include_attributes=False)
            != ast.dump(exchange_expression, include_attributes=False)
            for node in assignments
        )
    ):
        raise StrategyAnalysisError("source constructor exchange binding changed")
    early = (
        {"exit_profit_only": True}
        if (config.get("exit_profit_only") or config.get("sell_profit_only"))
        else {}
    )
    startup = {"okx": 480, "okex": 480, "kraken": 710, "bybit": 199, "bitget": 499, "bingx": 499}
    exchange = config.get("exchange")
    name = exchange.get("name") if isinstance(exchange, Mapping) else None
    late = (
        {"startup_candle_count": startup[name]} if isinstance(name, str) and name in startup else {}
    )
    return early, late
