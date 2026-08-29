"""Typed bindings and environment resolution for indicator compilation."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._indicator_ast import (
    _loop_target_names,
    _small_static_candidate,
)

if TYPE_CHECKING:
    from .indicator_compiler_protocol import CompilerProtocol


@dataclass(frozen=True)
class _CallableRef:
    name: str


@dataclass(frozen=True)
class _StaticBinding:
    value: Any


@dataclass(frozen=True)
class _LambdaRef:
    parameters: tuple[str, ...]
    body: ast.expr


@dataclass
class _SequenceBinding:
    items: list[Any]


@dataclass(frozen=True)
class _DataProviderRef:
    pass


@dataclass(frozen=True)
class _ColumnBundleBinding:
    dataframe: str
    columns: tuple[tuple[str, str], ...]


@dataclass
class _MappingBinding:
    items: dict[Any, Any]


Binding = (
    str
    | _CallableRef
    | _StaticBinding
    | _LambdaRef
    | _SequenceBinding
    | _DataProviderRef
    | _ColumnBundleBinding
    | _MappingBinding
)


class BindingsLoweringMixin:
    def static_reference(self: CompilerProtocol, node: ast.expr) -> _StaticBinding | None:
        """Keep static containers out of IR until a concrete value is read."""
        if not isinstance(
            node,
            ast.Name
            | ast.Attribute
            | ast.Subscript
            | ast.Call
            | ast.JoinedStr
            | ast.List
            | ast.Tuple
            | ast.Set
            | ast.Dict
            | ast.Compare
            | ast.IfExp,
        ):
            return None
        found, value = self.try_static_value(node)
        if not found or not (
            isinstance(value, list | tuple | Mapping)
            or isinstance(node, ast.Compare | ast.IfExp | ast.JoinedStr)
        ):
            return None
        return _StaticBinding(value)

    @staticmethod
    def sequence_reference(node: ast.expr) -> _SequenceBinding | None:
        if isinstance(node, ast.List) and not node.elts:
            return _SequenceBinding([])
        return None

    @staticmethod
    def mapping_reference(node: ast.expr) -> _MappingBinding | None:
        if isinstance(node, ast.Dict) and not node.keys:
            return _MappingBinding({})
        return None

    def mapping_write(self: CompilerProtocol, target: ast.Subscript, value_node: ast.expr) -> bool:
        if not isinstance(target.value, ast.Name):
            return False
        mapping = self.bindings.get(target.value.id)
        if not isinstance(mapping, _MappingBinding):
            return False
        found_key, key = self.try_static_value(target.slice)
        if not found_key or not isinstance(key, bool | int | float | str):
            self.unsupported(target.slice, "dynamic mapping key")
        found_value, value = (
            self.try_static_value(value_node)
            if _small_static_candidate(value_node)
            else (False, None)
        )
        mapping.items[key] = _StaticBinding(value) if found_value else self.expression(value_node)
        return True

    def static_loop_iterations(
        self: CompilerProtocol, node: ast.For
    ) -> list[tuple[Binding, ...]] | None:
        target_names = _loop_target_names(node.target)
        if len(target_names) == 1 and isinstance(node.iter, ast.Tuple | ast.List):
            bound_items: list[tuple[Binding, ...]] = []
            for element in node.iter.elts:
                if not isinstance(element, ast.Name):
                    bound_items = []
                    break
                binding = self.bindings.get(element.id)
                if not isinstance(binding, _MappingBinding | _SequenceBinding):
                    bound_items = []
                    break
                bound_items.append((binding,))
            if bound_items:
                return bound_items
        if (
            isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Attribute)
            and node.iter.func.attr == "items"
            and isinstance(node.iter.func.value, ast.Name)
            and not node.iter.args
            and not node.iter.keywords
        ):
            mapping = self.bindings.get(node.iter.func.value.id)
            if isinstance(mapping, _MappingBinding) and len(target_names) == 2:
                return [(_StaticBinding(key), value) for key, value in mapping.items.items()]
        found, iterable = self.try_static_value(node.iter)
        if not found or not isinstance(iterable, list | tuple | Mapping):
            return None
        values = iterable if not isinstance(iterable, Mapping) else iterable.keys()
        if len(target_names) != 1:
            return None
        return [(_StaticBinding(value),) for value in values]

    @staticmethod
    def data_provider_reference(node: ast.expr) -> _DataProviderRef | None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "dp"
        ):
            return _DataProviderRef()
        return None

    def column_bundle_reference(
        self: CompilerProtocol, node: ast.expr
    ) -> _ColumnBundleBinding | None:
        if not (
            isinstance(node, ast.Call)
            and self.resolved_callable(node.func) == "pd.DataFrame"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Dict)
        ):
            return None
        options = {keyword.arg: keyword.value for keyword in node.keywords}
        if set(options) != {"index"} or None in options:
            self.unsupported(node, "pandas column-bundle signature")
        index = options["index"]
        if not (
            isinstance(index, ast.Attribute)
            and index.attr == "index"
            and isinstance(index.value, ast.Name)
        ):
            self.unsupported(index, "pandas column-bundle index")
        dataframe = self.bindings.get(index.value.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(index, "pandas column-bundle dataframe")
        columns: list[tuple[str, str]] = []
        for key_node, value_node in zip(
            node.args[0].keys,
            node.args[0].values,
            strict=True,
        ):
            if key_node is None:
                self.unsupported(node.args[0], "expanded pandas column bundle")
            found, key = self.try_static_value(key_node)
            if not found or not isinstance(key, str) or not key:
                self.unsupported(key_node, "dynamic pandas column-bundle name")
            if any(existing == key for existing, _ in columns):
                self.unsupported(key_node, "duplicate pandas column-bundle name")
            value = self.expression(value_node)
            if not self.node_types[value].endswith("-column"):
                self.unsupported(value_node, "pandas column-bundle value")
            columns.append((key, value))
        if not columns:
            self.unsupported(node, "empty pandas column bundle")
        return _ColumnBundleBinding(dataframe=dataframe, columns=tuple(columns))
