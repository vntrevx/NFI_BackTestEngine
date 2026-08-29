"""Indicator compiler arguments helpers."""

from __future__ import annotations

import ast

from ._indicator_ast import (
    _qualified_name,
)
from .indicator_compiler_protocol import CompilerProtocol


def _bind_helper_arguments(
    call: ast.Call,
    method: ast.FunctionDef,
    compiler: CompilerProtocol,
) -> list[tuple[str, ast.expr]]:
    if method.args.vararg is not None or method.args.kwarg is not None:
        compiler.unsupported(call, "variadic indicator helper signature")
    positional = [*method.args.posonlyargs, *method.args.args]
    if positional and positional[0].arg == "self":
        positional = positional[1:]
    names = [argument.arg for argument in positional]
    keyword_only = [argument.arg for argument in method.args.kwonlyargs]
    if len(call.args) > len(positional):
        compiler.unsupported(call, "indicator helper call signature")
    bound: dict[str, ast.expr] = {
        name: value for name, value in zip(names, call.args, strict=False)
    }
    for keyword in call.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded indicator helper arguments")
        if keyword.arg not in {*names, *keyword_only}:
            compiler.unsupported(keyword.value, f"unknown indicator helper argument {keyword.arg}")
        if keyword.arg in bound:
            compiler.unsupported(
                keyword.value,
                f"duplicate indicator helper argument {keyword.arg}",
            )
        bound[keyword.arg] = keyword.value

    positional_defaults = [None] * (len(positional) - len(method.args.defaults)) + list(
        method.args.defaults
    )
    for argument, default in zip(positional, positional_defaults, strict=True):
        if argument.arg not in bound:
            if default is None:
                compiler.unsupported(call, "indicator helper call signature")
            bound[argument.arg] = default
    for argument, default in zip(method.args.kwonlyargs, method.args.kw_defaults, strict=True):
        if argument.arg not in bound:
            if default is None:
                compiler.unsupported(call, "indicator helper call signature")
            bound[argument.arg] = default
    return [(name, bound[name]) for name in [*names, *keyword_only]]


def _bind_pair_dataframe_arguments(
    call: ast.Call,
    compiler: CompilerProtocol,
) -> dict[str, ast.expr]:
    names = ("pair", "timeframe")
    if len(call.args) > len(names):
        compiler.unsupported(call, "get_pair_dataframe signature")
    bound: dict[str, ast.expr] = dict(zip(names, call.args, strict=False))
    for keyword in call.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded get_pair_dataframe arguments")
        if keyword.arg not in names:
            compiler.unsupported(
                keyword.value,
                f"unknown get_pair_dataframe argument {keyword.arg}",
            )
        if keyword.arg in bound:
            compiler.unsupported(
                keyword.value,
                f"duplicate get_pair_dataframe argument {keyword.arg}",
            )
        bound[str(keyword.arg)] = keyword.value
    if set(bound) != set(names):
        compiler.unsupported(call, "get_pair_dataframe requires pair and timeframe")
    return bound


def _recognized_sequence_reducer(function: ast.FunctionDef) -> str | None:
    calls = {
        _qualified_name(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)
    }
    matched = {
        operator_name
        for qualified, operator_name in (
            ("np.logical_and.reduce", "and"),
            ("np.logical_or.reduce", "or"),
        )
        if qualified in calls
    }
    return next(iter(matched)) if len(matched) == 1 else None
