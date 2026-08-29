"""Backtest exit-confirm callback specialization and proofs."""

from __future__ import annotations

import ast
import copy
import hashlib
import json

from .callback_ast import _qualified_name
from .callback_confirm import _compile_confirm_statements
from .callback_contract import CALLBACK_LOWERING_VERSION, JsonObject
from .callback_lifecycle import _runmode_not_in_values


def _lower_confirm_trade_exit(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    constants: JsonObject,
    method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    run_mode: str | None,
) -> JsonObject | None:
    if isinstance(node, ast.AsyncFunctionDef) or run_mode not in {"backtest", "hyperopt"}:
        return None
    backtest_helper = method_nodes.get("is_backtest_mode")
    hold_helper = method_nodes.get("_should_hold_trade")
    remove_helper = method_nodes.get("_remove_profit_target")
    if (
        backtest_helper is None
        or hold_helper is None
        or remove_helper is None
        or not _proves_backtest_mode(backtest_helper)
        or not _proves_backtest_hold_disabled(hold_helper, run_mode=run_mode)
        or not _proves_profit_target_remove(remove_helper)
    ):
        return None
    transformed = copy.deepcopy(node)
    transformed = _BacktestExitTransformer().visit(transformed)
    ast.fix_missing_locations(transformed)
    effective_constants = dict(constants)
    if effective_constants.get("exit_profit_only") is False:
        effective_constants.setdefault("exit_profit_offset", 0.0)
    statements = _compile_confirm_statements(
        _through_first_unconditional_return(transformed.body),
        constants=effective_constants,
    )
    if statements is None:
        return None
    operation = {
        "opcode": "exit-confirm-program-v1",
        "statements": statements,
        "functions": {},
    }
    return {
        "backend": "rust-exit-confirm-vm",
        "executable_in_rust": True,
        "operation": operation,
        "proof": {
            "compiler_version": CALLBACK_LOWERING_VERSION,
            "matcher": "bounded-backtest-exit-confirm-ast-v1",
            "backtest_hold_behavior": "disabled",
            "profit_target_effect": "clear-pair",
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


def _through_first_unconditional_return(statements: list[ast.stmt]) -> list[ast.stmt]:
    for index, statement in enumerate(statements):
        if isinstance(statement, ast.Return):
            return statements[: index + 1]
    return statements


class _BacktestExitTransformer(ast.NodeTransformer):
    def visit_Call(self, node: ast.Call) -> ast.AST:
        transformed = self.generic_visit(node)
        if not isinstance(transformed, ast.Call):
            return transformed
        name = _qualified_name(transformed.func)
        if name == "self.is_backtest_mode":
            return ast.copy_location(ast.Constant(value=True), transformed)
        if name == "self._should_hold_trade":
            return ast.copy_location(ast.Constant(value=False), transformed)
        return transformed


def _proves_backtest_mode(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return False
    value = statements[0].value
    return (
        isinstance(value, ast.Compare)
        and len(value.ops) == 1
        and isinstance(value.ops[0], ast.In)
        and len(value.comparators) == 1
        and isinstance(value.left, ast.Attribute)
        and value.left.attr == "value"
        and _qualified_name(value.left.value) == "self.dp.runmode"
        and isinstance(value.comparators[0], ast.List | ast.Tuple)
        and {
            item.value
            for item in value.comparators[0].elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        == {"backtest", "hyperopt"}
    )


def _proves_backtest_hold_disabled(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    run_mode: str,
) -> bool:
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(item, ast.Call) for item in ast.walk(statement)):
                return False
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if not isinstance(statement, ast.If):
            return False
        excluded = _runmode_not_in_values(statement.test)
        return (
            excluded is not None
            and run_mode not in excluded
            and len(statement.body) == 1
            and isinstance(statement.body[0], ast.Return)
            and isinstance(statement.body[0].value, ast.Constant)
            and statement.body[0].value.value is False
        )
    return False


def _proves_profit_target_remove(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    calls = {_qualified_name(item.func) for item in ast.walk(node) if isinstance(item, ast.Call)}
    return calls == {"target_profit_cache.data.pop", "target_profit_cache.save"}
