"""Indicator compiler indicators concern."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from ._indicator_ast import (
    _indicator_output_names,
    _indicator_signature,
    _safe_expression,
)
from .indicator_compiler_protocol import CompilerProtocol
from .indicator_compiler_windows import (
    _literal_keyword_arguments,
    _required_static,
)


class IndicatorsMixin:
    def indicator_output(
        self: CompilerProtocol, call: ast.Call, index: int, source_node: ast.AST
    ) -> str:
        callable_name = self.resolved_callable(call.func)
        output_names = _indicator_output_names(callable_name)
        if output_names is None or index < 0 or index >= len(output_names):
            self.unsupported(source_node, "indicator output index")
        inputs, parameters = self.indicator_call_parts(call, callable_name)
        return self.emit_indicator_call(
            source_node,
            callable_name,
            inputs,
            parameters,
            output=output_names[index],
        )

    def indicator_call(self: CompilerProtocol, node: ast.Call) -> str:
        callable_name = self.resolved_callable(node.func)
        output_names = _indicator_output_names(callable_name)
        if output_names is not None and len(output_names) != 1:
            self.unsupported(node, "multi-output indicator requires tuple assignment or subscript")
        inputs, parameters = self.indicator_call_parts(node, callable_name)
        return self.emit_indicator_call(node, callable_name, inputs, parameters)

    def indicator_call_parts(
        self: CompilerProtocol,
        node: ast.Call,
        callable_name: str,
    ) -> tuple[list[str], dict[str, Any]]:
        if not callable_name.startswith(("ta.", "qtpylib.")):
            self.unsupported(node, "indexed value is not an indicator call")
        signature = _indicator_signature(callable_name)
        if signature is None:
            return (
                [self.expression(argument) for argument in node.args],
                _literal_keyword_arguments(node, self),
            )
        input_count, parameter_names = signature
        if len(node.args) < input_count or len(node.args) > input_count + len(parameter_names):
            self.unsupported(node, "indicator positional signature")
        inputs = [self.expression(argument) for argument in node.args[:input_count]]
        arguments = _literal_keyword_arguments(node, self)
        if any(name not in parameter_names for name in arguments):
            self.unsupported(node, "unknown indicator keyword argument")
        for name, argument in zip(parameter_names, node.args[input_count:], strict=False):
            if name in arguments:
                self.unsupported(argument, "duplicate indicator argument")
            arguments[name] = _required_static(argument, self)
        return inputs, arguments

    def emit_indicator_call(
        self: CompilerProtocol,
        source_node: ast.AST,
        callable_name: str,
        inputs: Sequence[str],
        arguments: Mapping[str, Any],
        *,
        output: str | None = None,
    ) -> str:
        family, _, name = callable_name.partition(".")
        parameters: dict[str, Any] = {
            "family": family,
            "name": name,
            "arguments": dict(arguments),
        }
        if output is not None:
            parameters["output"] = output
        return self.emit(
            source_node,
            "indicator-call",
            "f64-column",
            inputs=inputs,
            parameters=parameters,
            lookback={
                "kind": "library-defined",
                "candles": None,
                "expression": _safe_expression(source_node),
                "causal": True,
            },
        )
