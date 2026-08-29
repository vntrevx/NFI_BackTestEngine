"""Recursive predicate and value compiler for confirmation callbacks."""

from __future__ import annotations

import ast

from .callback_confirm_calls import _compile_confirm_call
from .callback_contract import JsonObject, JsonValue


def _compile_confirm_expression(
    node: ast.AST | None,
    *,
    constants: JsonObject,
) -> JsonObject | None:
    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, bool | int | float | str)
    ):
        return {"op": "literal", "value": node.value}
    if isinstance(node, ast.Name):
        return {"op": "variable", "name": node.id}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "is_futures_mode"
    ):
        # NFI initializes this convenience flag before the backtest starts.
        # The class-level analysis value is only the spot default; freezing it
        # would make a futures run reject liquidation-risk stop exits.
        return {"op": "config_value", "name": "is_futures"}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in constants
    ):
        value = constants[node.attr]
        return {"op": "literal", "value": value} if _confirm_literal(value) else None
    if isinstance(node, ast.Attribute):
        value = _compile_confirm_expression(node.value, constants=constants)
        if value is None:
            return None
        if node.attr == "iloc":
            return value
        return {"op": "field", "value": value, "name": node.attr}
    if isinstance(node, ast.List | ast.Tuple):
        values = []
        for item in node.elts:
            compiled = _compile_confirm_expression(item, constants=constants)
            if compiled is None or compiled.get("op") != "literal":
                return None
            values.append(compiled["value"])
        return {"op": "literal", "value": values}
    if isinstance(node, ast.Subscript):
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "config"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return {"op": "config_value", "name": node.slice.value}
        value = _compile_confirm_expression(node.value, constants=constants)
        index = _compile_confirm_expression(node.slice, constants=constants)
        if value is None or index is None:
            return None
        return {"op": "index", "value": value, "index": index}
    if isinstance(node, ast.UnaryOp):
        value = _compile_confirm_expression(node.operand, constants=constants)
        if value is None:
            return None
        if isinstance(node.op, ast.USub):
            return {"op": "negative", "value": value}
        if isinstance(node.op, ast.Not):
            return {"op": "not", "value": value}
        return None
    if isinstance(node, ast.BinOp):
        left = _compile_confirm_expression(node.left, constants=constants)
        right = _compile_confirm_expression(node.right, constants=constants)
        binary = (
            "add"
            if isinstance(node.op, ast.Add)
            else "subtract"
            if isinstance(node.op, ast.Sub)
            else "multiply"
            if isinstance(node.op, ast.Mult)
            else "divide"
            if isinstance(node.op, ast.Div)
            else None
        )
        if binary is None or left is None or right is None:
            return None
        return {"op": binary, "left": left, "right": right}
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        values = [_compile_confirm_expression(value, constants=constants) for value in node.values]
        if any(value is None for value in values):
            return None
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "values": values,
        }
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _compile_confirm_expression(node.left, constants=constants)
        right = _compile_confirm_expression(node.comparators[0], constants=constants)
        compare = (
            "equal"
            if isinstance(node.ops[0], ast.Eq | ast.Is)
            else "not_equal"
            if isinstance(node.ops[0], ast.NotEq | ast.IsNot)
            else "greater"
            if isinstance(node.ops[0], ast.Gt)
            else "greater_equal"
            if isinstance(node.ops[0], ast.GtE)
            else "less"
            if isinstance(node.ops[0], ast.Lt)
            else "less_equal"
            if isinstance(node.ops[0], ast.LtE)
            else "contains"
            if isinstance(node.ops[0], ast.In)
            else None
        )
        if compare is None or left is None or right is None:
            return None
        if compare == "contains":
            return {"op": "contains", "container": right, "value": left}
        return {"op": compare, "left": left, "right": right}
    if isinstance(node, ast.Call):
        return _compile_confirm_call(
            node,
            constants=constants,
            compile_expression=_compile_confirm_expression,
        )
    return None


def _confirm_literal(value: JsonValue) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, bool | int | float | str) for item in value
    )
