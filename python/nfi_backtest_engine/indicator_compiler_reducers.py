"""Indicator compiler reducers concern."""

from __future__ import annotations

import ast

from ._indicator_ast import (
    _recognized_tag_appender,
)
from ._indicator_contract import (
    _boolean_result_type,
)
from .indicator_compiler_arguments import (
    _recognized_sequence_reducer,
)
from .indicator_compiler_bindings import (
    _SequenceBinding,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol


class ReducersMixin:
    def reduce_sequence_call(
        self: CompilerProtocol, call: ast.Call, callable_name: str
    ) -> str | None:
        function = self.module_functions.get(callable_name)
        operator_name = _recognized_sequence_reducer(function) if function is not None else None
        if operator_name is None:
            return None
        if len(call.args) != 1 or call.keywords or not isinstance(call.args[0], ast.Name):
            self.unsupported(call, "sequence reducer signature")
        binding = self.bindings.get(call.args[0].id)
        if not isinstance(binding, _SequenceBinding):
            self.unsupported(call.args[0], "sequence reducer input")
        dynamic: list[str] = []
        static_values: list[bool] = []
        for item in binding.items:
            if isinstance(item, _StaticBinding):
                if not isinstance(item.value, bool):
                    self.unsupported(call.args[0], "non-Boolean sequence reducer value")
                static_values.append(item.value)
            elif isinstance(item, str):
                if self.node_types[item] not in {"bool-scalar", "bool-column"}:
                    self.unsupported(call.args[0], "non-Boolean sequence reducer value")
                dynamic.append(item)
            else:
                self.unsupported(call.args[0], "nested sequence reducer value")
        absorbing = operator_name != "and"
        if absorbing in static_values:
            return self.literal(call, absorbing)
        if not dynamic:
            return self.literal(call, not absorbing)
        if len(dynamic) == 1:
            return dynamic[0]
        return self.emit(
            call,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in dynamic)),
            inputs=dynamic,
            parameters={"operator": operator_name},
            lookback=self.merged_lookback(dynamic),
        )

    def append_tag_call(self: CompilerProtocol, call: ast.Call, callable_name: str) -> str | None:
        function = self.module_functions.get(callable_name)
        if function is None or not _recognized_tag_appender(function):
            return None
        if len(call.args) != 3 or call.keywords or not isinstance(call.args[0], ast.Name):
            self.unsupported(call, "tag append helper signature")
        target_name = call.args[0].id
        target = self.bindings.get(target_name)
        if not isinstance(target, str) or self.node_types[target] != "string-column":
            self.unsupported(call.args[0], "tag append target")
        mask = self.expression(call.args[1])
        tag = self.expression(call.args[2])
        if self.node_types[mask] not in {"bool-scalar", "bool-column"}:
            self.unsupported(call.args[1], "tag append mask")
        if self.node_types[tag] != "string-scalar":
            self.unsupported(call.args[2], "tag append value")
        result = self.emit(
            call,
            "masked-string-append",
            "string-column",
            inputs=[target, mask, tag],
            lookback=self.merged_lookback([target, mask, tag]),
        )
        self.bindings[target_name] = result
        return result
