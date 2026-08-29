"""Call and generator forms supported by confirmation callbacks."""

from __future__ import annotations

import ast
from typing import Protocol

from .callback_ast import _qualified_name
from .callback_contract import JsonObject


class _ExpressionCompiler(Protocol):
    def __call__(
        self,
        node: ast.AST | None,
        *,
        constants: JsonObject,
    ) -> JsonObject | None: ...


def _compile_confirm_call(
    node: ast.Call,
    *,
    constants: JsonObject,
    compile_expression: _ExpressionCompiler,
) -> JsonObject | None:
    name = _qualified_name(node.func)
    if name in {"all", "any"} and len(node.args) == 1 and not node.keywords:
        membership = _compile_confirm_membership_generator(
            node.args[0], constants=constants, compile_expression=compile_expression
        )
        if membership is None:
            return None
        return {"op": "all_in" if name == "all" else "any_in", **membership}
    if name == "len" and len(node.args) == 1 and not node.keywords:
        value = compile_expression(node.args[0], constants=constants)
        return {"op": "length", "value": value} if value is not None else None
    if name == "sum" and len(node.args) == 1 and not node.keywords:
        return _compile_confirm_count_generator(
            node.args[0], constants=constants, compile_expression=compile_expression
        )
    if name == "Trade.get_trades_proxy":
        if len(node.keywords) != 1 or node.keywords[0].arg != "is_open":
            return None
        return {"op": "open_trades"}
    if name == "Trade.get_open_trade_count" and not node.args and not node.keywords:
        return {"op": "open_trade_count"}
    if name == "self.dp.get_analyzed_dataframe":
        return {"op": "analyzed_frame"}
    if name == "trade.calc_profit_ratio" and len(node.args) == 1 and not node.keywords:
        rate = compile_expression(node.args[0], constants=constants)
        return {"op": "trade_profit_ratio", "rate": rate} if rate is not None else None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "split":
        if node.args or node.keywords:
            return None
        value = compile_expression(node.func.value, constants=constants)
        return {"op": "split_words", "value": value} if value is not None else None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "partition":
        if (
            len(node.args) != 1
            or node.keywords
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            return None
        value = compile_expression(node.func.value, constants=constants)
        return (
            {"op": "partition", "value": value, "separator": node.args[0].value}
            if value is not None
            else None
        )
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr in {"_handle_grind_mode", "_handle_top_coins_mode", "_handle_scalp_mode"}
    ):
        arguments = [compile_expression(argument, constants=constants) for argument in node.args]
        if node.keywords or any(argument is None for argument in arguments):
            return None
        return {"op": "call", "name": node.func.attr, "arguments": arguments}
    return None


def _compile_confirm_membership_generator(
    node: ast.AST,
    *,
    constants: JsonObject,
    compile_expression: _ExpressionCompiler,
) -> JsonObject | None:
    if (
        not isinstance(node, ast.GeneratorExp)
        or len(node.generators) != 1
        or node.generators[0].ifs
        or node.generators[0].is_async
        or not isinstance(node.generators[0].target, ast.Name)
        or not isinstance(node.elt, ast.Compare)
        or len(node.elt.ops) != 1
        or not isinstance(node.elt.ops[0], ast.In)
        or len(node.elt.comparators) != 1
        or not isinstance(node.elt.left, ast.Name)
        or node.elt.left.id != node.generators[0].target.id
    ):
        return None
    items = compile_expression(
        node.generators[0].iter,
        constants=constants,
    )
    container = compile_expression(
        node.elt.comparators[0],
        constants=constants,
    )
    if items is None or container is None:
        return None
    return {"items": items, "container": container}


def _compile_confirm_count_generator(
    node: ast.AST,
    *,
    constants: JsonObject,
    compile_expression: _ExpressionCompiler,
) -> JsonObject | None:
    if (
        not isinstance(node, ast.GeneratorExp)
        or len(node.generators) != 1
        or node.generators[0].is_async
        or not isinstance(node.generators[0].target, ast.Name)
        or not isinstance(node.elt, ast.Constant)
        or node.elt.value != 1
    ):
        return None
    iterable = compile_expression(
        node.generators[0].iter,
        constants=constants,
    )
    filters = [compile_expression(item, constants=constants) for item in node.generators[0].ifs]
    if iterable is None or any(item is None for item in filters):
        return None
    return {
        "op": "count",
        "name": node.generators[0].target.id,
        "iterable": iterable,
        "filters": filters,
    }
