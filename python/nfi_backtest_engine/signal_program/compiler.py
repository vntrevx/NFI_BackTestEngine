"""Static compiler for source-ordered Freqtrade signal mutations."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Never

from .._indicator_ast import (
    _declared_class_constants,
    _effective_backtest_config,
    _recognized_tag_appender,
)
from .._indicator_contract import _cast_result_type
from ..errors import StrategyAnalysisError
from ..indicator_program import (
    _Compiler as _VectorExpressionCompiler,
)
from ..strategy_ir import analyze_strategy
from .validation import (
    SIGNAL_COLUMNS,
    SIGNAL_PHASES,
    SIGNAL_SIDES,
    fingerprint_program,
    merge_lookbacks,
    validate_signal_program,
)

SIGNAL_PROGRAM_VERSION = "signal-program-v1"
_NUMERIC_VALUE_TYPES = {
    "bool-scalar",
    "int-scalar",
    "f64-scalar",
    "bool-column",
    "int-column",
    "f64-column",
}


class SignalProgramCompileError(StrategyAnalysisError):
    """Signal source cannot be represented exactly by signal-program-v1."""


def compile_signal_program(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str = "spot",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile entry and exit DataFrame mutations without executing strategy Python."""
    if trading_mode not in {"spot", "futures"}:
        raise SignalProgramCompileError(f"unsupported signal trading mode: {trading_mode}")
    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    strategy = _selected_strategy(analysis)
    source_bytes = path.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if source_sha != analysis["source"]["sha256"]:
        raise SignalProgramCompileError("signal source changed after static analysis")
    try:
        tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - analyzed above
        raise SignalProgramCompileError("signal source no longer parses") from exc
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if class_node is None:  # pragma: no cover - analyze_strategy selected it
        raise SignalProgramCompileError("selected strategy class disappeared")
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    module_functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for method_name in ("populate_entry_trend", "populate_exit_trend"):
        method = methods.get(method_name)
        if method is None:
            raise SignalProgramCompileError(f"strategy does not define {method_name}")
        if isinstance(method, ast.AsyncFunctionDef):
            _unsupported(method, "async signal entrypoint")

    constants = strategy.get("constants", {})
    class_constants = _declared_class_constants(
        class_node,
        constants if isinstance(constants, Mapping) else {},
    )
    effective_config = dict(config or {})
    configured_mode = effective_config.get("trading_mode")
    if configured_mode is not None and configured_mode != trading_mode:
        raise SignalProgramCompileError(
            "signal trading mode differs from the supplied configuration"
        )
    effective_config["trading_mode"] = trading_mode
    compiler = _SignalCompiler(
        path=path,
        methods=methods,
        class_constants=class_constants,
        instance_constants={
            "config": _effective_backtest_config(effective_config),
            "dp": {"runmode": {"value": "backtest"}},
        },
        module_functions=module_functions,
    )
    # Reserve the two public IDs before helper discovery so their identity never
    # depends on how many helper functions one phase happens to call.
    compiler.method_ids.update({"populate_entry_trend": "f1", "populate_exit_trend": "f2"})
    try:
        compiler.current_phase = "entry"
        entry_id = compiler.compile_method(
            "populate_entry_trend",
            kind="entrypoint-entry",
        )
        compiler.current_phase = "exit"
        exit_id = compiler.compile_method(
            "populate_exit_trend",
            kind="entrypoint-exit",
        )
    except SignalProgramCompileError:
        raise
    except StrategyAnalysisError as exc:
        raise SignalProgramCompileError(str(exc)) from exc

    program: dict[str, Any] = {
        "schema_version": SIGNAL_PROGRAM_VERSION,
        "source": {"path": str(path), "sha256": source_sha},
        "selected_class": strategy["name"],
        "compile_context": {"run_mode": "backtest", "trading_mode": trading_mode},
        "entrypoints": [
            {"phase": "entry", "function": entry_id},
            {"phase": "exit", "function": exit_id},
        ],
        "functions": sorted(compiler.functions, key=_numeric_record_id),
        "nodes": compiler.nodes,
        "signal_outputs": [
            {
                "column": column,
                "phase": SIGNAL_PHASES[column],
                "side": SIGNAL_SIDES[column],
                "final_mutation": compiler.final_mutations[column],
            }
            for column in SIGNAL_COLUMNS
            if column in compiler.final_mutations
        ],
        "required_input_columns": sorted(compiler.required_input_columns),
        "mutation_nodes": compiler.mutation_nodes,
        "opcodes": sorted(compiler.opcodes),
        "max_lookback": merge_lookbacks(compiler.nodes),
        "source_map": compiler.source_map,
    }
    program["fingerprint"] = fingerprint_program(program)
    validate_signal_program(program)
    return program


class _SignalCompiler(_VectorExpressionCompiler):
    """Reuse the vector expression DAG while replacing whole-column writes."""

    def __init__(
        self,
        *,
        path: Path,
        methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
        class_constants: Mapping[str, Any],
        instance_constants: Mapping[str, Any] | None = None,
        module_functions: Mapping[str, ast.FunctionDef] | None = None,
    ) -> None:
        super().__init__(
            path=path,
            methods=methods,
            class_constants=class_constants,
            instance_constants=instance_constants,
            module_functions=module_functions,
        )
        self.current_phase = "entry"
        self.compile_tags = False
        self.mutation_nodes: list[str] = []
        self.final_mutations: dict[str, str] = {}

    def statement(self, node: ast.stmt) -> None:
        if (
            not self.compile_tags
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
        ):
            callable_name = self.resolved_callable(node.value.func)
            function = self.module_functions.get(callable_name)
            if function is not None and _recognized_tag_appender(function):
                return
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if self._is_loc_target(target):
                assert isinstance(target, ast.Subscript)
                if not self.compile_tags and _loc_targets_only_tags(target):
                    return
                self._loc_write(target, node.value, node)
                return
        super().statement(node)

    def column_write(self, target: ast.Subscript, value_node: ast.expr, node: ast.AST) -> None:
        if not isinstance(target.value, ast.Name):
            self.unsupported(target, "nested dataframe write")
        dataframe = self.bindings.get(target.value.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(target, "write target is not a dataframe")
        column = _literal_string(target.slice)
        if column is None:
            self.unsupported(target, "dynamic signal output column")
        self._require_signal_columns(target, [column])
        value = self.expression(value_node)
        self._require_numeric_value(value_node, value)
        written = self._emit_write(
            node,
            dataframe=dataframe,
            mask=None,
            values=[value],
            columns=[column],
            mode="column",
            assignment="column-values",
        )
        self.bindings[target.value.id] = written

    def method_call(self, node: ast.Call, callable_name: str) -> str:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "astype":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "signal astype signature")
            target = _cast_target(node.args[0])
            if target is None:
                self.unsupported(node.args[0], "dynamic signal astype target")
            base = self.expression(node.func.value)
            return self.emit(
                node,
                "cast",
                _cast_result_type(self.node_types[base], target),
                inputs=[base],
                parameters={"target": target},
                lookback=self.lookback(base),
            )
        return super().method_call(node, callable_name)

    def unsupported(self, node: ast.AST, description: str) -> Never:
        _unsupported(node, description)

    @staticmethod
    def _is_loc_target(target: ast.expr) -> bool:
        return (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "loc"
        )

    def _loc_write(self, target: ast.Subscript, value_node: ast.expr, node: ast.AST) -> None:
        assert isinstance(target.value, ast.Attribute)
        owner = target.value.value
        if not isinstance(owner, ast.Name):
            self.unsupported(owner, "nested signal loc write")
        dataframe = self.bindings.get(owner.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(owner, "loc write target is not a dataframe")
        if not isinstance(target.slice, ast.Tuple) or len(target.slice.elts) != 2:
            self.unsupported(target, "signal loc selector")
        rows_node, columns_node = target.slice.elts
        columns = _literal_columns(columns_node)
        if columns is None or not columns:
            self.unsupported(columns_node, "dynamic signal loc columns")
        if len(set(columns)) != len(columns):
            self.unsupported(columns_node, "duplicate signal loc columns")
        self._require_signal_columns(columns_node, columns)

        mask: str | None = None
        if not _is_full_slice(rows_node):
            mask = self.expression(rows_node)
            if self.node_types[mask] not in {"bool-scalar", "bool-column", "f64-column"}:
                self.unsupported(rows_node, "non-boolean signal loc mask")

        assignment = "column-values"
        value_nodes: Sequence[ast.expr]
        if len(columns) > 1 and isinstance(value_node, ast.Tuple | ast.List):
            if len(value_node.elts) != len(columns):
                self.unsupported(value_node, "signal loc value arity")
            value_nodes = value_node.elts
        elif len(columns) > 1:
            assignment = "scalar-broadcast"
            value_nodes = [value_node]
        else:
            value_nodes = [value_node]
        values = [self.expression(value) for value in value_nodes]
        for source_node, value in zip(value_nodes, values, strict=True):
            self._require_numeric_value(source_node, value)
        written = self._emit_write(
            node,
            dataframe=dataframe,
            mask=mask,
            values=values,
            columns=columns,
            mode="loc",
            assignment=assignment,
        )
        self.bindings[owner.id] = written

    def _emit_write(
        self,
        node: ast.AST,
        *,
        dataframe: str,
        mask: str | None,
        values: Sequence[str],
        columns: Sequence[str],
        mode: str,
        assignment: str,
    ) -> str:
        inputs = [dataframe]
        if mask is not None:
            inputs.append(mask)
        inputs.extend(values)
        written = self.emit(
            node,
            "frame-write",
            "dataframe",
            inputs=inputs,
            parameters={
                "rows": "all" if mask is None else "mask",
                "columns": list(columns),
                "mode": mode,
                "assignment": assignment,
            },
            lookback=self.merged_lookback(inputs),
        )
        self.mutation_nodes.append(written)
        for column in columns:
            self.produced_columns.add(column)
            self.final_mutations[column] = written
        return written

    def _require_signal_columns(self, node: ast.AST, columns: Sequence[str]) -> None:
        for column in columns:
            if column not in SIGNAL_COLUMNS:
                if column in {"enter_tag", "exit_tag"}:
                    self.unsupported(node, "tag mutation before tag-program lowering")
                self.unsupported(node, f"non-signal dataframe output {column!r}")
            if SIGNAL_PHASES[column] != self.current_phase:
                self.unsupported(
                    node,
                    f"{column} mutation during the {self.current_phase} phase",
                )

    def _require_numeric_value(self, node: ast.AST, value: str) -> None:
        if self.node_types[value] not in _NUMERIC_VALUE_TYPES:
            self.unsupported(node, "non-numeric signal assignment value")


def _selected_strategy(analysis: dict[str, Any]) -> dict[str, Any]:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise SignalProgramCompileError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise SignalProgramCompileError("signal program compilation requires one selected strategy")
    return analysis["strategies"][0]


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_columns(node: ast.expr) -> list[str] | None:
    scalar = _literal_string(node)
    if scalar is not None:
        return [scalar]
    if not isinstance(node, ast.Tuple | ast.List):
        return None
    result = [_literal_string(item) for item in node.elts]
    return None if any(item is None for item in result) else [str(item) for item in result]


def _is_full_slice(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Slice)
        and node.lower is None
        and node.upper is None
        and node.step is None
    )


def _loc_targets_only_tags(target: ast.Subscript) -> bool:
    if not isinstance(target.slice, ast.Tuple) or len(target.slice.elts) != 2:
        return False
    columns = _literal_columns(target.slice.elts[1])
    return bool(columns) and all(column in {"enter_tag", "exit_tag"} for column in columns)


def _cast_target(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name) and node.id in {"bool", "float", "int"}:
        return node.id
    if isinstance(node, ast.Constant) and node.value in {"bool", "float", "int"}:
        return str(node.value)
    return None


def _numeric_record_id(record: Mapping[str, Any]) -> int:
    return int(str(record["id"])[1:])


def _unsupported(node: ast.AST, description: str) -> Never:
    line = getattr(node, "lineno", 1)
    column = getattr(node, "col_offset", 0)
    raise SignalProgramCompileError(
        f"strategy.py:{line}:{column}: signal-program-v1 does not support {description}"
    )
