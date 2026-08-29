"""Source identity validation for X7 trade-manager assembly."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import StrategyAnalysisError


@dataclass(frozen=True)
class TradeManagerSource:
    strategy: dict[str, Any]
    source_sha256: str
    methods: dict[str, ast.FunctionDef]
    method_records: dict[str, dict[str, Any]]
    constants: dict[str, Any]


def load_trade_manager_source(analysis: dict[str, Any]) -> TradeManagerSource | None:
    """Validate the selected strategy and return its hash-bound AST context."""

    strategies = analysis.get("strategies")
    source = analysis.get("source")
    if not isinstance(strategies, list) or len(strategies) != 1:
        raise StrategyAnalysisError("NFI trade manager requires one selected strategy")
    strategy = strategies[0]
    if not isinstance(strategy, dict):
        raise StrategyAnalysisError("NFI trade manager strategy record is invalid")
    strategy_name = strategy.get("name")
    if not isinstance(strategy_name, str):
        raise StrategyAnalysisError("NFI trade manager strategy name is invalid")
    if not strategy_name.startswith("NostalgiaForInfinityX7"):
        return None
    if not isinstance(source, dict):
        raise StrategyAnalysisError("NFI trade manager requires hash-bound source")
    source_path = source.get("path")
    source_sha256 = source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise StrategyAnalysisError("NFI trade manager source identity is invalid")

    path = Path(source_path).resolve()
    try:
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyAnalysisError(f"NFI trade manager source cannot be read: {path}") from exc
    if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
        raise StrategyAnalysisError("NFI trade manager source hash differs from analysis")
    tree = ast.parse(text, filename=str(path), type_comments=True)
    class_node = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == strategy_name
        ),
        None,
    )
    if class_node is None:
        raise StrategyAnalysisError("NFI trade manager strategy class disappeared")
    methods = {
        item.name: item for item in class_node.body if isinstance(item, ast.FunctionDef)
    }
    method_records = {
        method["name"]: method
        for method in strategy.get("methods", [])
        if isinstance(method, dict) and isinstance(method.get("name"), str)
    }
    constants = strategy.get("constants")
    if not isinstance(constants, dict):
        raise StrategyAnalysisError("NFI trade manager constants are invalid")
    return TradeManagerSource(
        strategy=strategy,
        source_sha256=source_sha256,
        methods=methods,
        method_records=method_records,
        constants=constants,
    )
