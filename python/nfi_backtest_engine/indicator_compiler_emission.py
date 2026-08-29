"""Indicator compiler emission concern."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any, Never

from ._indicator_ast import (
    _is_json_value,
)
from ._indicator_contract import (
    _literal_parameters,
    _literal_type,
    _location,
    _merge_lookbacks,
    _numeric_identifier_key,
    _unsupported,
    _zero_lookback,
)
from .indicator_compiler_protocol import CompilerProtocol


class EmissionMixin:
    def literal(self: CompilerProtocol, node: ast.AST, value: Any) -> str:
        if not _is_json_value(value):
            self.unsupported(node, "non-JSON indicator literal")
        return self.emit(
            node,
            "literal",
            _literal_type(value),
            parameters=_literal_parameters(value),
        )

    def emit(
        self: CompilerProtocol,
        source_node: ast.AST,
        opcode: str,
        value_type: str,
        *,
        inputs: Sequence[str] = (),
        parameters: Mapping[str, Any] | None = None,
        lookback: Mapping[str, Any] | None = None,
    ) -> str:
        identifier = f"n{len(self.nodes) + 1}"
        record = {
            "id": identifier,
            "function": self.current_function,
            "source_order": len(self.current_nodes),
            "op": opcode,
            "value_type": value_type,
            "inputs": list(inputs),
            "parameters": dict(parameters or {}),
            "lookback": dict(lookback or _zero_lookback()),
        }
        self.nodes.append(record)
        self.node_types[identifier] = value_type
        self.current_nodes.append(identifier)
        self.opcodes.add(opcode)
        self.source_map[identifier] = _location(source_node)
        return identifier

    def lookback(self: CompilerProtocol, node_id: str) -> dict[str, Any]:
        return dict(self.nodes[_numeric_identifier_key({"id": node_id}) - 1]["lookback"])

    def node_static_value(self: CompilerProtocol, node_id: str) -> tuple[bool, Any]:
        current = node_id
        visited: set[str] = set()
        while current not in visited:
            visited.add(current)
            record = self.nodes[_numeric_identifier_key({"id": current}) - 1]
            if record["op"] == "literal":
                parameters = record["parameters"]
                if "value" in parameters:
                    return True, parameters["value"]
                special = parameters.get("special")
                if special == "nan":
                    return True, float("nan")
                if special == "+infinity":
                    return True, float("inf")
                if special == "-infinity":
                    return True, float("-inf")
                return False, None
            if record["op"] in {"return", "cast"} and len(record["inputs"]) == 1:
                current = record["inputs"][0]
                continue
            return False, None
        return False, None

    def merged_lookback(self: CompilerProtocol, node_ids: Sequence[str]) -> dict[str, Any]:
        return _merge_lookbacks([self.lookback(node_id) for node_id in node_ids])

    def unsupported(self: CompilerProtocol, node: ast.AST, description: str) -> Never:
        _unsupported(node, description)
