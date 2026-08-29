"""Compiler lifecycle, mutable state, and function assembly."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._indicator_ast import (
    _normalized_static_value,
    _parameter_type,
)
from ._indicator_contract import (
    IndicatorProgramCompileError,
)
from .indicator_compiler_bindings import (
    Binding,
    _CallableRef,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol


class CoreMixin:
    def compile_method(
        self: CompilerProtocol,
        name: str,
        *,
        kind: str = "helper",
        static_arguments: Mapping[str, Any] | None = None,
        callable_arguments: Mapping[str, _CallableRef] | None = None,
    ) -> str:
        normalized_static = {
            key: _normalized_static_value(value) for key, value in (static_arguments or {}).items()
        }
        callable_signature = {key: value.name for key, value in (callable_arguments or {}).items()}
        specialization = (
            name,
            json.dumps(
                {"static": normalized_static, "callable": callable_signature},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        is_specialized = bool(normalized_static or callable_signature)
        if is_specialized and specialization in self.compiled_specializations:
            return self.specialized_method_ids[specialization]
        if not is_specialized and name in self.compiled:
            return self.method_ids[name]
        node = self.methods.get(name)
        if node is None:
            raise IndicatorProgramCompileError(f"indicator helper was not found: {name}")
        if isinstance(node, ast.AsyncFunctionDef):
            self.unsupported(node, "async indicator helper")
        if name in self.compiling:
            self.unsupported(node, "recursive indicator helper")
        if is_specialized:
            function_id = self.specialized_method_ids.setdefault(
                specialization,
                self._next_function_id(),
            )
        else:
            function_id = self.method_ids.setdefault(name, self._next_function_id())
        self.compiling.add(name)

        previous_state = (
            self.current_function,
            self.current_nodes,
            self.bindings,
            self.return_node,
        )
        self.current_function = function_id
        self.current_nodes = []
        self.bindings = {}
        self.return_node = None
        parameter_records = []
        parameters = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        for argument in parameters:
            if argument.arg == "self":
                continue
            if argument.arg in normalized_static:
                self.bindings[argument.arg] = _StaticBinding(normalized_static[argument.arg])
                continue
            if argument.arg in (callable_arguments or {}):
                self.bindings[argument.arg] = (callable_arguments or {})[argument.arg]
                continue
            value_type = _parameter_type(argument.arg)
            parameter_node = self.emit(
                node,
                "parameter",
                value_type,
                parameters={"name": argument.arg},
            )
            self.bindings[argument.arg] = parameter_node
            parameter_records.append(
                {
                    "name": argument.arg,
                    "node": parameter_node,
                    "value_type": value_type,
                }
            )
        self.statements(node.body)
        if self.return_node is None:
            self.unsupported(node, "indicator function without an explicit return")
        function_record = {
            "id": function_id,
            "source_name": name,
            "kind": kind,
            "parameters": parameter_records,
            "node_ids": self.current_nodes,
            "return_node": self.return_node,
        }
        return_type = self.node_types[self.return_node]
        return_lookback = self.lookback(self.return_node)
        static_return = self.node_static_value(self.return_node)

        self.current_function, self.current_nodes, self.bindings, self.return_node = previous_state
        self.function_return_types[function_id] = return_type
        self.function_arities[function_id] = len(parameter_records)
        self.function_lookbacks[function_id] = return_lookback
        if static_return[0]:
            self.function_static_returns[function_id] = static_return[1]
        self.functions.append(function_record)
        self.compiling.remove(name)
        if is_specialized:
            self.compiled_specializations.add(specialization)
        else:
            self.compiled.add(name)
        return function_id

    def _next_function_id(self: CompilerProtocol) -> str:
        identifiers = [*self.method_ids.values(), *self.specialized_method_ids.values()]
        next_index = max((int(identifier[1:]) for identifier in identifiers), default=0) + 1
        return f"f{next_index}"


class CompilerState:
    def __init__(
        self,
        *,
        path: Path,
        methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
        class_constants: Mapping[str, Any],
        instance_constants: Mapping[str, Any] | None = None,
        module_functions: Mapping[str, ast.FunctionDef] | None = None,
    ) -> None:
        self.path = path
        self.methods = methods
        self.class_constants = class_constants
        self.instance_constants = dict(instance_constants or {})
        self.module_functions = dict(module_functions or {})
        self.method_ids: dict[str, str] = {}
        self.specialized_method_ids: dict[tuple[str, str], str] = {}
        self.compiling: set[str] = set()
        self.compiled: set[str] = set()
        self.compiled_specializations: set[tuple[str, str]] = set()
        self.function_return_types: dict[str, str] = {}
        self.function_static_returns: dict[str, Any] = {}
        self.function_arities: dict[str, int] = {}
        self.function_lookbacks: dict[str, dict[str, Any]] = {}
        self.functions: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.node_types: dict[str, str] = {}
        self.source_map: dict[str, dict[str, Any]] = {}
        self.required_input_columns: set[str] = set()
        self.produced_columns: set[str] = set()
        self.informative_nodes: list[str] = []
        self.opcodes: set[str] = set()
        self.current_function = ""
        self.current_nodes: list[str] = []
        self.bindings: dict[str, Binding] = {}
        self.return_node: str | None = None
