"""Indicator compiler expressions concern."""

from __future__ import annotations

import ast

from ._indicator_ast import (
    _indicator_output_names,
)
from ._indicator_contract import (
    _literal_type,
)
from .indicator_compiler_bindings import (
    _CallableRef,
    _ColumnBundleBinding,
    _DataProviderRef,
    _LambdaRef,
    _MappingBinding,
    _SequenceBinding,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol

_UNARY_OPS = {
    ast.USub: "negate",
    ast.UAdd: "positive",
    ast.Not: "not",
    ast.Invert: "invert",
}


class ExpressionsMixin:
    def expression(self: CompilerProtocol, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return self.emit(
                node,
                "literal",
                _literal_type(node.value),
                parameters={"value": node.value},
            )
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, str):
                return binding
            if isinstance(binding, _CallableRef):
                self.unsupported(node, "callable used as an indicator value")
            if isinstance(binding, _LambdaRef):
                self.unsupported(node, "lambda used as an indicator value")
            if isinstance(binding, _SequenceBinding):
                self.unsupported(node, "sequence used as an indicator value")
            if isinstance(binding, _DataProviderRef):
                self.unsupported(node, "data provider used as an indicator value")
            if isinstance(binding, _ColumnBundleBinding):
                self.unsupported(node, "column bundle used as an indicator value")
            if isinstance(binding, _MappingBinding):
                self.unsupported(node, "mapping used as an indicator value")
            if isinstance(binding, _StaticBinding):
                return self.literal(node, binding.value)
            if node.id in self.class_constants:
                return self.literal(node, self.class_constants[node.id])
            self.unsupported(node, f"unknown indicator value {node.id}")
        if isinstance(node, ast.Attribute):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            self.unsupported(node, "attribute value")
        if isinstance(node, ast.Subscript):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            return self.subscript(node)
        if isinstance(node, ast.BinOp):
            return self.binary(node)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.BoolOp):
            return self.logical(node, type(node.op))
        if isinstance(node, ast.UnaryOp):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            operator = _UNARY_OPS.get(type(node.op))
            if operator is None:
                self.unsupported(node, "unary indicator operator")
            value = self.expression(node.operand)
            value_type = "bool-column" if operator in {"not", "invert"} else self.node_types[value]
            return self.emit(
                node,
                "unary",
                value_type,
                inputs=[value],
                parameters={"operator": operator},
                lookback=self.lookback(value),
            )
        if isinstance(node, ast.IfExp):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            return self.select(node, node.test, node.body, node.orelse)
        if isinstance(node, ast.Call):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            return self.call(node)
        if isinstance(node, ast.JoinedStr | ast.List | ast.Tuple | ast.Set | ast.Dict):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
        self.unsupported(node, f"indicator expression {type(node).__name__}")

    def subscript(self: CompilerProtocol, node: ast.Subscript) -> str:
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and not isinstance(node.slice.value, bool)
        ):
            callable_name = self.resolved_callable(node.value.func)
            if _indicator_output_names(callable_name) is not None:
                return self.indicator_output(node.value, node.slice.value, node)
            indexed_string = self.indexed_string_call(
                node.value,
                index=node.slice.value,
                source_node=node,
            )
            if indexed_string is not None:
                return indexed_string
        base = self.expression(node.value)
        base_type = self.node_types[base]
        found_key, key = self.try_static_value(node.slice)
        if not found_key or not isinstance(key, str):
            self.unsupported(node, "dynamic dataframe or metadata subscript")
        if base_type == "dataframe":
            value_type = "timestamp-column" if key == "date" else "f64-column"
            if key not in self.produced_columns:
                self.required_input_columns.add(key)
            return self.emit(
                node,
                "column-read",
                value_type,
                inputs=[base],
                parameters={"column": key},
                lookback=self.lookback(base),
            )
        if base_type == "metadata":
            return self.emit(
                node,
                "metadata-read",
                "string-scalar",
                inputs=[base],
                parameters={"key": key},
                lookback=self.lookback(base),
            )
        self.unsupported(node, "subscript source type")

    def indexed_string_call(
        self: CompilerProtocol,
        call: ast.Call,
        *,
        index: int,
        source_node: ast.AST,
    ) -> str | None:
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
            "partition",
            "split",
            "rsplit",
        }:
            return None
        if len(call.args) != 1 or call.keywords:
            self.unsupported(call, "indexed string call signature")
        found, separator = self.try_static_value(call.args[0])
        if not found or not isinstance(separator, str) or not separator:
            self.unsupported(call.args[0], "indexed string separator")
        base = self.expression(call.func.value)
        if self.node_types[base] != "string-scalar":
            self.unsupported(call.func.value, "indexed string source type")
        return self.emit(
            source_node,
            "string-split-index",
            "string-scalar",
            inputs=[base],
            parameters={
                "method": call.func.attr,
                "separator": separator,
                "index": index,
            },
            lookback=self.lookback(base),
        )
