"""Indicator compiler statements concern."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from ._indicator_ast import (
    _loop_target_names,
)
from .indicator_compiler_protocol import CompilerProtocol


class StatementsMixin:
    def statement(self: CompilerProtocol, node: ast.stmt) -> None:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                callable_ref = self.callable_reference(node.value)
                lambda_ref = self.lambda_reference(node.value)
                sequence_ref = self.sequence_reference(node.value)
                provider_ref = self.data_provider_reference(node.value)
                bundle_ref = self.column_bundle_reference(node.value)
                mapping_ref = self.mapping_reference(node.value)
                static_ref = self.static_reference(node.value)
                self.bindings[target.id] = (
                    callable_ref
                    or lambda_ref
                    or sequence_ref
                    or provider_ref
                    or bundle_ref
                    or mapping_ref
                    or static_ref
                    or self.expression(node.value)
                )
                return
            if isinstance(target, ast.Subscript):
                if self.mapping_write(target, node.value):
                    return
                if self.array_index_write(target, node.value):
                    return
                self.column_write(target, node.value, node)
                return
            if isinstance(target, ast.Tuple | ast.List) and isinstance(node.value, ast.Call):
                self.multi_output_assignment(target, node.value)
                return
            self.unsupported(node, "indicator assignment target")
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            if not isinstance(node.target, ast.Name):
                self.unsupported(node, "annotated indicator assignment target")
            callable_ref = self.callable_reference(node.value)
            self.bindings[node.target.id] = callable_ref or self.expression(node.value)
            return
        if isinstance(node, ast.Return) and node.value is not None:
            value = self.expression(node.value)
            self.return_node = self.emit(
                node,
                "return",
                self.node_types[value],
                inputs=[value],
                lookback=self.lookback(value),
            )
            return
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if self.append_sequence(node.value):
                return
            if self.inplace_forward_fill(node.value):
                return
            self.expression(node.value)
            return
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return
        if isinstance(node, ast.If):
            if self.frame_empty_guard(node):
                return
            if self.frame_drop_guard(node):
                return
            condition = self.static_value(node.test)
            if not isinstance(condition, bool):
                self.unsupported(node, "dynamic indicator control-flow if")
            selected = node.body if condition else node.orelse
            self.statements(selected)
            return
        if isinstance(node, ast.Assert):
            found, condition = self.try_static_value(node.test)
            if not found:
                self.unsupported(node, "dynamic indicator assertion")
            if not bool(condition):
                self.unsupported(node, "failed static indicator assertion")
            return
        if isinstance(node, ast.For):
            iterations = self.static_loop_iterations(node)
            if iterations is None:
                self.unsupported(node.iter, "dynamic indicator loop iterable")
            target_names = _loop_target_names(node.target)
            previous = {name: self.bindings.get(name) for name in target_names}
            present = {name for name in target_names if name in self.bindings}
            for values in iterations:
                for name, value in zip(target_names, values, strict=True):
                    self.bindings[name] = value
                self.statements(node.body)
            if node.orelse:
                self.statements(node.orelse)
            for name in target_names:
                if name in present:
                    prior = previous[name]
                    assert prior is not None
                    self.bindings[name] = prior
                else:
                    self.bindings.pop(name, None)
            return
        if isinstance(node, ast.Pass):
            return
        self.unsupported(node, f"indicator statement {type(node).__name__}")

    def statements(self: CompilerProtocol, statements: Sequence[ast.stmt]) -> None:
        index = 0
        while index < len(statements):
            consumed = self.absolute_difference_block(statements, index)
            if not consumed:
                consumed = self.lagged_array_pair_block(statements, index)
            if not consumed:
                consumed = self.age_filter_block(statements, index)
            if not consumed:
                consumed = self.opening_range_block(statements, index)
            if not consumed:
                consumed = self.inside_bar_block(statements, index)
            if not consumed:
                consumed = self.first_fire_block(statements, index)
            if consumed:
                index += consumed
                continue
            self.statement(statements[index])
            index += 1
