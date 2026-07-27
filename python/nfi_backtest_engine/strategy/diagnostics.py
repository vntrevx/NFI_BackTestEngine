"""Fail-closed source diagnostics for unsupported strategy constructs."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import STRATEGY_CALLBACKS
from .inventory import _qualified_name

_DYNAMIC_CALLS = {"compile", "eval", "exec", "__import__", "globals", "locals"}
_DYNAMIC_ATTRIBUTE_CALLS = {"setattr", "delattr"}

class _DiagnosticVisitor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.diagnostics: list[dict[str, Any]] = []

    def scan(self, tree: ast.AST) -> list[dict[str, Any]]:
        for node, function_name in _iter_nodes_with_function(tree):
            if isinstance(node, ast.Import):
                self._check_import(node)
            elif isinstance(node, ast.ImportFrom):
                self._check_import_from(node)
            elif isinstance(node, ast.Call):
                self._check_call(node, function_name)
        return self.diagnostics

    def _check_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "importlib" or alias.name.startswith("importlib."):
                self._add(node, "DYNAMIC_IMPORT", "importlib is not allowed in compiled strategies")

    def _check_import_from(self, node: ast.ImportFrom) -> None:
        if node.module == "importlib" or (node.module or "").startswith("importlib."):
            self._add(node, "DYNAMIC_IMPORT", "importlib is not allowed in compiled strategies")
        if any(alias.name == "*" for alias in node.names):
            self._add(node, "STAR_IMPORT", "star imports make strategy dependencies ambiguous")

    def _check_call(self, node: ast.Call, function_name: str | None) -> None:
        name = _qualified_name(node.func)
        leaf = name.split(".")[-1] if name else None
        if leaf in _DYNAMIC_CALLS:
            self._add(node, "DYNAMIC_EXECUTION", f"{leaf}() cannot be compiled exactly")
        elif leaf in _DYNAMIC_ATTRIBUTE_CALLS:
            self._dynamic_attribute(node, function_name, f"{leaf}()")
        elif leaf == "getattr":
            if len(node.args) < 2 or not (
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
            ):
                self._dynamic_attribute(node, function_name, "dynamic getattr()")
        elif leaf == "shift" and node.args and _negative_number(node.args[0]):
            self._add(
                node,
                "LOOKAHEAD_NEGATIVE_SHIFT",
                "negative dataframe shift reads future candles",
            )
        elif leaf == "rolling":
            for keyword in node.keywords:
                if (
                    keyword.arg == "center"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    self._add(
                        node,
                        "LOOKAHEAD_CENTERED_WINDOW",
                        "centered rolling windows read future candles",
                    )

    def _dynamic_attribute(
        self,
        node: ast.AST,
        function_name: str | None,
        operation: str,
    ) -> None:
        compile_time_methods = {
            *STRATEGY_CALLBACKS,
            "populate_indicators",
            "populate_entry_trend",
            "populate_exit_trend",
        }
        if function_name in compile_time_methods:
            self._add(
                node,
                "DYNAMIC_ATTRIBUTE",
                f"{operation} cannot be compiled exactly in {function_name}()",
            )
        else:
            self._add(
                node,
                "DYNAMIC_ATTRIBUTE_INIT",
                f"{operation} requires effective-config freezing during preparation",
                severity="warning",
            )

    def _add(
        self,
        node: ast.AST,
        code: str,
        message: str,
        *,
        severity: str = "error",
    ) -> None:
        self.diagnostics.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "location": {"path": str(self.path), **_location(node)},
            }
        )


def _negative_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    )


def _location(node: ast.AST) -> dict[str, int]:
    return {
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _document_diagnostic(path: Path, code: str, message: str) -> dict[str, Any]:
    return {
        "severity": "error",
        "code": code,
        "message": message,
        "location": {
            "path": str(path),
            "line": 1,
            "column": 0,
            "end_line": 1,
            "end_column": 0,
        },
    }

def _iter_nodes_with_function(tree: ast.AST) -> Any:
    stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
    while stack:
        node, function_name = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function_name = node.name
        yield node, function_name
        children = list(ast.iter_child_nodes(node))
        stack.extend((child, function_name) for child in reversed(children))
