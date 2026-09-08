"""Bind the legacy short entry decision to its source call and argument contract."""

from __future__ import annotations

import ast
from collections.abc import Mapping

from ..errors import StrategyAnalysisError


def source_legacy_entry_program(
    method: ast.FunctionDef,
    methods: Mapping[str, ast.FunctionDef],
) -> str:
    assignments = [
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "is_short_grind_entry"
    ]
    writes = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Name)
        and node.id == "is_short_grind_entry"
        and not isinstance(node.ctx, ast.Load)
    ]
    if len(assignments) != 1 or len(writes) != 1:
        raise StrategyAnalysisError("legacy entry decision binding changed")
    call = assignments[0].value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    ):
        raise StrategyAnalysisError("legacy entry decision is not a source method")
    name = call.func.attr
    expected = ast.parse(
        f"self.{name}(last_candle, previous_candle, slice_profit, True)", mode="eval"
    ).body
    if ast.dump(call, include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise StrategyAnalysisError("legacy entry decision arguments changed")
    helper = methods.get(name)
    if helper is None or (
        [arg.arg for arg in helper.args.args]
        != ["self", "last_candle", "previous_candle", "slice_profit", "is_derisk"]
        or helper.decorator_list
        or helper.args.defaults
        or helper.args.kwonlyargs
        or helper.args.posonlyargs
        or helper.args.vararg
        or helper.args.kwarg
    ):
        raise StrategyAnalysisError("legacy entry decision signature changed")
    return name
