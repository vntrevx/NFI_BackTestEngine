"""Static environment and state-write parsing for order callbacks."""

from __future__ import annotations

import ast
from enum import Enum, auto

from .callback_ast import _is_none_expression, _qualified_name
from .callback_contract import JsonObject, JsonValue


class _Unknown(Enum):
    TOKEN = auto()


_UNKNOWN = _Unknown.TOKEN


def _record_static_alias(
    statement: ast.Assign,
    environment: JsonObject,
    constants: JsonObject,
) -> bool:
    if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
        return False
    name = statement.targets[0].id
    value = statement.value
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
        and value.attr in constants
    ):
        environment[name] = constants[value.attr]
        return True
    if (
        name == "set_custom_data"
        and isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "trade"
        and value.attr == "set_custom_data"
    ):
        environment[name] = "trade.set_custom_data"
        return True
    return False


def _is_first_successful_entry_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "trade"
        and node.left.attr == "nr_of_successful_entries"
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == 1
    )


def _select_static_if(
    node: ast.If,
    environment: JsonObject,
) -> list[ast.stmt] | None:
    selected = _static_bool(node.test, environment)
    if selected is None:
        return None
    statements = node.body if selected else node.orelse
    if len(statements) == 1 and isinstance(statements[0], ast.If):
        return _select_static_if(statements[0], environment)
    return statements


def _static_bool(node: ast.AST, environment: JsonObject) -> bool | None:
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
        and isinstance(node.ops[0], ast.Eq | ast.NotEq)
    ):
        left = _environment_value(node.left, environment)
        right = _environment_value(node.comparators[0], environment)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        equal = left == right
        return equal if isinstance(node.ops[0], ast.Eq) else not equal
    return None


def _environment_value(node: ast.AST, environment: JsonObject) -> JsonValue | _Unknown:
    if isinstance(node, ast.Name):
        return environment.get(node.id, _UNKNOWN)
    if isinstance(node, ast.Constant):
        value = node.value
        return value if value is None or isinstance(value, bool | int | float | str) else _UNKNOWN
    return _UNKNOWN


def _literal_write_block(
    statements: list[ast.stmt],
    environment: JsonObject,
    constants: JsonObject,
) -> list[JsonObject] | None:
    writes: list[JsonObject] = []
    for statement in statements:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            return None
        write = _literal_write(statement.value, environment, constants)
        if write is None:
            return None
        writes.append(write)
    return writes


def _literal_write(
    call: ast.Call,
    environment: JsonObject,
    constants: JsonObject,
) -> JsonObject | None:
    if not _is_set_custom_data_call(call) or call.args:
        return None
    keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
    if set(keywords) != {"key", "value"}:
        return None
    key = _literal_value(keywords["key"], environment, constants)
    value = _literal_value(keywords["value"], environment, constants)
    if not isinstance(key, str) or value is _UNKNOWN:
        return None
    if value is not None and not isinstance(value, bool | int | float | str):
        return None
    return {"key": key, "value": value}


def _literal_value(
    node: ast.AST,
    environment: JsonObject,
    constants: JsonObject,
) -> JsonValue | _Unknown:
    if isinstance(node, ast.Constant):
        value = node.value
        return value if value is None or isinstance(value, bool | int | float | str) else _UNKNOWN
    if isinstance(node, ast.Name):
        return environment.get(node.id, _UNKNOWN)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return constants.get(node.attr, _UNKNOWN)
    return _UNKNOWN


def _extract_order_tag_actions(
    statements: list[ast.stmt],
    environment: JsonObject,
    constants: JsonObject,
) -> dict[str, list[JsonObject]]:
    effectful = [
        statement
        for statement in statements
        if any(
            isinstance(item, ast.Call) and _is_set_custom_data_call(item)
            for item in ast.walk(statement)
        )
    ]
    if len(effectful) != 1 or not isinstance(effectful[0], ast.If):
        return {}
    if not any(_is_none_order_tag_return(item) for item in statements):
        return {}
    actions: dict[str, list[JsonObject]] = {}
    current: ast.If | None = effectful[0]
    while current is not None:
        modes = _order_mode_values(current.test)
        writes = _literal_write_block(current.body, environment, constants)
        if modes is None or writes is None or not writes:
            return {}
        for mode in modes:
            if mode in actions:
                return {}
            actions[mode] = writes
        if not current.orelse:
            current = None
        elif len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
            current = current.orelse[0]
        else:
            return {}
    return dict(sorted(actions.items()))


def _is_none_order_tag_return(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Is)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "order_tag"
        and len(node.test.comparators) == 1
        and _is_none_expression(node.test.comparators[0])
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Return)
        and _is_none_expression(node.body[0].value)
    )


def _order_mode_values(node: ast.AST) -> list[str] | None:
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.In)
        or len(node.comparators) != 1
        or not isinstance(node.left, ast.Name)
        or node.left.id != "order_mode"
        or not isinstance(node.comparators[0], ast.List | ast.Tuple | ast.Set)
    ):
        return None
    values = []
    for item in node.comparators[0].elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.append(item.value)
    return values


def _is_set_custom_data_call(call: ast.Call) -> bool:
    return _qualified_name(call.func) in {"set_custom_data", "trade.set_custom_data"}
