"""Indicator compiler patterns concern."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from ._indicator_ast import (
    _AGE_FILTER_TEMPLATE,
    _OPENING_RANGE_TEMPLATE,
    _assigned_name,
    _ast_equal,
    _dataframe_column_target,
    _is_absolute_difference_write,
    _is_array_index_nan_write,
    _qualified_name,
    _template_statements,
)
from ._indicator_contract import _add_finite_lookback
from .indicator_compiler_arrays import _zero_index_target
from .indicator_compiler_protocol import CompilerProtocol


def _lagged_array_allocation(statement: ast.stmt) -> tuple[str, ast.expr] | None:
    if not (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and isinstance(statement.value, ast.Call)
        and _qualified_name(statement.value.func) == "np.empty_like"
        and len(statement.value.args) == 1
        and not statement.value.keywords
    ):
        return None
    return statement.targets[0].id, statement.value.args[0]




def _lagged_array_write(statement: ast.stmt, output: str, source: ast.expr) -> bool:
    if not (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Subscript)
        and isinstance(statement.targets[0].value, ast.Name)
        and statement.targets[0].value.id == output
        and isinstance(statement.targets[0].slice, ast.Slice)
        and isinstance(statement.value, ast.Subscript)
        and _ast_equal(statement.value.value, source)
        and isinstance(statement.value.slice, ast.Slice)
    ):
        return False
    target_slice = statement.targets[0].slice
    source_slice = statement.value.slice
    return (
        isinstance(target_slice.lower, ast.Constant)
        and target_slice.lower.value == 1
        and target_slice.upper is None
        and target_slice.step is None
        and source_slice.lower is None
        and isinstance(source_slice.upper, ast.UnaryOp)
        and isinstance(source_slice.upper.op, ast.USub)
        and isinstance(source_slice.upper.operand, ast.Constant)
        and source_slice.upper.operand.value == 1
        and source_slice.step is None
    )


class PatternsMixin:
    def absolute_difference_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        if index + 2 >= len(statements):
            return 0
        allocated, warmup, difference = statements[index : index + 3]
        if not (
            isinstance(allocated, ast.Assign)
            and len(allocated.targets) == 1
            and isinstance(allocated.targets[0], ast.Name)
            and isinstance(allocated.value, ast.Call)
            and self.resolved_callable(allocated.value.func) == "np.empty_like"
            and len(allocated.value.args) == 1
            and len(allocated.value.keywords) == 1
            and allocated.value.keywords[0].arg == "dtype"
            and _qualified_name(allocated.value.keywords[0].value) == "np.float64"
        ):
            return 0
        output_name = allocated.targets[0].id
        source_node = allocated.value.args[0]
        if not _is_array_index_nan_write(warmup, output_name, 0):
            return 0
        if not _is_absolute_difference_write(difference, output_name, source_node):
            return 0
        source = self.expression(source_node)
        if self.node_types[source] != "f64-column":
            self.unsupported(source_node, "absolute difference source type")
        self.bindings[output_name] = self.emit(
            allocated,
            "array-call",
            "f64-column",
            inputs=[source],
            parameters={
                "family": "numpy",
                "name": "absolute-difference",
                "arguments": {},
            },
            lookback=_add_finite_lookback(self.lookback(source), 1),
        )
        return 3

    def lagged_array_pair_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        candidate = statements[index : index + 5]
        if len(candidate) != 5:
            return 0
        first = _lagged_array_allocation(candidate[0])
        second = _lagged_array_allocation(candidate[1])
        warmup = candidate[2]
        if (
            first is None
            or second is None
            or not isinstance(warmup, ast.Assign)
            or len(warmup.targets) != 2
            or not all(
                _zero_index_target(target, name)
                for target, name in zip(
                    warmup.targets,
                    (first[0], second[0]),
                    strict=True,
                )
            )
            or not isinstance(warmup.value, ast.Attribute)
            or _qualified_name(warmup.value) != "np.nan"
            or not _lagged_array_write(candidate[3], first[0], first[1])
            or not _lagged_array_write(candidate[4], second[0], second[1])
        ):
            return 0
        for allocated, (output_name, source_node) in zip(
            candidate[:2],
            (first, second),
            strict=True,
        ):
            source = self.expression(source_node)
            value_type = self.node_types[source]
            if not value_type.endswith("-column"):
                self.unsupported(source_node, "lagged array source type")
            self.bindings[output_name] = self.emit(
                allocated,
                "shift",
                value_type,
                inputs=[source],
                parameters={"periods": 1},
                lookback=_add_finite_lookback(self.lookback(source), 1),
            )
        return len(candidate)

    def age_filter_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_AGE_FILTER_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (
            len(candidate) == len(expected)
            and isinstance(candidate[0], ast.Assign)
            and len(candidate[0].targets) == 1
            and _dataframe_column_target(candidate[0].targets[0]) == ("df", "bt_agefilter_ok")
        ):
            return 0
        if not _ast_equal(
            ast.Module(body=list(candidate), type_ignores=[]),
            ast.Module(body=list(expected), type_ignores=[]),
        ):
            return 0
        frame = self.bindings.get("df")
        age_days = self.class_constants.get("bt_min_age_days")
        if not isinstance(frame, str) or self.node_types[frame] != "dataframe":
            self.unsupported(candidate[0], "age-filter dataframe")
        if not isinstance(age_days, int) or isinstance(age_days, bool) or age_days < 0:
            self.unsupported(candidate[1], "age-filter day count")
        row_index = self.emit(
            candidate[1],
            "row-index",
            "int-column",
            inputs=[frame],
            lookback=self.lookback(frame),
        )
        threshold = self.literal(candidate[1], 12 * 24 * age_days)
        enabled = self.emit(
            candidate[1],
            "compare",
            "bool-column",
            inputs=[row_index, threshold],
            parameters={"operator": "greater-than"},
            lookback=self.merged_lookback([row_index, threshold]),
        )
        written = self.emit(
            candidate[-1],
            "column-write",
            "dataframe",
            inputs=[frame, enabled],
            parameters={"column": "bt_agefilter_ok"},
            lookback=self.merged_lookback([frame, enabled]),
        )
        self.bindings["df"] = written
        self.produced_columns.add("bt_agefilter_ok")
        return len(expected)

    def opening_range_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_OPENING_RANGE_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (len(candidate) == len(expected) and _assigned_name(candidate[0]) == "_or_day"):
            return 0
        if not _ast_equal(
            ast.Module(body=list(candidate), type_ignores=[]),
            ast.Module(body=list(expected), type_ignores=[]),
        ):
            return 0
        frame = self.bindings.get("df")
        if not isinstance(frame, str) or self.node_types[frame] != "dataframe":
            self.unsupported(candidate[0], "opening-range dataframe")
        source_nodes = [
            ast.Subscript(
                value=ast.Name(id="df", ctx=ast.Load()),
                slice=ast.Constant(value=name),
                ctx=ast.Load(),
            )
            for name in ("date", "high", "low")
        ]
        for source in source_nodes:
            ast.copy_location(source, candidate[0])
        date, high, low = [self.expression(source) for source in source_nodes]
        for output_name, operation in (
            ("orange_h_col", "opening-range-high"),
            ("orange_l_col", "opening-range-low"),
        ):
            self.bindings[output_name] = self.emit(
                candidate[-1],
                "array-call",
                "f64-column",
                inputs=[date, high, low],
                parameters={
                    "family": "native",
                    "name": operation,
                    "arguments": {"cutoff_hour": 4},
                },
                lookback={
                    "kind": "function-defined",
                    "candles": None,
                    "expression": operation,
                    "causal": True,
                },
            )
        return len(expected)
