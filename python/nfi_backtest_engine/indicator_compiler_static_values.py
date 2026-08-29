"""Side-effect-free static expression and call evaluation."""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

from ._indicator_ast import (
    _normalized_static_value,
    _qualified_name,
)
from ._indicator_contract import _numeric_identifier_key
from .indicator_compiler_bindings import (
    _CallableRef,
    _DataProviderRef,
    _MappingBinding,
    _SequenceBinding,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol


class StaticValuesMixin:
    def try_static_value(self: CompilerProtocol, node: ast.expr) -> tuple[bool, Any]:
        """Evaluate a side-effect-free Python expression used for source routing."""
        if isinstance(node, ast.Constant):
            return True, node.value
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, _SequenceBinding):
                return True, bool(binding.items)
            if isinstance(binding, _DataProviderRef):
                return True, True
            if isinstance(binding, _MappingBinding):
                if all(isinstance(value, _StaticBinding) for value in binding.items.values()):
                    return True, {key: value.value for key, value in binding.items.items()}
                return False, None
            if isinstance(binding, _StaticBinding):
                return True, binding.value
            if isinstance(binding, str):
                record = self.nodes[_numeric_identifier_key({"id": binding}) - 1]
                if record["op"] == "literal":
                    return True, record["parameters"].get("value")
            if node.id in self.class_constants:
                return True, self.class_constants[node.id]
            if node.id == "object":
                return True, "object"
            return False, None
        if isinstance(node, ast.Attribute):
            qualified = _qualified_name(node)
            if qualified == "np.nan":
                return True, float("nan")
            if qualified == "np.inf":
                return True, float("inf")
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                if node.attr in self.instance_constants:
                    return True, self.instance_constants[node.attr]
                if node.attr in self.class_constants:
                    return True, self.class_constants[node.attr]
                return False, None
            found, base = self.try_static_value(node.value)
            if found and isinstance(base, Mapping) and node.attr in base:
                return True, base[node.attr]
            return False, None
        if isinstance(node, ast.Subscript):
            found_base, base = self.try_static_value(node.value)
            found_key, key = self.try_static_value(node.slice)
            if not found_base or not found_key:
                return False, None
            try:
                return True, base[key]
            except (KeyError, IndexError, TypeError):
                return False, None
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            values = [self.try_static_value(item) for item in node.elts]
            if not all(found for found, _ in values):
                return False, None
            sequence_items = [value for _, value in values]
            return (
                True,
                sequence_items if isinstance(node, ast.List | ast.Set) else tuple(sequence_items),
            )
        if isinstance(node, ast.Dict):
            items: dict[str, Any] = {}
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                if key_node is None:
                    return False, None
                found_key, key = self.try_static_value(key_node)
                found_value, value = self.try_static_value(value_node)
                if not found_key or not isinstance(key, str) or not found_value:
                    return False, None
                items[key] = value
            return True, items
        if isinstance(node, ast.UnaryOp):
            found, value = self.try_static_value(node.operand)
            if not found:
                return False, None
            operation = {
                ast.Not: operator.not_,
                ast.USub: operator.neg,
                ast.UAdd: operator.pos,
                ast.Invert: operator.invert,
            }.get(type(node.op))
            if operation is None:
                return False, None
            try:
                return True, operation(value)
            except (TypeError, ValueError):
                return False, None
        if isinstance(node, ast.BinOp):
            found_left, left = self.try_static_value(node.left)
            found_right, right = self.try_static_value(node.right)
            operation = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
                ast.Pow: operator.pow,
            }.get(type(node.op))
            if not found_left or not found_right or operation is None:
                return False, None
            try:
                return True, operation(left, right)
            except (ArithmeticError, TypeError, ValueError):
                return False, None
        if isinstance(node, ast.BoolOp):
            values = [self.try_static_value(item) for item in node.values]
            if not all(found for found, _ in values):
                return False, None
            resolved = [value for _, value in values]
            if isinstance(node.op, ast.And):
                return True, all(resolved)
            if isinstance(node.op, ast.Or):
                return True, any(resolved)
            return False, None
        if isinstance(node, ast.Compare):
            found_left, left = self.try_static_value(node.left)
            if not found_left:
                return False, None
            for operation_node, comparator in zip(node.ops, node.comparators, strict=True):
                found_right, right = self.try_static_value(comparator)
                if not found_right:
                    return False, None
                operation = {
                    ast.Eq: operator.eq,
                    ast.NotEq: operator.ne,
                    ast.Lt: operator.lt,
                    ast.LtE: operator.le,
                    ast.Gt: operator.gt,
                    ast.GtE: operator.ge,
                    ast.In: lambda item, container: item in container,
                    ast.NotIn: lambda item, container: item not in container,
                    ast.Is: operator.is_,
                    ast.IsNot: operator.is_not,
                }.get(type(operation_node))
                if operation is None:
                    return False, None
                try:
                    if not operation(left, right):
                        return True, False
                except (TypeError, ValueError):
                    return False, None
                left = right
            return True, True
        if isinstance(node, ast.IfExp):
            found, condition = self.try_static_value(node.test)
            if not found:
                return False, None
            return self.try_static_value(node.body if condition else node.orelse)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                    continue
                if not isinstance(value, ast.FormattedValue) or value.format_spec is not None:
                    return False, None
                found, resolved = self.try_static_value(value.value)
                if not found:
                    return False, None
                parts.append(str(resolved))
            return True, "".join(parts)
        if isinstance(node, ast.Call):
            return self._try_static_call(node)
        return False, None


class StaticCallsMixin:
    def _try_static_call(self: CompilerProtocol, node: ast.Call) -> tuple[bool, Any]:
        if any(keyword.arg is None for keyword in node.keywords):
            return False, None
        arguments = [self.try_static_value(argument) for argument in node.args]
        keywords = {
            str(keyword.arg): self.try_static_value(keyword.value) for keyword in node.keywords
        }
        if not all(found for found, _ in arguments) or not all(
            found for found, _ in keywords.values()
        ):
            return False, None
        values = [value for _, value in arguments]
        options = {name: value for name, (_, value) in keywords.items()}
        if isinstance(node.func, ast.Name):
            function = {
                "bool": bool,
                "float": float,
                "frozenset": frozenset,
                "int": int,
                "len": len,
                "list": list,
                "set": set,
                "str": str,
                "tuple": tuple,
            }.get(node.func.id)
            if function is None:
                return False, None
            try:
                return True, _normalized_static_value(function(*values, **options))
            except (TypeError, ValueError, OverflowError):
                return False, None
        if not isinstance(node.func, ast.Attribute):
            return False, None
        found, base = self.try_static_value(node.func.value)
        if not found:
            return False, None
        method = node.func.attr
        try:
            if method == "get" and isinstance(base, Mapping):
                return True, base.get(*values)
            if method == "items" and isinstance(base, Mapping) and not values and not options:
                return True, list(base.items())
            if method in {"partition", "rsplit", "split", "startswith", "endswith"} and isinstance(
                base, str
            ):
                return True, _normalized_static_value(getattr(base, method)(*values, **options))
        except (TypeError, ValueError):
            return False, None
        return False, None

    def static_value(self: CompilerProtocol, node: ast.expr) -> Any:
        found, value = self.try_static_value(node)
        return value if found else None

    def resolved_callable(self: CompilerProtocol, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, _CallableRef):
                return binding.name
            return node.id
        name = _qualified_name(node)
        return name or "<dynamic>"
