"""Custom-stake callback statement lowering."""

from __future__ import annotations

import ast
import hashlib
import json

from .callback_ast import _is_none_expression
from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject
from .callback_stake_expression import _compile_stake_expression


def _lower_custom_stake_amount(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: JsonObject,
) -> JsonObject | None:
    if isinstance(node, ast.AsyncFunctionDef):
        return None
    nested_functions = [
        statement for statement in node.body if isinstance(statement, ast.FunctionDef)
    ]
    if len(nested_functions) != 1 or not _is_scaled_stake_helper(nested_functions[0]):
        return None
    statements = [
        statement for statement in node.body if not isinstance(statement, ast.FunctionDef)
    ]
    program = _compile_stake_statements(statements, constants=constants)
    if program is None:
        return None
    program_identity = {
        "opcode": "custom-stake-program-v1",
        "statements": program,
    }
    return {
        "backend": "rust-stake-vm",
        "executable_in_rust": True,
        "operation": program_identity,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-stake-ast-v1",
            "statement_count": len(program),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    program_identity,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _is_scaled_stake_helper(node: ast.FunctionDef) -> bool:
    if (
        node.name != "scaled_stake"
        or len(node.args.args) != 1
        or node.args.args[0].arg != "stake_multiplier"
        or len(node.body) != 2
        or not isinstance(node.body[0], ast.Assign)
        or not isinstance(node.body[1], ast.Return)
    ):
        return False
    assignment = node.body[0]
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "stake"
        or not isinstance(assignment.value, ast.BinOp)
        or not isinstance(assignment.value.op, ast.Mult)
        or not isinstance(assignment.value.left, ast.Name)
        or assignment.value.left.id != "proposed_stake"
        or not isinstance(assignment.value.right, ast.Name)
        or assignment.value.right.id != "stake_multiplier"
    ):
        return False
    value = node.body[1].value
    if not (
        isinstance(value, ast.IfExp)
        and isinstance(value.body, ast.Name)
        and value.body.id == "stake"
        and isinstance(value.orelse, ast.Name)
        and value.orelse.id == "min_stake"
    ):
        return False
    minimum_test = value.test
    if isinstance(minimum_test, ast.BoolOp) and isinstance(minimum_test.op, ast.Or):
        if len(minimum_test.values) != 2:
            return False
        none_test, minimum_test = minimum_test.values
        if not (
            isinstance(none_test, ast.Compare)
            and isinstance(none_test.left, ast.Name)
            and none_test.left.id == "min_stake"
            and len(none_test.ops) == 1
            and isinstance(none_test.ops[0], ast.Is)
            and len(none_test.comparators) == 1
            and _is_none_expression(none_test.comparators[0])
        ):
            return False
    return (
        isinstance(minimum_test, ast.Compare)
        and len(minimum_test.ops) == 1
        and isinstance(minimum_test.ops[0], ast.Gt)
        and isinstance(minimum_test.left, ast.Name)
        and minimum_test.left.id == "stake"
        and len(minimum_test.comparators) == 1
        and isinstance(minimum_test.comparators[0], ast.Name)
        and minimum_test.comparators[0].id == "min_stake"
    )


def _compile_stake_statements(
    statements: list[ast.stmt],
    *,
    constants: JsonObject,
) -> list[JsonObject] | None:
    compiled: list[JsonObject] = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return None
            value = _compile_stake_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "let", "name": statement.targets[0].id, "value": value})
            continue
        if isinstance(statement, ast.If):
            condition = _compile_stake_expression(statement.test, constants=constants)
            body = _compile_stake_statements(statement.body, constants=constants)
            otherwise = _compile_stake_statements(statement.orelse, constants=constants)
            if condition is None or body is None or otherwise is None:
                return None
            compiled.append(
                {"op": "if", "condition": condition, "then": body, "otherwise": otherwise}
            )
            continue
        if isinstance(statement, ast.For):
            if (
                not isinstance(statement.target, ast.Name)
                or statement.orelse
                or statement.type_comment is not None
            ):
                return None
            iterable = _compile_stake_expression(statement.iter, constants=constants)
            body = _compile_stake_statements(statement.body, constants=constants)
            if iterable is None or body is None:
                return None
            compiled.append(
                {"op": "for", "name": statement.target.id, "iterable": iterable, "body": body}
            )
            continue
        if isinstance(statement, ast.Return):
            value = _compile_stake_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "return", "value": value})
            continue
        return None
    return compiled
