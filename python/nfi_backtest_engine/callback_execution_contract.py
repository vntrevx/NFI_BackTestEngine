"""Source-bound callback order, visibility, and transactional contract IR."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from .callback_contract import JsonObject
from .callback_execution_dependencies import _execution_closure
from .callback_execution_policy import (
    CALLBACK_EXECUTION_IR_VERSION,
    FREQTRADE_CALLBACK_CONTRACT_FINGERPRINT,
    FREQTRADE_CALLBACK_CONTRACT_VERSION,
    _callback_policy,
)
from .callback_source_identity import _fingerprint
from .callback_source_routes import _ordered_nodes
from .errors import StrategyAnalysisError
from .strategy import STRATEGY_CALLBACKS

__all__ = ["CALLBACK_EXECUTION_IR_VERSION", "compile_callback_execution_ir"]


def compile_callback_execution_ir(
    analysis: dict[str, Any],
    *,
    trading_mode: str,
    run_mode: str,
) -> dict[str, Any]:
    """Compile reviewed scheduler semantics plus exact source predicates.

    The program is descriptive: executable callback lowerers remain separately
    fail-closed. Unknown callback families are rejected rather than inheriting a
    scheduler policy by resemblance.
    """
    if trading_mode not in {"spot", "futures"}:
        raise StrategyAnalysisError("callback execution IR mode must be spot or futures")
    if run_mode not in {"backtest", "hyperopt"}:
        raise StrategyAnalysisError("callback execution IR requires backtest or hyperopt")
    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1 or not isinstance(source, dict):
        raise StrategyAnalysisError("callback execution IR requires one selected strategy")
    path_value, expected_sha = source.get("path"), source.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_sha, str):
        raise StrategyAnalysisError("callback execution IR requires a hash-bound source")
    path = Path(path_value).resolve()
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError("callback execution IR source cannot be read") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise StrategyAnalysisError("callback execution IR source hash differs from analysis")
    tree = ast.parse(text, filename=str(path), type_comments=True)
    strategy_name = strategies[0].get("name")
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy_name
        ),
        None,
    )
    if class_node is None:
        raise StrategyAnalysisError("callback execution IR strategy class disappeared")
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    callback_nodes = [
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in STRATEGY_CALLBACKS
    ]
    present = {node.name for node in callback_nodes}
    callbacks: list[JsonObject] = []
    for source_order, node in enumerate(callback_nodes):
        policy = _callback_policy(
            node.name,
            present=present,
            trading_mode=trading_mode,
            run_mode=run_mode,
        )
        if policy is None:
            raise StrategyAnalysisError(
                f"callback execution IR has no pinned execution contract for {node.name}"
            )
        closure = _execution_closure(node.name, methods)
        predicates, predicate_ids = _source_predicates(closure, methods)
        callbacks.append(
            {
                "name": node.name,
                "source_order": source_order,
                "reachable_methods": closure,
                "source_predicates": predicates,
                "source_return_classes": _return_classes(closure, methods),
                "custom_state_deltas": _custom_state_deltas(
                    closure,
                    methods,
                    predicate_ids,
                ),
                **policy,
            }
        )
    document: JsonObject = {
        "schema_version": CALLBACK_EXECUTION_IR_VERSION,
        "freqtrade_contract": {
            "version": FREQTRADE_CALLBACK_CONTRACT_VERSION,
            "fingerprint": FREQTRADE_CALLBACK_CONTRACT_FINGERPRINT,
        },
        "source": {"path": str(path), "sha256": expected_sha},
        "selected_class": strategy_name,
        "trading_mode": trading_mode,
        "run_mode": run_mode,
        "callbacks": callbacks,
    }
    document["fingerprint"] = _fingerprint(document)
    return document


def _source_predicates(
    closure: list[str],
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> tuple[list[JsonObject], dict[int, str]]:
    records: list[JsonObject] = []
    identifiers: dict[int, str] = {}
    for method_name in closure:
        method = methods[method_name]
        control_tests = [
            node.test
            for node in _ordered_nodes(method, (ast.If, ast.IfExp, ast.While))
        ]
        returned_tests = [
            node.value
            for node in _ordered_nodes(method, ast.Return)
            if isinstance(node.value, ast.BoolOp | ast.Compare)
        ]
        tests = sorted(
            {id(test): test for test in (*control_tests, *returned_tests)}.values(),
            key=lambda test: (test.lineno, test.col_offset, test.end_lineno, test.end_col_offset),
        )
        for test in tests:
            identifier = f"p{len(records) + 1}"
            identifiers[id(test)] = identifier
            canonical = ast.dump(test, annotate_fields=True, include_attributes=False)
            records.append(
                {
                    "id": identifier,
                    "producer_method": method_name,
                    "expression": ast.unparse(test),
                    "ast_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                    "source_order": len(records),
                }
            )
    return records, identifiers


def _return_classes(
    closure: list[str],
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[str]:
    result: list[str] = []
    for method_name in closure:
        for returned in _ordered_nodes(methods[method_name], ast.Return):
            for value in _expression_classes(returned.value):
                if value not in result:
                    result.append(value)
    return result


def _expression_classes(node: ast.AST | None) -> list[str]:
    if node is None or isinstance(node, ast.Constant) and node.value is None:
        return ["null"]
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return ["true" if node.value else "false"]
        if isinstance(node.value, int | float):
            return ["number"]
        if isinstance(node.value, str):
            return ["string"]
    if isinstance(node, ast.JoinedStr):
        return ["string"]
    if isinstance(node, ast.Tuple):
        return ["tuple"]
    if isinstance(node, ast.IfExp):
        return [*_expression_classes(node.body), *_expression_classes(node.orelse)]
    return ["dynamic"]


def _custom_state_deltas(
    closure: list[str],
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    predicate_ids: dict[int, str],
) -> list[JsonObject]:
    result: list[JsonObject] = []
    for method_name in closure:
        visitor = _StateDeltaVisitor(method_name, predicate_ids)
        visitor.visit(methods[method_name])
        result.extend(visitor.records)
    for source_order, record in enumerate(result):
        record["source_order"] = source_order
    return result


class _StateDeltaVisitor(ast.NodeVisitor):
    def __init__(self, method_name: str, predicate_ids: dict[int, str]) -> None:
        self.method_name = method_name
        self.predicate_ids = predicate_ids
        self.predicates: list[str] = []
        self.records: list[JsonObject] = []

    def _visit_guarded(self, node: ast.If | ast.IfExp | ast.While) -> None:
        identifier = self.predicate_ids[id(node.test)]
        self.visit(node.test)
        self.predicates.append(identifier)
        for child in node.body if not isinstance(node, ast.IfExp) else [node.body]:
            self.visit(child)
        self.predicates.pop()
        children = node.orelse if not isinstance(node, ast.IfExp) else [node.orelse]
        for child in children:
            self.visit(child)

    visit_If = _visit_guarded
    visit_IfExp = _visit_guarded
    visit_While = _visit_guarded

    def visit_Call(self, node: ast.Call) -> None:
        leaf = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if leaf in {"set_custom_data", "delete_custom_data"}:
            key_node = node.args[0] if node.args else next(
                (item.value for item in node.keywords if item.arg == "key"),
                None,
            )
            key = (
                key_node.value
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
                else "<dynamic>"
            )
            self.records.append(
                {
                    "operation": "set" if leaf == "set_custom_data" else "delete",
                    "key": key,
                    "producer_method": self.method_name,
                    "predicate_ids": list(self.predicates),
                }
            )
        self.generic_visit(node)
