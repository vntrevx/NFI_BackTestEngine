"""Predicate and value compiler for stake callbacks."""

from __future__ import annotations

import ast

from .callback_ast import _qualified_name
from .callback_contract import JsonObject, JsonValue


def _compile_stake_expression(
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
        and node.attr in constants
    ):
        value = constants[node.attr]
        if _stake_literal(value):
            return {"op": "literal", "value": value}
        return None
    if isinstance(node, ast.List | ast.Tuple):
        values = []
        for item in node.elts:
            compiled = _compile_stake_expression(item, constants=constants)
            if compiled is None or compiled.get("op") != "literal":
                return None
            values.append(compiled["value"])
        if not _stake_literal(values):
            return None
        return {"op": "literal", "value": values}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _compile_stake_expression(node.left, constants=constants)
        right = _compile_stake_expression(node.right, constants=constants)
        return (
            {"op": "multiply", "left": left, "right": right}
            if left is not None and right is not None
            else None
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        values = [_compile_stake_expression(value, constants=constants) for value in node.values]
        if any(value is None for value in values):
            return None
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "values": values,
        }
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _compile_stake_expression(node.left, constants=constants)
        right = _compile_stake_expression(node.comparators[0], constants=constants)
        compare_op = (
            "equal"
            if isinstance(node.ops[0], ast.Eq | ast.Is)
            else "greater"
            if isinstance(node.ops[0], ast.Gt)
            else None
        )
        if compare_op is None or left is None or right is None:
            return None
        return {"op": compare_op, "left": left, "right": right}
    if isinstance(node, ast.IfExp):
        condition = _compile_stake_expression(node.test, constants=constants)
        body = _compile_stake_expression(node.body, constants=constants)
        otherwise = _compile_stake_expression(node.orelse, constants=constants)
        if condition is None or body is None or otherwise is None:
            return None
        return {
            "op": "choose",
            "condition": condition,
            "then": body,
            "otherwise": otherwise,
        }
    if isinstance(node, ast.Subscript):
        value = _compile_stake_expression(node.value, constants=constants)
        index = _compile_stake_expression(node.slice, constants=constants)
        if value is None or index is None:
            return None
        return {"op": "index", "value": value, "index": index}
    if isinstance(node, ast.Call):
        name = _qualified_name(node.func)
        if name == "entry_tag.split" and not node.args and not node.keywords:
            return {
                "op": "split_words",
                "value": {"op": "variable", "name": "entry_tag"},
            }
        if name == "scaled_stake" and len(node.args) == 1 and not node.keywords:
            multiplier = _compile_stake_expression(node.args[0], constants=constants)
            return (
                {"op": "stake_clamp_min", "multiplier": multiplier}
                if multiplier is not None
                else None
            )
        if name in {"all", "any"} and len(node.args) == 1 and not node.keywords:
            membership = _compile_membership_generator(
                node.args[0],
                constants=constants,
            )
            if membership is None:
                return None
            return {
                "op": "all_in" if name == "all" else "any_in",
                **membership,
            }
    return None


def _compile_membership_generator(
    node: ast.AST,
    *,
    constants: JsonObject,
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
    items = _compile_stake_expression(
        node.generators[0].iter,
        constants=constants,
    )
    container = _compile_stake_expression(
        node.elt.comparators[0],
        constants=constants,
    )
    if items is None or container is None:
        return None
    return {"items": items, "container": container}


def _stake_literal(value: JsonValue) -> bool:
    if isinstance(value, bool | int | float | str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, bool | int | float | str) for item in value
    )
