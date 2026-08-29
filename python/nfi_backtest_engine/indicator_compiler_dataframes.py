"""Data-provider acquisition and dataframe state transitions."""

from __future__ import annotations

import ast

from ._indicator_ast import (
    _safe_expression,
    _small_static_candidate,
)
from ._indicator_contract import _numeric_identifier_key
from .indicator_compiler_arguments import (
    _bind_pair_dataframe_arguments,
)
from .indicator_compiler_bindings import (
    _ColumnBundleBinding,
    _DataProviderRef,
    _SequenceBinding,
    _StaticBinding,
)
from .indicator_compiler_protocol import CompilerProtocol


class ProvidersMixin:
    def concat_column_bundle(
        self: CompilerProtocol, node: ast.Call, callable_name: str
    ) -> str | None:
        if callable_name != "pd.concat":
            return None
        if len(node.args) != 1 or not isinstance(node.args[0], ast.List):
            self.unsupported(node, "pandas column concat signature")
        if len(node.args[0].elts) != 2:
            self.unsupported(node.args[0], "pandas column concat inputs")
        base_node, bundle_node = node.args[0].elts
        if not isinstance(base_node, ast.Name) or not isinstance(bundle_node, ast.Name):
            self.unsupported(node.args[0], "pandas column concat bindings")
        base = self.bindings.get(base_node.id)
        bundle = self.bindings.get(bundle_node.id)
        if not isinstance(base, str) or self.node_types[base] != "dataframe":
            self.unsupported(base_node, "pandas column concat dataframe")
        if not isinstance(bundle, _ColumnBundleBinding) or bundle.dataframe != base:
            self.unsupported(bundle_node, "pandas column concat index identity")
        options = {keyword.arg: self.try_static_value(keyword.value) for keyword in node.keywords}
        if (
            set(options) != {"axis", "copy"}
            or options["axis"] != (True, 1)
            or options["copy"] != (True, False)
        ):
            self.unsupported(node, "pandas column concat options")
        dataframe = base
        for column, value in bundle.columns:
            dataframe = self.emit(
                node,
                "column-write",
                "dataframe",
                inputs=[dataframe, value],
                parameters={"column": column, "collision": "reject"},
                lookback=self.merged_lookback([dataframe, value]),
            )
            self.produced_columns.add(column)
        return dataframe

    def frame_source_call(self: CompilerProtocol, call: ast.Call) -> str | None:
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "get_pair_dataframe":
            return None
        provider = call.func.value
        is_provider = (
            isinstance(provider, ast.Name)
            and isinstance(self.bindings.get(provider.id), _DataProviderRef)
        ) or (
            isinstance(provider, ast.Attribute)
            and isinstance(provider.value, ast.Name)
            and provider.value.id == "self"
            and provider.attr == "dp"
        )
        if not is_provider:
            self.unsupported(provider, "get_pair_dataframe provider")
        arguments = _bind_pair_dataframe_arguments(call, self)
        found_timeframe, timeframe = self.try_static_value(arguments["timeframe"])
        if not found_timeframe or not isinstance(timeframe, str):
            self.unsupported(arguments["timeframe"], "dynamic frame-source timeframe")
        found_pair, pair = self.try_static_value(arguments["pair"])
        if found_pair:
            if not isinstance(pair, str) or not pair:
                self.unsupported(arguments["pair"], "frame-source literal pair")
            pair_selector = {"kind": "literal", "value": pair}
        else:
            pair_node = self.expression(arguments["pair"])
            record = self.nodes[_numeric_identifier_key({"id": pair_node}) - 1]
            if not (
                record["op"] == "metadata-read"
                and record["value_type"] == "string-scalar"
                and record["parameters"].get("key") == "pair"
            ):
                self.unsupported(arguments["pair"], "dynamic frame-source pair selector")
            pair_selector = {"kind": "metadata", "key": "pair"}
        return self.emit(
            call,
            "frame-source",
            "dataframe",
            parameters={"pair": pair_selector, "timeframe": timeframe},
        )

    def frame_empty_guard(self: CompilerProtocol, node: ast.If) -> bool:
        if not (
            isinstance(node.test, ast.Attribute)
            and node.test.attr == "empty"
            and isinstance(node.test.value, ast.Name)
            and not node.orelse
            and len(node.body) == 1
        ):
            return False
        name = node.test.value.id
        binding = self.bindings.get(name)
        if not isinstance(binding, str) or self.node_types[binding] != "dataframe":
            return False
        branch = node.body[0]
        if isinstance(branch, ast.Return):
            if not isinstance(branch.value, ast.Name) or branch.value.id != name:
                return False
        elif not isinstance(branch, ast.Continue):
            return False
        checked = self.emit(
            node,
            "frame-nonempty",
            "dataframe",
            inputs=[binding],
            lookback=self.lookback(binding),
        )
        self.bindings[name] = checked
        return True


class GuardsMixin:
    def frame_drop_guard(self: CompilerProtocol, node: ast.If) -> bool:
        if not (
            isinstance(node.test, ast.Compare)
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.In)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Attribute)
            and node.test.comparators[0].attr == "columns"
            and isinstance(node.test.comparators[0].value, ast.Name)
            and not node.orelse
            and len(node.body) == 1
        ):
            return False
        dataframe_name = node.test.comparators[0].value.id
        branch = node.body[0]
        assignment_target: str | None = None
        if (
            isinstance(branch, ast.Assign)
            and len(branch.targets) == 1
            and isinstance(branch.targets[0], ast.Name)
            and isinstance(branch.value, ast.Call)
        ):
            assignment_target = branch.targets[0].id
            call = branch.value
        elif isinstance(branch, ast.Expr) and isinstance(branch.value, ast.Call):
            call = branch.value
        else:
            return False
        if not (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "drop"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == dataframe_name
            and not call.args
        ):
            return False
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        if None in keywords or "columns" not in keywords:
            return False
        if assignment_target is not None:
            if assignment_target != dataframe_name or set(keywords) != {"columns"}:
                return False
        elif set(keywords) != {"columns", "inplace"}:
            return False
        if assignment_target is None:
            found_inplace, inplace = self.try_static_value(keywords["inplace"])
            if not found_inplace or inplace is not True:
                return False
        found_test, test_column = self.try_static_value(node.test.left)
        found_drop, drop_column = self.try_static_value(keywords["columns"])
        if (
            not found_test
            or not found_drop
            or not isinstance(test_column, str)
            or test_column != drop_column
        ):
            return False
        dataframe = self.bindings.get(dataframe_name)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(node, "frame drop dataframe")
        dropped = self.emit(
            node,
            "frame-drop-if-present",
            "dataframe",
            inputs=[dataframe],
            parameters={"column": test_column},
            lookback=self.lookback(dataframe),
        )
        self.bindings[dataframe_name] = dropped
        return True

    def inplace_forward_fill(self: CompilerProtocol, node: ast.Call) -> bool:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "ffill"
            and isinstance(node.func.value, ast.Name)
            and not node.args
            and len(node.keywords) == 1
            and node.keywords[0].arg == "inplace"
        ):
            return False
        found, inplace = self.try_static_value(node.keywords[0].value)
        if not found or inplace is not True:
            return False
        name = node.func.value.id
        base = self.bindings.get(name)
        if not isinstance(base, str) or self.node_types[base] != "dataframe":
            self.unsupported(node.func.value, "in-place forward-fill dataframe")
        filled = self.emit(
            node,
            "fill",
            "dataframe",
            inputs=[base],
            parameters={"direction": "forward"},
            lookback={
                "kind": "recursive",
                "candles": None,
                "expression": _safe_expression(node),
                "causal": bool(self.lookback(base)["causal"]),
            },
        )
        self.bindings[name] = filled
        return True

    def append_sequence(self: CompilerProtocol, call: ast.Call) -> bool:
        if not (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "append"
            and isinstance(call.func.value, ast.Name)
        ):
            return False
        binding = self.bindings.get(call.func.value.id)
        if not isinstance(binding, _SequenceBinding):
            return False
        if len(call.args) != 1 or call.keywords:
            self.unsupported(call, "sequence append signature")
        found, value = (
            self.try_static_value(call.args[0])
            if _small_static_candidate(call.args[0])
            else (False, None)
        )
        binding.items.append(_StaticBinding(value) if found else self.expression(call.args[0]))
        return True
