"""Indicator compiler calls concern."""

from __future__ import annotations

import ast
from typing import Any

from ._indicator_contract import (
    _add_finite_lookback,
    _array_result_type,
    _merge_lookbacks,
)
from .indicator_compiler_arguments import (
    _bind_helper_arguments,
)
from .indicator_compiler_bindings import (
    _CallableRef,
)
from .indicator_compiler_normalizers import (
    _normalized_frame_projection_helper,
    _normalized_native_indicator_helper,
    _normalized_shift_helper,
)
from .indicator_compiler_protocol import CompilerProtocol
from .indicator_compiler_windows import (
    _literal_keyword_arguments,
)

_SCALAR_CALLS = {"abs", "bool", "float", "int", "max", "min"}


class CallsMixin:
    def call(self: CompilerProtocol, node: ast.Call) -> str:
        window = self.window_call(node)
        if window is not None:
            return window
        lambda_value = self._inline_lambda_call(node)
        if lambda_value is not None:
            return lambda_value
        callable_name = self.resolved_callable(node.func)
        frame_source = self.frame_source_call(node)
        if frame_source is not None:
            return frame_source
        sequence_value = self.reduce_sequence_call(node, callable_name)
        if sequence_value is not None:
            return sequence_value
        tag_value = self.append_tag_call(node, callable_name)
        if tag_value is not None:
            return tag_value
        if callable_name == "time.perf_counter":
            return self.emit(
                node,
                "instrumentation",
                "f64-scalar",
                parameters={"name": callable_name},
            )
        if callable_name.startswith("log."):
            return self.emit(
                node,
                "instrumentation",
                "null",
                parameters={"name": callable_name},
            )
        concatenated = self.concat_column_bundle(node, callable_name)
        if concatenated is not None:
            return concatenated
        if callable_name == "len":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "len signature")
            value = self.expression(node.args[0])
            if self.node_types[value] != "dataframe":
                self.unsupported(node.args[0], "len source type")
            return self.emit(
                node,
                "row-count",
                "int-scalar",
                inputs=[value],
                lookback=self.lookback(value),
            )
        if callable_name.startswith("self."):
            method_name = callable_name.removeprefix("self.")
            method = self.methods.get(method_name)
            if method is None or isinstance(method, ast.AsyncFunctionDef):
                self.unsupported(node, "indicator helper call target")
            bound = _bind_helper_arguments(node, method, self)
            shift = _normalized_shift_helper(method, bound, self)
            if shift is not None:
                source_node, periods = shift
                source = self.expression(source_node)
                if not self.node_types[source].endswith("-column"):
                    self.unsupported(source_node, "shift helper source type")
                return self.emit(
                    node,
                    "shift",
                    self.node_types[source],
                    inputs=[source],
                    parameters={"periods": periods},
                    lookback=_add_finite_lookback(self.lookback(source), periods),
                )
            projection = _normalized_frame_projection_helper(method, bound, self)
            if projection is not None:
                source_node, parameters = projection
                source = self.expression(source_node)
                if self.node_types[source] != "dataframe":
                    self.unsupported(source_node, "frame projection source type")
                return self.emit(
                    node,
                    "frame-project",
                    "dataframe",
                    inputs=[source],
                    parameters=parameters,
                    lookback=self.lookback(source),
                )
            native_indicator = _normalized_native_indicator_helper(method, bound, self)
            if native_indicator is not None:
                name, argument_nodes, parameters = native_indicator
                inputs = [self.expression(argument) for argument in argument_nodes]
                if any(not self.node_types[value].endswith("-column") for value in inputs):
                    self.unsupported(node, "native indicator input type")
                return self.emit(
                    node,
                    "indicator-call",
                    "f64-column",
                    inputs=inputs,
                    parameters={
                        "family": "native",
                        "name": name,
                        "arguments": parameters,
                    },
                    lookback={
                        "kind": "function-defined",
                        "candles": parameters.get("timeperiod", 1) - 1,
                        "expression": name,
                        "causal": True,
                    },
                )
            static_arguments: dict[str, Any] = {}
            callable_arguments: dict[str, _CallableRef] = {}
            dynamic_arguments: list[ast.expr] = []
            for name, argument in bound:
                if isinstance(argument, ast.Name):
                    argument_binding = self.bindings.get(argument.id)
                    if isinstance(argument_binding, _CallableRef):
                        callable_arguments[name] = argument_binding
                        continue
                found, value = self.try_static_value(argument)
                if found:
                    static_arguments[name] = value
                else:
                    dynamic_arguments.append(argument)
            function_id = self.compile_method(
                method_name,
                static_arguments=static_arguments,
                callable_arguments=callable_arguments,
            )
            if len(dynamic_arguments) != self.function_arities[function_id]:
                self.unsupported(node, "indicator helper call signature")
            inputs = [self.expression(argument) for argument in dynamic_arguments]
            value_type = self.function_return_types[function_id]
            if not inputs and function_id in self.function_static_returns:
                return self.literal(node, self.function_static_returns[function_id])
            return self.emit(
                node,
                "function-call",
                value_type,
                inputs=inputs,
                parameters={"function": function_id},
                lookback=_merge_lookbacks(
                    [
                        *(self.lookback(input_id) for input_id in inputs),
                        self.function_lookbacks[function_id],
                    ]
                ),
            )
        if callable_name in {"merge_informative_pair", "freqtrade.merge_informative_pair"}:
            return self.informative_merge(node)
        if callable_name == "np.where":
            if len(node.args) != 3 or node.keywords:
                self.unsupported(node, "numpy where signature")
            return self.select(node, node.args[0], node.args[1], node.args[2])
        if callable_name in {"pd.Series", "pd.DataFrame", "pd.array"}:
            target = callable_name.removeprefix("pd.").lower()
            if target == "array":
                positional_dtype = None
                if len(node.args) == 2 and not node.keywords:
                    found, positional_dtype = self.try_static_value(node.args[1])
                    if not found:
                        self.unsupported(node.args[1], "pandas array dtype")
                elif len(node.args) == 1:
                    positional_dtype = _literal_keyword_arguments(node, self).get("dtype")
                else:
                    self.unsupported(node, "pandas cast signature")
                if positional_dtype != "string":
                    self.unsupported(node, "pandas array dtype")
                target = "string-array"
            elif len(node.args) != 1 or node.keywords:
                self.unsupported(node, "pandas cast signature")
            value = self.expression(node.args[0])
            return self.emit(
                node,
                "cast",
                "string-column" if target == "string-array" else self.node_types[value],
                inputs=[value],
                parameters={"target": target},
                lookback=self.lookback(value),
            )
        if callable_name.startswith("ta.") or callable_name.startswith("qtpylib."):
            return self.indicator_call(node)
        if callable_name.startswith("np."):
            return self.array_call(node, callable_name)
        if callable_name in _SCALAR_CALLS:
            if node.keywords:
                self.unsupported(node, "scalar indicator keyword arguments")
            inputs = [self.expression(argument) for argument in node.args]
            return self.emit(
                node,
                "scalar-call",
                _array_result_type(inputs, self.node_types),
                inputs=inputs,
                parameters={"name": callable_name},
                lookback=self.merged_lookback(inputs),
            )
        if isinstance(node.func, ast.Attribute):
            return self.method_call(node, callable_name)
        self.unsupported(node, f"indicator call {callable_name}")
