"""Indicator compiler arrays concern."""

from __future__ import annotations

import ast

from ._indicator_ast import (
    _indicator_output_names,
    _qualified_name,
)
from ._indicator_contract import (
    _array_call_result_type,
)
from .indicator_compiler_arguments import (
    _bind_helper_arguments,
)
from .indicator_compiler_bindings import (
    Binding,
    _CallableRef,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol
from .indicator_compiler_windows import (
    _literal_keyword_arguments,
)


def _zero_index_target(node: ast.expr, name: str) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
    )


class ArraysMixin:
    def array_index_write(
        self: CompilerProtocol,
        target: ast.Subscript,
        value_node: ast.expr,
    ) -> bool:
        if not isinstance(target.value, ast.Name):
            return False
        source = self.bindings.get(target.value.id)
        if not isinstance(source, str) or not self.node_types[source].endswith("-column"):
            return False
        found_index, index = self.try_static_value(target.slice)
        found_value, value = self.try_static_value(value_node)
        if (
            not found_index
            or index != 0
            or not found_value
            or not isinstance(value, int | float)
            or isinstance(value, bool)
        ):
            self.unsupported(target, "array index write")
        replacement = self.literal(value_node, value)
        self.bindings[target.value.id] = self.emit(
            target,
            "array-call",
            self.node_types[source],
            inputs=[source, replacement],
            parameters={
                "family": "native",
                "name": "set-index",
                "arguments": {"index": 0},
            },
            lookback=self.merged_lookback([source, replacement]),
        )
        return True

    def multi_output_assignment(
        self: CompilerProtocol, target: ast.Tuple | ast.List, node: ast.Call
    ) -> None:
        callable_name = self.resolved_callable(node.func)
        inlined = self.inline_tuple_helper_call(node, callable_name)
        if inlined is not None:
            if len(inlined) != len(target.elts):
                self.unsupported(node, "indicator helper tuple output arity")
            for element, value in zip(target.elts, inlined, strict=True):
                if not isinstance(element, ast.Name):
                    self.unsupported(element, "indicator helper tuple assignment target")
                self.bindings[element.id] = value
            return
        output_names = _indicator_output_names(callable_name)
        if output_names is None or len(output_names) != len(target.elts):
            self.unsupported(node, "indicator tuple output contract")
        inputs, parameters = self.indicator_call_parts(node, callable_name)
        for element, output_name in zip(target.elts, output_names, strict=True):
            if not isinstance(element, ast.Name):
                self.unsupported(element, "indicator tuple assignment target")
            self.bindings[element.id] = self.emit_indicator_call(
                node,
                callable_name,
                inputs,
                parameters,
                output=output_name,
            )

    def inline_tuple_helper_call(
        self: CompilerProtocol,
        call: ast.Call,
        callable_name: str,
    ) -> list[str] | None:
        if not callable_name.startswith("self."):
            return None
        method = self.methods.get(callable_name.removeprefix("self."))
        if method is None or isinstance(method, ast.AsyncFunctionDef):
            return None
        body = method.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if not (
            body
            and isinstance(body[-1], ast.Return)
            and isinstance(body[-1].value, ast.Tuple | ast.List)
            and all(isinstance(statement, ast.Assign | ast.AnnAssign) for statement in body[:-1])
        ):
            return None
        bound = _bind_helper_arguments(call, method, self)
        argument_bindings: dict[str, Binding] = {}
        for name, argument in bound:
            if isinstance(argument, ast.Name):
                existing = self.bindings.get(argument.id)
                if isinstance(existing, _CallableRef):
                    argument_bindings[name] = existing
                    continue
            found, value = self.try_static_value(argument)
            argument_bindings[name] = _StaticBinding(value) if found else self.expression(argument)
        previous = self.bindings
        self.bindings = argument_bindings
        try:
            self.statements(body[:-1])
            returned = body[-1]
            assert isinstance(returned, ast.Return)
            assert isinstance(returned.value, ast.Tuple | ast.List)
            return [self.expression(value) for value in returned.value.elts]
        finally:
            self.bindings = previous

    def array_call(self: CompilerProtocol, node: ast.Call, callable_name: str) -> str:
        name = callable_name.removeprefix("np.")
        if name == "arange":
            if (
                len(node.args) != 1
                or node.keywords
                or not isinstance(node.args[0], ast.Attribute)
                or node.args[0].attr != "size"
            ):
                self.unsupported(node, "numpy arange signature")
            source = self.expression(node.args[0].value)
            if not self.node_types[source].endswith("-column"):
                self.unsupported(node.args[0].value, "numpy arange size source")
            frames = {
                binding
                for binding in self.bindings.values()
                if isinstance(binding, str)
                and self.node_types.get(binding) == "dataframe"
            }
            if len(frames) != 1:
                self.unsupported(node.args[0], "numpy arange dataframe")
            frame = frames.pop()
            return self.emit(
                node,
                "row-index",
                "int-column",
                inputs=[frame],
                lookback=self.lookback(frame),
            )
        if name == "zeros_like":
            if len(node.args) != 1:
                self.unsupported(node, "numpy zeros_like signature")
            if len(node.keywords) > 1:
                self.unsupported(node, "numpy zeros_like signature")
            explicit_float64 = bool(node.keywords)
            if explicit_float64:
                keyword = node.keywords[0]
                if keyword.arg != "dtype" or _qualified_name(keyword.value) != "np.float64":
                    self.unsupported(keyword.value, "numpy zeros_like dtype")
            inputs = [self.expression(node.args[0])]
            template_type = self.node_types[inputs[0]]
            if template_type != "f64-column" and not (
                explicit_float64 and template_type == "dynamic"
            ):
                self.unsupported(node.args[0], "numpy zeros_like template type")
            return self.emit(
                node,
                "array-call",
                "f64-column",
                inputs=inputs,
                parameters={"family": "numpy", "name": name, "arguments": {}},
                lookback=self.merged_lookback(inputs),
            )
        if name == "full_like":
            if len(node.args) != 2 or node.keywords:
                self.unsupported(node, "numpy full_like signature")
            inputs = [self.expression(argument) for argument in node.args]
            value_type = self.node_types[inputs[0]]
            if not value_type.endswith("-column"):
                self.unsupported(node.args[0], "numpy full_like template type")
            return self.emit(
                node,
                "array-call",
                value_type,
                inputs=inputs,
                parameters={"family": "numpy", "name": name, "arguments": {}},
                lookback=self.merged_lookback(inputs),
            )
        if name == "divide":
            if len(node.args) != 2:
                self.unsupported(node, "numpy divide signature")
            options: dict[str, ast.expr] = {}
            for keyword in node.keywords:
                if keyword.arg not in {"out", "where"}:
                    self.unsupported(keyword.value, "numpy divide keyword arguments")
                if keyword.arg in options:
                    self.unsupported(keyword.value, f"duplicate numpy divide {keyword.arg}")
                options[str(keyword.arg)] = keyword.value
            if set(options) != {"out", "where"}:
                self.unsupported(node, "numpy divide requires explicit out and where")
            inputs = [
                self.expression(node.args[0]),
                self.expression(node.args[1]),
                self.expression(options["out"]),
                self.expression(options["where"]),
            ]
            if any(self.node_types[item] != "f64-column" for item in inputs[:3]):
                self.unsupported(node, "numpy divide numeric column types")
            if self.node_types[inputs[3]] != "bool-column":
                self.unsupported(options["where"], "numpy divide where mask type")
            return self.emit(
                node,
                "array-call",
                "f64-column",
                inputs=inputs,
                parameters={"family": "numpy", "name": name, "arguments": {}},
                lookback=self.merged_lookback(inputs),
            )
        inputs = [self.expression(argument) for argument in node.args]
        parameters = _literal_keyword_arguments(node, self)
        return self.emit(
            node,
            "array-call",
            _array_call_result_type(callable_name, inputs, self.node_types),
            inputs=inputs,
            parameters={"family": "numpy", "name": name, "arguments": parameters},
            lookback=self.merged_lookback(inputs),
        )
