"""Compile X7's independently authored managed-short exit router.

Short callbacks have their own condition direction, state ordering, compound
tag rules, and final normal fallback.  This module intentionally consumes the
short AST itself instead of deriving a program by transforming the long side.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import StrategyAnalysisError
from .managed_exit_ir import (
    MANAGED_EXIT_PROGRAM_VERSION,
    _assigned_exit_callbacks,
    _compile_decision_prefix,
    _compile_state_policy,
    _compile_tag_matcher,
    _decision_profit_basis,
    _location,
    _mode_name,
    _RouteSpec,
    _self_aliases,
    _validate_dispatch_branch,
    _validate_literal_terminal_exits,
)


@dataclass(frozen=True)
class ManagedShortExitCompilation:
    """Executable short IR plus the route order read from source."""

    program: dict[str, Any]
    short_route_order: tuple[str, ...]


def compile_managed_short_exit_ir(
    methods: Mapping[str, ast.FunctionDef],
    constants: Mapping[str, Any],
    route_specs: Sequence[_RouteSpec],
) -> ManagedShortExitCompilation:
    """Compile every executed managed-short branch in source order."""

    custom_exit = methods.get("custom_exit")
    if custom_exit is None:
        raise StrategyAnalysisError("managed short exit IR requires custom_exit")
    aliases = _self_aliases(custom_exit)
    short_side_names = _short_side_names(custom_exit)
    specs_by_method: dict[str, list[_RouteSpec]] = defaultdict(list)
    for spec in route_specs:
        specs_by_method[spec.method].append(spec)
    occurrences: dict[str, int] = defaultdict(int)
    discovered: list[tuple[int, _RouteSpec, ast.If, dict[str, Any]]] = []
    unknown: set[str] = set()
    for index, statement in enumerate(custom_exit.body):
        if not isinstance(statement, ast.If):
            continue
        calls = [
            name
            for name in _assigned_exit_callbacks(statement, aliases)
            if name.startswith("short_exit_")
        ]
        for method_name in calls:
            candidates = specs_by_method.get(method_name)
            occurrence = occurrences[method_name]
            occurrences[method_name] += 1
            if candidates is None or occurrence >= len(candidates):
                unknown.add(method_name)
                continue
            spec = candidates[occurrence]
            matcher = _compile_short_matcher(
                statement.test,
                aliases,
                constants,
                short_side_names,
            )
            if matcher is None:
                raise StrategyAnalysisError(
                    f"NFI {spec.key} short tag/side matcher cannot be represented"
                )
            discovered.append((index, spec, statement, matcher))
    if unknown:
        raise StrategyAnalysisError(
            "NFI custom_exit contains unclassified short routes: "
            + ", ".join(sorted(unknown))
        )
    expected_keys = [spec.key for spec in route_specs]
    actual_keys = [spec.key for _, spec, _, _ in discovered]
    if actual_keys != expected_keys:
        missing = [key for key in expected_keys if key not in actual_keys]
        raise StrategyAnalysisError(
            "NFI managed short route inventory changed"
            + (f": missing {', '.join(missing)}" if missing else "")
        )

    routes: list[dict[str, Any]] = []
    for source_order, (_custom_index, spec, branch, matcher) in enumerate(discovered):
        wrapper = methods.get(spec.method)
        if wrapper is None:
            raise StrategyAnalysisError(f"NFI managed short wrapper is missing: {spec.method}")
        _validate_dispatch_branch(branch, spec.method, aliases)
        programs, profit_gate = _compile_decision_prefix(wrapper)
        _validate_literal_terminal_exits(wrapper, None)
        routes.append(
            {
                "id": spec.key,
                "source_order": source_order,
                "match": matcher,
                "initial_profit_gate": profit_gate,
                "profit_basis": _decision_profit_basis(wrapper, programs),
                "mode_name": _mode_name(wrapper, constants),
                "decision_program_order": programs,
                "state_program": _compile_state_policy(
                    wrapper,
                    constants,
                    target_helper=methods.get("exit_profit_target"),
                    side="short",
                ),
                "terminal_exit": None,
                "location": _location(branch),
            }
        )
    program: dict[str, Any] = {
        "schema_version": MANAGED_EXIT_PROGRAM_VERSION,
        "execution_mode": "primary-with-legacy-shadow",
        "routes": routes,
    }
    encoded = json.dumps(
        program,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    program["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return ManagedShortExitCompilation(
        program=program,
        short_route_order=tuple(actual_keys),
    )


def _compile_short_matcher(
    node: ast.AST,
    aliases: Mapping[str, str],
    constants: Mapping[str, Any],
    short_side_names: frozenset[str],
) -> dict[str, Any] | None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And | ast.Or):
        operands = [
            _compile_short_matcher(value, aliases, constants, short_side_names)
            for value in node.values
        ]
        if any(operand is None for operand in operands):
            return None
        return {
            "operator": "all-of" if isinstance(node.op, ast.And) else "any-of",
            "operands": operands,
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        operand = _compile_short_matcher(
            node.operand,
            aliases,
            constants,
            short_side_names,
        )
        return None if operand is None else {"operator": "not", "operands": [operand]}
    if isinstance(node, ast.Name) and node.id in short_side_names:
        return {"operator": "is-short"}
    return _compile_tag_matcher(node, aliases, constants)


def _short_side_names(method: ast.FunctionDef) -> frozenset[str]:
    names = {
        target.id
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance((target := statement.targets[0]), ast.Name)
        and isinstance(statement.value, ast.Attribute)
        and statement.value.attr == "is_short"
        and isinstance(statement.value.value, ast.Name)
        and statement.value.value.id == "trade"
    }
    if not names:
        raise StrategyAnalysisError("NFI custom_exit short-side binding is missing")
    return frozenset(names)
