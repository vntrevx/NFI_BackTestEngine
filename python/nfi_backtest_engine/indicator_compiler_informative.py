"""Informative data binding, validation, and merge lowering."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from ._indicator_ast import (
    _qualified_name,
    _safe_expression,
)
from .indicator_compiler_bindings import (
    _CallableRef,
    _LambdaRef,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol
from .indicator_compiler_windows import _required_static

_INFORMATIVE_MERGE_PARAMETERS = (
    "dataframe",
    "informative",
    "timeframe",
    "timeframe_inf",
    "ffill",
    "append_timeframe",
    "date_column",
    "suffix",
)


_INFORMATIVE_MERGE_DEFAULTS: Mapping[str, Any] = {
    "ffill": True,
    "append_timeframe": True,
    "date_column": "date",
    "suffix": None,
}


def _bind_informative_merge_arguments(
    node: ast.Call,
    compiler: CompilerProtocol,
) -> dict[str, ast.expr]:
    if len(node.args) > len(_INFORMATIVE_MERGE_PARAMETERS):
        compiler.unsupported(node, "informative merge signature")
    arguments: dict[str, ast.expr] = dict(
        zip(_INFORMATIVE_MERGE_PARAMETERS, node.args, strict=False)
    )
    for keyword in node.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded informative merge keyword arguments")
        if keyword.arg not in _INFORMATIVE_MERGE_PARAMETERS:
            compiler.unsupported(
                keyword.value,
                f"unknown informative merge keyword {keyword.arg}",
            )
        if keyword.arg in arguments:
            compiler.unsupported(
                keyword.value,
                f"duplicate informative merge argument {keyword.arg}",
            )
        arguments[keyword.arg] = keyword.value
    missing = [name for name in _INFORMATIVE_MERGE_PARAMETERS[:4] if name not in arguments]
    if missing:
        compiler.unsupported(node, f"informative merge missing arguments: {missing}")
    return arguments


def _static_informative_option(
    arguments: Mapping[str, ast.expr],
    name: str,
    compiler: CompilerProtocol,
) -> Any:
    node = arguments.get(name)
    if node is None:
        return _INFORMATIVE_MERGE_DEFAULTS[name]
    return _required_static(node, compiler)


def _freqtrade_timeframe_minutes(
    node: ast.expr,
    timeframe: str,
    compiler: CompilerProtocol,
) -> int:
    """Mirror pinned CCXT parsing used by Freqtrade's timeframe helper."""
    try:
        amount = int(timeframe[:-1])
        scale = {
            "y": 31_536_000,
            "M": 2_592_000,
            "w": 604_800,
            "d": 86_400,
            "h": 3_600,
            "m": 60,
            "s": 1,
        }[timeframe[-1]]
    except (IndexError, KeyError, ValueError):
        compiler.unsupported(node, f"invalid informative merge timeframe {timeframe!r}")
    return amount * scale // 60


class InformativeLoweringMixin:
    def informative_merge(self: CompilerProtocol, node: ast.Call) -> str:
        arguments = _bind_informative_merge_arguments(node, self)
        base = self.expression(arguments["dataframe"])
        informative = self.expression(arguments["informative"])
        if self.node_types[base] != "dataframe" or self.node_types[informative] != "dataframe":
            self.unsupported(node, "informative merge dataframe inputs")

        timeframe_node = arguments["timeframe"]
        informative_timeframe_node = arguments["timeframe_inf"]
        base_timeframe = _required_static(timeframe_node, self)
        informative_timeframe = _required_static(informative_timeframe_node, self)
        if not isinstance(base_timeframe, str) or not isinstance(informative_timeframe, str):
            self.unsupported(node, "dynamic informative merge timeframe")

        ffill = _static_informative_option(arguments, "ffill", self)
        append_timeframe = _static_informative_option(arguments, "append_timeframe", self)
        date_column = _static_informative_option(arguments, "date_column", self)
        suffix = _static_informative_option(arguments, "suffix", self)
        if not isinstance(ffill, bool):
            self.unsupported(arguments.get("ffill", node), "non-boolean informative merge ffill")
        if not isinstance(append_timeframe, bool):
            self.unsupported(
                arguments.get("append_timeframe", node),
                "non-boolean informative merge append_timeframe",
            )
        if not isinstance(date_column, str):
            self.unsupported(
                arguments.get("date_column", node),
                "non-string informative merge date_column",
            )
        if suffix is not None and not isinstance(suffix, str):
            self.unsupported(arguments.get("suffix", node), "non-string informative merge suffix")
        if suffix and append_timeframe:
            self.unsupported(
                arguments.get("suffix", node),
                "informative merge suffix conflicts with append_timeframe",
            )
        normalized_suffix = suffix or None
        if not append_timeframe and normalized_suffix is None:
            self.unsupported(
                arguments.get("append_timeframe", node),
                "informative merge without an output suffix",
            )

        base_minutes = _freqtrade_timeframe_minutes(timeframe_node, base_timeframe, self)
        informative_minutes = _freqtrade_timeframe_minutes(
            informative_timeframe_node,
            informative_timeframe,
            self,
        )
        if base_minutes > informative_minutes:
            self.unsupported(
                informative_timeframe_node,
                "faster informative timeframe would create rows",
            )
        lookback = self.merged_lookback([base, informative])
        if ffill:
            lookback = {
                "kind": "recursive",
                "candles": None,
                "expression": _safe_expression(node),
                "causal": bool(lookback["causal"]),
            }
        merged = self.emit(
            node,
            "informative-merge",
            "dataframe",
            inputs=[base, informative],
            parameters={
                "base_timeframe": base_timeframe,
                "informative_timeframe": informative_timeframe,
                "ffill": ffill,
                "append_timeframe": append_timeframe,
                "date_column": date_column,
                "suffix": normalized_suffix,
            },
            lookback=lookback,
        )
        self.informative_nodes.append(merged)
        return merged

    def callable_reference(self: CompilerProtocol, node: ast.expr) -> _CallableRef | None:
        name = _qualified_name(node)
        if name is None:
            return None
        if name.startswith(("ta.", "qtpylib.", "np.", "pd.")):
            return _CallableRef(name)
        if name.startswith("self.") and name.removeprefix("self.") in self.methods:
            return _CallableRef(name)
        return None

    @staticmethod
    def lambda_reference(node: ast.expr) -> _LambdaRef | None:
        if not isinstance(node, ast.Lambda):
            return None
        if (
            node.args.posonlyargs
            or node.args.kwonlyargs
            or node.args.vararg is not None
            or node.args.kwarg is not None
            or node.args.defaults
        ):
            return None
        return _LambdaRef(tuple(argument.arg for argument in node.args.args), node.body)

    def _inline_lambda_call(self: CompilerProtocol, node: ast.Call) -> str | None:
        if not isinstance(node.func, ast.Name):
            return None
        binding = self.bindings.get(node.func.id)
        if not isinstance(binding, _LambdaRef):
            return None
        if node.keywords or len(node.args) != len(binding.parameters):
            self.unsupported(node, "lambda call signature")
        previous = {name: self.bindings.get(name) for name in binding.parameters}
        present = {name for name in binding.parameters if name in self.bindings}
        for name, argument in zip(binding.parameters, node.args, strict=True):
            found, value = self.try_static_value(argument)
            self.bindings[name] = _StaticBinding(value) if found else self.expression(argument)
        try:
            return self.expression(binding.body)
        finally:
            for name in binding.parameters:
                if name in present:
                    prior = previous[name]
                    assert prior is not None
                    self.bindings[name] = prior
                else:
                    self.bindings.pop(name, None)
