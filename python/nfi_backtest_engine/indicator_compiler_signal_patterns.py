"""Inside-bar, first-fire, and signal output source patterns."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from ._indicator_ast import (
    _FIRST_FIRE_TEMPLATE,
    _INSIDE_BAR_TEMPLATE,
    _assigned_name,
    _ast_equal,
    _literal_string,
    _template_statements,
)
from ._indicator_contract import (
    _add_finite_lookback,
)
from .indicator_compiler_protocol import CompilerProtocol


class SignalPatternsMixin:
    def inside_bar_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_INSIDE_BAR_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (len(candidate) == len(expected) and _assigned_name(candidate[0]) == "_ib_hr"):
            return 0
        if not _ast_equal(
            ast.Module(body=list(candidate), type_ignores=[]),
            ast.Module(body=list(expected), type_ignores=[]),
        ):
            return 0
        frame = self.bindings.get("df")
        if not isinstance(frame, str) or self.node_types[frame] != "dataframe":
            self.unsupported(candidate[0], "inside-bar dataframe")
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
            ("ib_ready_col", "inside-bar-ready"),
            ("ib_mother_h_col", "inside-bar-mother-high"),
            ("ib_mother_l_col", "inside-bar-mother-low"),
        ):
            self.bindings[output_name] = self.emit(
                candidate[-1],
                "array-call",
                "f64-column",
                inputs=[date, high, low],
                parameters={"family": "native", "name": operation, "arguments": {}},
                lookback={
                    "kind": "function-defined",
                    "candles": None,
                    "expression": operation,
                    "causal": True,
                },
            )
        return len(expected)

    def first_fire_block(
        self: CompilerProtocol,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_FIRST_FIRE_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (
            len(candidate) == len(expected)
            and isinstance(candidate[0], ast.Assign)
            and len(candidate[0].targets) == 1
            and isinstance(candidate[0].targets[0], ast.Name)
            and candidate[0].targets[0].id == "_ph_cross"
        ):
            return 0
        if not _ast_equal(
            ast.Module(body=list(candidate), type_ignores=[]),
            ast.Module(body=list(expected), type_ignores=[]),
        ):
            return 0
        close = self.bindings.get("close_np")
        previous_max = self.bindings.get("_ph_prev_max")
        if not isinstance(close, str) or self.node_types[close] != "f64-column":
            self.unsupported(candidate[0], "first-fire close column")
        if not isinstance(previous_max, str) or self.node_types[previous_max] != "f64-column":
            self.unsupported(candidate[0], "first-fire previous maximum")
        shifted = self.emit(
            candidate[0],
            "shift",
            "f64-column",
            inputs=[close],
            parameters={"periods": 1},
            lookback=_add_finite_lookback(self.lookback(close), 1),
        )
        current_break = self.emit(
            candidate[0],
            "compare",
            "bool-column",
            inputs=[close, previous_max],
            parameters={"operator": "greater-than"},
            lookback=self.merged_lookback([close, previous_max]),
        )
        previous_below = self.emit(
            candidate[0],
            "compare",
            "bool-column",
            inputs=[shifted, previous_max],
            parameters={"operator": "less-than-or-equal"},
            lookback=self.merged_lookback([shifted, previous_max]),
        )
        crossed = self.emit(
            candidate[0],
            "logical",
            "bool-column",
            inputs=[current_break, previous_below],
            parameters={"operator": "and"},
            lookback=self.merged_lookback([current_break, previous_below]),
        )
        cross_float = self.emit(
            candidate[0],
            "cast",
            "f64-column",
            inputs=[crossed],
            parameters={"target": "float"},
            lookback=self.lookback(crossed),
        )
        self.bindings["_ph_cross"] = cross_float
        counted = self.emit(
            candidate[-1],
            "window",
            "f64-column",
            inputs=[cross_float],
            parameters={
                "kind": "rolling",
                "reducer": "sum",
                "window": 12,
                "center": False,
                "min_periods": None,
            },
            lookback=_add_finite_lookback(self.lookback(cross_float), 11),
        )
        self.bindings["ph_cross_cnt12_col"] = counted
        return len(expected)

    def column_write(
        self: CompilerProtocol, target: ast.Subscript, value_node: ast.expr, node: ast.AST
    ) -> None:
        if not isinstance(target.value, ast.Name):
            self.unsupported(target, "nested dataframe write")
        dataframe = self.bindings.get(target.value.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(target, "write target is not a dataframe")
        column = _literal_string(target.slice)
        if column is None:
            self.unsupported(target, "dynamic indicator output column")
        value = self.expression(value_node)
        written = self.emit(
            node,
            "column-write",
            "dataframe",
            inputs=[dataframe, value],
            parameters={"column": column},
            lookback=self.merged_lookback([dataframe, value]),
        )
        self.bindings[target.value.id] = written
        self.produced_columns.add(column)
