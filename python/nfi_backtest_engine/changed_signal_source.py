"""Derive the compact Signal 562 predicate from authenticated upstream source."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from .errors import SpecValidationError

_VARIABLES: Final = (
    "rsi_3_15m_gt_15",
    "rsi_3_1h_gt_20",
    "rsi_3_4h_gt_25",
    "aroonu_14_1h_gt_0",
)
_COLUMNS: Final = {
    "rsi_3_15m": "RSI_3_15m",
    "rsi_3_1h": "RSI_3_1h",
    "rsi_3_4h": "RSI_3_4h",
    "aroonu_14_1h": "AROONU_14_1h",
}


def upstream_signal_562_terms(source_path: Path) -> list[str]:
    """Resolve the changed OR clause and each nearest source assignment."""
    return upstream_signal_562_terms_from_bytes(source_path.read_bytes())


def upstream_signal_562_terms_from_bytes(source: bytes) -> list[str]:
    """Resolve Signal 562 terms from independently obtained source bytes."""
    tree = ast.parse(source.decode("utf-8"))
    branch = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and ast.unparse(node.test) == "short_entry_condition_index == 562"
        ),
        None,
    )
    if branch is None:
        raise SpecValidationError("authenticated upstream Signal 562 branch is absent")
    clause = next(
        (
            node
            for node in ast.walk(branch)
            if isinstance(node, ast.BinOp)
            and tuple(ast.unparse(value) for value in _or_values(node)) == _VARIABLES
        ),
        None,
    )
    if clause is None:
        raise SpecValidationError("authenticated upstream Signal 562 changed clause differs")
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.lineno >= branch.lineno:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _VARIABLES:
                assignments[target.id] = node.value
    terms = []
    for variable in _VARIABLES:
        value = assignments.get(variable)
        if (
            not isinstance(value, ast.Compare)
            or len(value.ops) != 1
            or not isinstance(value.ops[0], ast.Gt)
            or not isinstance(value.left, ast.Name)
            or value.left.id not in _COLUMNS
            or len(value.comparators) != 1
        ):
            raise SpecValidationError("authenticated upstream Signal 562 term differs")
        terms.append(f"{_COLUMNS[value.left.id]} > {ast.unparse(value.comparators[0])}")
    return terms


def compact_predicate(source_path: Path) -> tuple[str, list[str], dict[str, int | str | None]]:
    """Extract the compact strategy predicate and its source span."""
    return compact_predicate_from_bytes(source_path.read_bytes())


def compact_predicate_from_bytes(
    source: bytes,
) -> tuple[str, list[str], dict[str, int | str | None]]:
    """Extract the compact predicate from one authenticated source snapshot."""
    tree = ast.parse(source.decode("utf-8"))
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "changed_predicate"
            for target in node.targets
        )
    )
    atomic: list[ast.expr] = []
    _flatten_or(assignment.value, atomic)
    terms = [
        ast.unparse(node).replace("dataframe['", "").replace("']", "")
        for node in atomic
    ]
    return ast.unparse(assignment.value), terms, {
        "method": "populate_entry_trend",
        "line": assignment.lineno,
        "end_line": assignment.end_lineno,
    }


def _flatten_or(node: ast.expr, output: list[ast.expr]) -> None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        _flatten_or(node.left, output)
        _flatten_or(node.right, output)
    else:
        output.append(node)


def _or_values(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_or_values(node.left), *_or_values(node.right)]
    return [node]
