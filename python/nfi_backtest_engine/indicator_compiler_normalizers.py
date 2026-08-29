"""Normalized source helper recognition for indicator compilation."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from typing import Any

from ._indicator_ast import (
    _NATIVE_HELPER_TEMPLATES,
    _helper_bodies_equal,
    _is_shift_slice_assignment,
    _qualified_name,
    _template_function,
)
from .indicator_compiler_protocol import CompilerProtocol


def _normalized_shift_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: CompilerProtocol,
) -> tuple[ast.expr, int] | None:
    """Recognize the canonical allocate-and-slice causal shift implementation."""
    parameters = [*method.args.posonlyargs, *method.args.args]
    if parameters and parameters[0].arg == "self":
        parameters = parameters[1:]
    if len(parameters) != 2 or len(method.body) != 4:
        return None
    source_name, periods_name = (argument.arg for argument in parameters)
    allocate, warmup, shifted, returned = method.body
    if not (
        isinstance(allocate, ast.Assign)
        and len(allocate.targets) == 1
        and isinstance(allocate.targets[0], ast.Name)
        and isinstance(allocate.value, ast.Call)
        and _qualified_name(allocate.value.func) == "np.empty_like"
        and len(allocate.value.args) == 1
        and isinstance(allocate.value.args[0], ast.Name)
        and allocate.value.args[0].id == source_name
        and not allocate.value.keywords
    ):
        return None
    output_name = allocate.targets[0].id
    if not _is_shift_slice_assignment(
        warmup,
        output_name=output_name,
        source_name=source_name,
        periods_name=periods_name,
        warmup=True,
    ) or not _is_shift_slice_assignment(
        shifted,
        output_name=output_name,
        source_name=source_name,
        periods_name=periods_name,
        warmup=False,
    ):
        return None
    if not (
        isinstance(returned, ast.Return)
        and isinstance(returned.value, ast.Name)
        and returned.value.id == output_name
    ):
        return None
    arguments = dict(bound)
    found, periods = compiler.try_static_value(arguments[periods_name])
    if not found or not isinstance(periods, int) or isinstance(periods, bool) or periods <= 0:
        compiler.unsupported(arguments[periods_name], "shift helper periods")
    return arguments[source_name], periods


def _normalized_frame_projection_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: CompilerProtocol,
) -> tuple[ast.expr, dict[str, Any]] | None:
    parameters = [*method.args.posonlyargs, *method.args.args]
    if parameters and parameters[0].arg == "self":
        parameters = parameters[1:]
    if len(parameters) != 2:
        return None
    frame_name, keep_name = (parameter.arg for parameter in parameters)
    assignments = [
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and isinstance(statement.value, ast.ListComp)
    ]
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    assert isinstance(assignment.targets[0], ast.Name)
    assert isinstance(assignment.value, ast.ListComp)
    output_name = assignment.targets[0].id
    comprehension = assignment.value
    if not (
        isinstance(comprehension.elt, ast.Name)
        and len(comprehension.generators) == 1
        and isinstance(comprehension.generators[0].target, ast.Name)
        and comprehension.elt.id == comprehension.generators[0].target.id
        and isinstance(comprehension.generators[0].iter, ast.Attribute)
        and comprehension.generators[0].iter.attr == "columns"
        and isinstance(comprehension.generators[0].iter.value, ast.Name)
        and comprehension.generators[0].iter.value.id == frame_name
        and len(comprehension.generators[0].ifs) == 1
    ):
        return None
    column_name = comprehension.elt.id
    always_keep: str | None = None
    candidates_name: str | None = None
    keeps_requested = False
    for comparison in (
        item
        for item in ast.walk(comprehension.generators[0].ifs[0])
        if isinstance(item, ast.Compare)
    ):
        if len(comparison.ops) != 1 or len(comparison.comparators) != 1:
            return None
        right = comparison.comparators[0]
        if not isinstance(comparison.left, ast.Name) or comparison.left.id != column_name:
            continue
        if isinstance(comparison.ops[0], ast.Eq) and isinstance(right, ast.Constant):
            if not isinstance(right.value, str):
                return None
            always_keep = right.value
        elif (
            isinstance(comparison.ops[0], ast.NotIn)
            and isinstance(right, ast.Attribute)
            and isinstance(right.value, ast.Name)
            and right.value.id == "self"
        ):
            candidates_name = right.attr
        elif (
            isinstance(comparison.ops[0], ast.In)
            and isinstance(right, ast.Name)
            and right.id == keep_name
        ):
            keeps_requested = True
    returned = next(
        (
            statement
            for statement in method.body
            if isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Subscript)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id == frame_name
            and isinstance(statement.value.slice, ast.Name)
            and statement.value.slice.id == output_name
        ),
        None,
    )
    if (
        always_keep is None
        or candidates_name is None
        or not keeps_requested
        or returned is None
        or candidates_name not in compiler.class_constants
    ):
        return None
    candidates = compiler.class_constants[candidates_name]
    if not isinstance(candidates, list | tuple) or not all(
        isinstance(value, str) for value in candidates
    ):
        compiler.unsupported(method, "frame projection candidate columns")
    arguments = dict(bound)
    found_keep, keep = compiler.try_static_value(arguments[keep_name])
    if not found_keep or keep is None:
        keep_values: list[str] = []
    elif isinstance(keep, list | tuple) and all(isinstance(value, str) for value in keep):
        keep_values = list(keep)
    else:
        compiler.unsupported(arguments[keep_name], "frame projection keep columns")
    return arguments[frame_name], {
        "always_keep": [always_keep],
        "drop_candidates": list(candidates),
        "keep": keep_values,
    }


def _normalized_native_indicator_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: CompilerProtocol,
) -> tuple[str, list[ast.expr], dict[str, Any]] | None:
    matched = next(
        (
            name
            for name, source in _NATIVE_HELPER_TEMPLATES.items()
            if _helper_bodies_equal(method, _template_function(source))
        ),
        None,
    )
    if matched is None:
        return None
    arguments = dict(bound)
    if matched in {
        "chaikin-money-flow",
        "chaikin-money-flow-legacy",
        "chaikin-money-flow-rolling-sum",
    }:
        found, period = compiler.try_static_value(arguments["timeperiod"])
        minimum = 2 if matched == "chaikin-money-flow-legacy" else 1
        if not found or not isinstance(period, int) or isinstance(period, bool) or period < minimum:
            compiler.unsupported(arguments["timeperiod"], "chaikin timeperiod")
        native_name = (
            "chaikin-money-flow" if matched == "chaikin-money-flow-rolling-sum" else matched
        )
        return (
            native_name,
            [arguments[name] for name in ("high", "low", "close", "volume")],
            {"timeperiod": period},
        )
    return matched, [arguments["arr"]], {}
