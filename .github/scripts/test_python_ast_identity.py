#!/usr/bin/env python3
"""Verify that reviewed callback identities are stable across supported Python."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]
PACKAGE = ROOT / "python/nfi_backtest_engine"
EXPECTED = "d09ebf44530dc027a248c4f5c20bf0135bf525e06bba73d74f12912b97b8a4a6"


def _namespace(name: str, path: Path) -> None:
    module = ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def main() -> None:
    # Loading the pure compiler modules directly keeps this compatibility
    # check independent of pandas, Arrow, and the native extension.
    _namespace("nfi_backtest_engine", PACKAGE)
    _namespace("nfi_backtest_engine.x7", PACKAGE / "x7")
    routes = importlib.import_module("nfi_backtest_engine.x7.routes")

    method = ast.parse(
        """
def route(self, value: float) -> tuple:
    if value >= 0.5:
        return True, "exit"
    return False, None
"""
    ).body[0]
    if not isinstance(method, ast.FunctionDef):
        raise AssertionError("AST fixture did not produce a function")
    actual = routes._method_ast_sha256(method, remove_statement_index=None)
    if actual != EXPECTED:
        raise AssertionError(f"callback AST identity changed: expected {EXPECTED}, got {actual}")


if __name__ == "__main__":
    main()
