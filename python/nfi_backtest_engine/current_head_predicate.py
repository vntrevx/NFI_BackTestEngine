"""Source-bound inventory for one changed signal predicate."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from .errors import SpecValidationError


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """One exact source span."""

    path: str
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class AtomicPredicate:
    """One resolved column-to-scalar comparison."""

    column: str
    operator: str
    threshold: float
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class PredicateClause:
    """One disjunction added to a changed conjunction."""

    terms: tuple[AtomicPredicate, ...]
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class ChangedSignalPredicate:
    """Ordered source-derived clauses added to one signal branch."""

    signal: int
    clauses: tuple[PredicateClause, ...]
    source: SourceLocation

    @property
    def atomic_terms(self) -> tuple[AtomicPredicate, ...]:
        """Flatten terms while preserving source order."""
        return tuple(term for clause in self.clauses for term in clause.terms)


def extract_changed_signal_clauses(
    old_source: str | Path,
    new_source: str | Path,
    *,
    class_name: str,
    signal: int,
) -> ChangedSignalPredicate:
    """Extract clauses present only in the new signal branch."""
    old_path = Path(old_source).resolve()
    new_path = Path(new_source).resolve()
    _, old_branch = _signal_branch(old_path, class_name, signal)
    new_method, new_branch = _signal_branch(new_path, class_name, signal)
    old_expression = _main_predicate(old_path, old_branch)
    new_expression = _main_predicate(new_path, new_branch)
    remaining = Counter(_node_identity(node) for node in _flatten(old_expression, ast.BitAnd))
    added = []
    for node in _flatten(new_expression, ast.BitAnd):
        identity = _node_identity(node)
        if remaining[identity]:
            remaining[identity] -= 1
        else:
            added.append(node)
    assignments = _assignments_before(new_method, new_branch.lineno)
    clauses = tuple(
        PredicateClause(
            terms=tuple(
                _atomic_predicate(new_path, term, assignments)
                for term in _flatten(clause, ast.BitOr)
            ),
            source=_location(new_path, clause),
        )
        for clause in added
    )
    if not clauses:
        raise SpecValidationError("changed signal predicate has no added clauses")
    return ChangedSignalPredicate(
        signal=signal,
        clauses=clauses,
        source=_location(new_path, new_branch),
    )


def _signal_branch(
    path: Path,
    class_name: str,
    signal: int,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.If]:
    try:
        tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise SpecValidationError(f"changed signal source does not parse: {path}") from exc
    class_node = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    method = (
        next(
            (
                node
                for node in class_node.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "populate_entry_trend"
            ),
            None,
        )
        if class_node is not None
        else None
    )
    if method is None:
        raise SpecValidationError("changed signal entrypoint is absent")
    expected = f"short_entry_condition_index == {signal}"
    branch = next(
        (
            node
            for node in ast.walk(method)
            if isinstance(node, ast.If) and ast.unparse(node.test) == expected
        ),
        None,
    )
    if branch is None:
        raise SpecValidationError(f"changed signal {signal} branch is absent")
    return method, branch


def _main_predicate(path: Path, branch: ast.If) -> ast.expr:
    candidates = [
        call.args[0]
        for statement in branch.body
        if isinstance(statement, ast.Expr)
        and isinstance((call := statement.value), ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "append"
        and len(call.args) == 1
    ]
    conjunctions = [
        node
        for node in candidates
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd)
    ]
    if not conjunctions:
        raise SpecValidationError(f"{path}:{branch.lineno}:0: changed conjunction is absent")
    return max(conjunctions, key=lambda node: len(_flatten(node, ast.BitAnd)))


def _assignments_before(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    line: int,
) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(method):
        if isinstance(node, ast.Assign) and node.lineno < line:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    return assignments


def _atomic_predicate(
    path: Path,
    node: ast.expr,
    assignments: dict[str, ast.expr],
) -> AtomicPredicate:
    resolved = assignments.get(node.id, node) if isinstance(node, ast.Name) else node
    if (
        not isinstance(resolved, ast.Compare)
        or len(resolved.ops) != 1
        or len(resolved.comparators) != 1
    ):
        _unsupported(path, node)
    left = resolved.left
    if not isinstance(left, ast.Name):
        _unsupported(path, left)
    source = assignments.get(left.id)
    if (
        not isinstance(source, ast.Call)
        or not isinstance(source.func, ast.Name)
        or source.func.id != "np_view"
        or len(source.args) != 1
        or not isinstance(source.args[0], ast.Constant)
        or not isinstance(source.args[0].value, str)
    ):
        _unsupported(path, left)
    threshold = _number(resolved.comparators[0])
    operators = {ast.Gt: "gt", ast.GtE: "gte", ast.Lt: "lt", ast.LtE: "lte"}
    operator = next(
        (name for kind, name in operators.items() if isinstance(resolved.ops[0], kind)), None
    )
    if operator is None or threshold is None:
        _unsupported(path, resolved)
    return AtomicPredicate(
        column=source.args[0].value,
        operator=operator,
        threshold=threshold,
        source=_location(path, node),
    )


def _number(node: ast.expr) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _number(node.operand)
        return -value if value is not None else None
    return None


def _flatten(node: ast.expr, operator: type[ast.operator]) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, operator):
        return [*_flatten(node.left, operator), *_flatten(node.right, operator)]
    return [node]


def _node_identity(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _location(path: Path, node: ast.AST) -> SourceLocation:
    return SourceLocation(
        path=path.name,
        line=getattr(node, "lineno", 1),
        column=getattr(node, "col_offset", 0),
        end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        end_column=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    )


def _unsupported(path: Path, node: ast.AST) -> Never:
    location = _location(path, node)
    raise SpecValidationError(
        f"{location.path}:{location.line}:{location.column}: unsupported changed predicate AST"
    )
