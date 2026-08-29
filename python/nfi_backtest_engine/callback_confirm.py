"""Entry-confirm callback statement and helper assembly."""

from __future__ import annotations

import ast
import hashlib
import json

from .callback_ast import _qualified_name
from .callback_confirm_expression import _compile_confirm_expression
from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject


def _lower_confirm_trade_entry(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: JsonObject,
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> JsonObject | None:
    if isinstance(node, ast.AsyncFunctionDef):
        return None
    helper_names = {
        "_handle_grind_mode",
        "_handle_top_coins_mode",
        "_handle_scalp_mode",
    }
    functions: JsonObject = {}
    for name in sorted(helper_names):
        helper = method_nodes.get(name)
        if helper is None or isinstance(helper, ast.AsyncFunctionDef):
            return None
        statements = _compile_confirm_statements(helper.body, constants=constants)
        if statements is None:
            return None
        functions[name] = {
            "parameters": [argument.arg for argument in helper.args.args[1:]],
            "statements": statements,
        }
    statements = _compile_confirm_statements(node.body, constants=constants)
    if statements is None:
        return None
    operation = {
        "opcode": "entry-confirm-program-v1",
        "statements": statements,
        "functions": functions,
    }
    return {
        "backend": "rust-entry-confirm-vm",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-entry-confirm-ast-v1",
            "helper_functions": sorted(functions),
            "program_sha256": hashlib.sha256(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }


def _compile_confirm_statements(
    statements: list[ast.stmt],
    *,
    constants: JsonObject,
) -> list[JsonObject] | None:
    compiled: list[JsonObject] = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            if (
                len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Tuple)
                and isinstance(statement.value, ast.Call)
                and _qualified_name(statement.value.func) == "self.dp.get_analyzed_dataframe"
            ):
                targets = statement.targets[0].elts
                if (
                    len(targets) != 2
                    or not isinstance(targets[0], ast.Name)
                    or not isinstance(targets[1], ast.Name)
                ):
                    return None
                compiled.append(
                    {"op": "let", "name": targets[0].id, "value": {"op": "analyzed_frame"}}
                )
                continue
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return None
            value = _compile_confirm_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "let", "name": statement.targets[0].id, "value": value})
            continue
        if isinstance(statement, ast.If):
            condition = _compile_confirm_expression(statement.test, constants=constants)
            body = _compile_confirm_statements(statement.body, constants=constants)
            otherwise = _compile_confirm_statements(statement.orelse, constants=constants)
            if condition is None or body is None or otherwise is None:
                return None
            compiled.append(
                {"op": "if", "condition": condition, "then": body, "otherwise": otherwise}
            )
            continue
        if isinstance(statement, ast.Return):
            value = _compile_confirm_expression(statement.value, constants=constants)
            if value is None:
                return None
            compiled.append({"op": "return", "value": value})
            continue
        if isinstance(statement, ast.Expr):
            if _is_log_call(statement.value):
                compiled.append({"op": "log_noop"})
                continue
            if not (
                isinstance(statement.value, ast.Call)
                and _qualified_name(statement.value.func) == "self._remove_profit_target"
                and len(statement.value.args) == 1
                and not statement.value.keywords
            ):
                return None
            pair = _compile_confirm_expression(statement.value.args[0], constants=constants)
            if pair is None:
                return None
            compiled.append({"op": "clear_profit_target", "pair": pair})
            continue
        return None
    return compiled


def _is_log_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _qualified_name(node.func) in {
        "log.info",
        "log.warning",
    }
