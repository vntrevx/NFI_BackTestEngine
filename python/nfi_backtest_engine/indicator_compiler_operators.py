"""Indicator compiler operators concern."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence

from ._indicator_ast import (
    _flatten_binary_values,
    _is_json_value,
    _normalized_static_value,
)
from ._indicator_contract import (
    _boolean_result_type,
    _merge_value_types,
    _numeric_result_type,
)
from .indicator_compiler_protocol import CompilerProtocol

_BINARY_OPS = {
    ast.Add: "add",
    ast.Sub: "subtract",
    ast.Mult: "multiply",
    ast.Div: "divide",
    ast.FloorDiv: "floor-divide",
    ast.Mod: "modulo",
    ast.Pow: "power",
}
_LOGICAL_BINARY_OPS = {ast.BitAnd: "and", ast.BitOr: "or"}
_COMPARE_OPS = {
    ast.Eq: "equal",
    ast.NotEq: "not-equal",
    ast.Lt: "less-than",
    ast.LtE: "less-than-or-equal",
    ast.Gt: "greater-than",
    ast.GtE: "greater-than-or-equal",
}


class OperatorsMixin:
    def binary(self: CompilerProtocol, node: ast.BinOp) -> str:
        logical = _LOGICAL_BINARY_OPS.get(type(node.op))
        if logical is not None:
            return self.logical(
                node,
                type(node.op),
                values=_flatten_binary_values(node, type(node.op)),
            )
        operator = _BINARY_OPS.get(type(node.op))
        if operator is None:
            self.unsupported(node, "binary indicator operator")
        left = self.expression(node.left)
        right = self.expression(node.right)
        value_type = _numeric_result_type(self.node_types[left], self.node_types[right])
        return self.emit(
            node,
            "binary",
            value_type,
            inputs=[left, right],
            parameters={"operator": operator},
            lookback=self.merged_lookback([left, right]),
        )

    def compare(self: CompilerProtocol, node: ast.Compare) -> str:
        left_node = node.left
        comparisons = []
        for operator_node, right_node in zip(node.ops, node.comparators, strict=True):
            if isinstance(operator_node, ast.In | ast.NotIn):
                found, collection = self.try_static_value(right_node)
                if not found or not isinstance(collection, list | tuple | Mapping):
                    self.unsupported(right_node, "dynamic membership collection")
                left = self.expression(left_node)
                values = list(collection)
                if not all(_is_json_value(value) for value in values):
                    self.unsupported(right_node, "non-JSON membership collection")
                comparisons.append(
                    self.emit(
                        node,
                        "membership",
                        _boolean_result_type(self.node_types[left]),
                        inputs=[left],
                        parameters={
                            "values": [_normalized_static_value(value) for value in values],
                            "negated": isinstance(operator_node, ast.NotIn),
                        },
                        lookback=self.lookback(left),
                    )
                )
                left_node = right_node
                continue
            operator = _COMPARE_OPS.get(type(operator_node))
            if operator is None:
                self.unsupported(node, "comparison indicator operator")
            left = self.expression(left_node)
            right = self.expression(right_node)
            value_type = _boolean_result_type(self.node_types[left], self.node_types[right])
            comparisons.append(
                self.emit(
                    node,
                    "compare",
                    value_type,
                    inputs=[left, right],
                    parameters={"operator": operator},
                    lookback=self.merged_lookback([left, right]),
                )
            )
            left_node = right_node
        if len(comparisons) == 1:
            return comparisons[0]
        return self.emit(
            node,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in comparisons)),
            inputs=comparisons,
            parameters={"operator": "and"},
            lookback=self.merged_lookback(comparisons),
        )

    def logical(
        self: CompilerProtocol,
        node: ast.AST,
        operator_type: type[ast.operator] | type[ast.boolop],
        *,
        values: Sequence[ast.expr] | None = None,
    ) -> str:
        operator = {
            ast.And: "and",
            ast.Or: "or",
            ast.BitAnd: "and",
            ast.BitOr: "or",
        }.get(operator_type)
        if operator is None:
            self.unsupported(node, "logical indicator operator")
        expressions = values or (node.values if isinstance(node, ast.BoolOp) else ())
        inputs = [self.expression(value) for value in expressions]
        return self.emit(
            node,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in inputs)),
            inputs=inputs,
            parameters={"operator": operator},
            lookback=self.merged_lookback(inputs),
        )

    def select(
        self: CompilerProtocol,
        node: ast.AST,
        condition_node: ast.expr,
        true_node: ast.expr,
        false_node: ast.expr,
    ) -> str:
        condition = self.expression(condition_node)
        true_value = self.expression(true_node)
        false_value = self.expression(false_node)
        value_type = _merge_value_types(
            self.node_types[true_value],
            self.node_types[false_value],
        )
        return self.emit(
            node,
            "select",
            value_type,
            inputs=[condition, true_value, false_value],
            lookback=self.merged_lookback([condition, true_value, false_value]),
        )
