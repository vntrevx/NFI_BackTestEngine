"""Typed expression lowering for executable callback programs."""

from __future__ import annotations

import ast
import copy
from typing import Any, NoReturn, Protocol, TypeGuard


class ExpressionContext(Protocol):
    locals: set[str]

    def fail(self, code: str, node: ast.AST, message: str) -> NoReturn: ...
    def register(self, name: str, node: ast.AST) -> str: ...
    def inline_static(self, name: str, call: ast.Call) -> dict[str, Any]: ...


def literal(value: object) -> dict[str, Any]:
    return {"op": "literal", "value": value}


def compile_expression(node: ast.AST, context: ExpressionContext, depth: int = 0) -> dict[str, Any]:
    if depth > 64:
        context.fail("CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", node, "expression depth exceeds 64")

    def child(item: ast.AST) -> dict[str, Any]:
        return compile_expression(item, context, depth + 1)

    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, bool | int | float | str)
    ):
        return literal(node.value)
    if isinstance(node, ast.Name):
        if node.id in context.locals:
            return {"op": "read_local", "name": node.id}
        if node.id in {"True", "False", "None"}:
            return literal({"True": True, "False": False, "None": None}[node.id])
        return {"op": "read_input", "name": node.id}
    if isinstance(node, ast.Attribute):
        path = _attribute_path(node)
        if path and path[0] == "self" and len(path) == 2:
            return {"op": "read_register", "register_id": context.register(path[1], node)}
        if path and path[0] in {"trade", "order"}:
            return {"op": f"read_{path[0]}", "field": ".".join(path[1:])}
        if path == ["current_time", "minute"]:
            return {
                "op": "binary",
                "operator": "div",
                "left": {
                    "op": "binary",
                    "operator": "mod",
                    "left": {"op": "read_local", "name": "current_time"},
                    "right": literal(3_600_000),
                },
                "right": literal(60_000),
            }
        if path == ["dataframe", "empty"]:
            return {"op": "read_input", "name": "callback_dataframe_empty"}
        if path and path[0] == "self" and "config" in path:
            return {"op": "read_input", "name": "config"}
    if isinstance(node, ast.Dict):
        fields: list[dict[str, Any]] = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                expanded = child(value)
                if expanded.get("op") != "read_local":
                    context.fail(
                        "CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION",
                        value,
                        "record merge is not bounded",
                    )
                fields.append({"spread": expanded})
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                fields.append({"name": key.value, "value": child(value)})
            else:
                context.fail(
                    "CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", key, "record key must be literal"
                )
        return {"op": "record", "fields": fields}
    if isinstance(node, ast.List | ast.Tuple):
        return {
            "op": "list" if isinstance(node, ast.List) else "tuple",
            "items": [child(x) for x in node.elts],
        }
    if isinstance(node, ast.Subscript):
        config_name = _config_subscript(node)
        if config_name is not None:
            return {"op": "read_input", "name": config_name}
        return {"op": "index", "value": child(node.value), "index": child(node.slice)}
    if isinstance(node, ast.UnaryOp):
        operator = {ast.Not: "not", ast.USub: "neg", ast.UAdd: "pos"}.get(type(node.op))
        if operator:
            return {"op": "unary", "operator": operator, "value": child(node.operand)}
    if isinstance(node, ast.BinOp):
        operator = {
            ast.Add: "add",
            ast.Sub: "sub",
            ast.Mult: "mul",
            ast.Div: "div",
            ast.Mod: "mod",
        }.get(type(node.op))
        if operator:
            return {
                "op": "binary",
                "operator": operator,
                "left": child(node.left),
                "right": child(node.right),
            }
    if isinstance(node, ast.BoolOp):
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "values": [child(x) for x in node.values],
        }
    if isinstance(node, ast.Compare):
        operators = {
            ast.Eq: "eq",
            ast.NotEq: "ne",
            ast.Lt: "lt",
            ast.LtE: "le",
            ast.Gt: "gt",
            ast.GtE: "ge",
            ast.Is: "is",
            ast.IsNot: "is_not",
            ast.In: "in",
            ast.NotIn: "not_in",
        }
        return {
            "op": "compare",
            "left": child(node.left),
            "comparisons": [
                {"operator": operators[type(op)], "right": child(value)}
                for op, value in zip(node.ops, node.comparators, strict=True)
            ],
        }
    if isinstance(node, ast.IfExp):
        return {
            "op": "choose",
            "condition": child(node.test),
            "then": child(node.body),
            "otherwise": child(node.orelse),
        }
    if isinstance(node, ast.Call):
        return _compile_call(node, context, depth)
    context.fail(
        "CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", node, f"unsupported {type(node).__name__}"
    )
    raise AssertionError("unreachable")


def _compile_call(node: ast.Call, context: ExpressionContext, depth: int) -> dict[str, Any]:
    def child(item: ast.AST) -> dict[str, Any]:
        return compile_expression(item, context, depth + 1)

    if isinstance(node.func, ast.Name) and node.func.id in {
        "min",
        "max",
        "len",
        "int",
        "float",
        "bool",
    }:
        name = node.func.id
        if (
            name == "len"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "dataframe"
        ):
            return {"op": "read_input", "name": "visible_rows"}
        return {"op": "call_builtin", "name": name, "args": [child(arg) for arg in node.args]}
    path = _attribute_path(node.func)
    if (
        path
        and len(path) == 3
        and path[0] == "self"
        and path[2] == "get"
        and len(node.args) in {1, 2}
    ):
        register = {
            "op": "read_register",
            "register_id": context.register(path[1], node),
        }
        return {
            "op": "map_get",
            "value": register,
            "key": child(node.args[0]),
            "default": child(node.args[1]) if len(node.args) == 2 else literal(None),
        }
    if path and path[-1] == "get_custom_data" and path[0] == "trade":
        key = _literal_key(node, context)
        default = child(node.args[1]) if len(node.args) > 1 else literal(None)
        return {"op": "read_custom_state", "key": key, "default": default}
    if isinstance(node.func, ast.Attribute) and node.func.attr == "timestamp":
        if any(
            isinstance(item, ast.Name) and item.id == "dataframe"
            for item in ast.walk(node.func.value)
        ):
            return {"op": "read_input", "name": "last_visible_timestamp_seconds"}
        return {"op": "timestamp_ms", "value": child(node.func.value)}
    if path and len(path) == 2 and path[0] == "self":
        if path[1] in {
            "bot_loop_start",
            "leverage",
            "custom_stake_amount",
            "confirm_trade_entry",
            "order_filled",
            "adjust_trade_position",
            "custom_stoploss",
            "custom_exit",
            "confirm_trade_exit",
        }:
            context.fail(
                "CALLBACK_PROGRAM_UNBOUNDED_CONTROL_FLOW", node, "callback recursion is forbidden"
            )
        return context.inline_static(path[1], node)
    context.fail(
        "CALLBACK_PROGRAM_UNSUPPORTED_EXPRESSION", node, "call is not in the bounded vocabulary"
    )
    raise AssertionError("unreachable")


def _literal_key(node: ast.Call, context: ExpressionContext) -> str:
    key = (
        node.args[0]
        if node.args
        else next((x.value for x in node.keywords if x.arg == "key"), None)
    )
    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
        context.fail(
            "CALLBACK_PROGRAM_DYNAMIC_CUSTOM_STATE_KEY", node, "custom-state key must be literal"
        )
    return key.value


def _attribute_path(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id, *reversed(parts)]
    return None


class ReplaceNames(ast.NodeTransformer):
    def __init__(self, values: dict[str, ast.expr]) -> None:
        self.values = values

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return copy.deepcopy(self.values.get(node.id, node))


def node_path(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call) \
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "super":
        return ["super", node.attr]
    return _attribute_path(node)


def self_attribute(node: ast.AST) -> TypeGuard[ast.Attribute]:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def literal_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.Dict):
        return "record"
    if isinstance(node, ast.List | ast.Tuple):
        return "list"
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, bool):
        return "bool"
    if isinstance(node.value, int):
        return "i64"
    if isinstance(node.value, float):
        return "f64"
    if isinstance(node.value, str):
        return "string"
    return "null" if node.value is None else None


def custom_key(call: ast.Call, context: ExpressionContext) -> str:
    return _literal_key(call, context)


def valid_emit(node: ast.FunctionDef) -> bool:
    paths = [_attribute_path(x.func) for x in ast.walk(node) if isinstance(x, ast.Call)]
    names = {path[-1] for path in paths if path}
    return {"timestamp", "int", "dumps", "print"} <= names and not any(
        isinstance(x, ast.For | ast.While) for x in ast.walk(node)
    )


def dataframe_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (_attribute_path(node.func) or [])[-1:] == [
        "get_analyzed_dataframe"
    ]


def _config_subscript(node: ast.Subscript) -> str | None:
    keys: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Subscript):
        if not isinstance(current.slice, ast.Constant) or not isinstance(current.slice.value, str):
            return None
        keys.append(current.slice.value)
        current = current.value
    path = _attribute_path(current)
    if path == ["self", "config"]:
        return "config." + ".".join(reversed(keys))
    return None
