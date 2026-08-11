"""Static compiler for exact Freqtrade tag generation."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Never

from ..errors import StrategyAnalysisError
from ..signal_program.compiler import (
    _is_full_slice,
    _literal_columns,
    _literal_string,
    _numeric_record_id,
    _SignalCompiler,
)
from ..strategy_ir import analyze_strategy
from .validation import (
    OUTPUT_PHASES,
    TAG_COLUMNS,
    fingerprint_program,
    merge_lookbacks,
    validate_tag_program,
)

TAG_PROGRAM_VERSION = "tag-program-v1"
_NUMERIC_VALUE_TYPES = {
    "bool-scalar",
    "int-scalar",
    "f64-scalar",
    "bool-column",
    "f64-column",
}
_STRING_VALUE_TYPES = {"null", "string-scalar", "string-column"}


class TagProgramCompileError(StrategyAnalysisError):
    """Tag source cannot be represented exactly by tag-program-v1."""


def compile_tag_program(
    source: str | Path,
    *,
    class_name: str | None = None,
    trading_mode: str = "spot",
) -> dict[str, Any]:
    """Compile ordered signal and tag writes without executing strategy Python."""
    if trading_mode not in {"spot", "futures"}:
        raise TagProgramCompileError(f"unsupported tag trading mode: {trading_mode}")
    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    strategy = _selected_strategy(analysis)
    source_bytes = path.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if source_sha != analysis["source"]["sha256"]:
        raise TagProgramCompileError("tag source changed after static analysis")
    try:
        tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - analyzed above
        raise TagProgramCompileError("tag source no longer parses") from exc
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if class_node is None:  # pragma: no cover - analyze_strategy selected it
        raise TagProgramCompileError("selected strategy class disappeared")
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for method_name in ("populate_entry_trend", "populate_exit_trend"):
        method = methods.get(method_name)
        if method is None:
            raise TagProgramCompileError(f"strategy does not define {method_name}")
        if isinstance(method, ast.AsyncFunctionDef):
            _unsupported(method, "async tag entrypoint")

    constants = strategy.get("constants", {})
    compiler = _TagCompiler(
        path=path,
        methods=methods,
        class_constants=constants if isinstance(constants, Mapping) else {},
    )
    compiler.method_ids.update({"populate_entry_trend": "f1", "populate_exit_trend": "f2"})
    try:
        compiler.current_phase = "entry"
        entry_id = compiler.compile_method("populate_entry_trend", kind="entrypoint-entry")
        compiler.current_phase = "exit"
        exit_id = compiler.compile_method("populate_exit_trend", kind="entrypoint-exit")
    except TagProgramCompileError:
        raise
    except StrategyAnalysisError as exc:
        raise TagProgramCompileError(str(exc)) from exc

    program: dict[str, Any] = {
        "schema_version": TAG_PROGRAM_VERSION,
        "source": {"path": str(path), "sha256": source_sha},
        "selected_class": strategy["name"],
        "compile_context": {"run_mode": "backtest", "trading_mode": trading_mode},
        "entrypoints": [
            {"phase": "entry", "function": entry_id},
            {"phase": "exit", "function": exit_id},
        ],
        "functions": sorted(compiler.functions, key=_numeric_record_id),
        "nodes": compiler.nodes,
        "tag_outputs": [
            {
                "column": column,
                "phase": OUTPUT_PHASES[column],
                "wrapper_initializer": "",
                "final_mutation": compiler.final_mutations.get(column),
            }
            for column in TAG_COLUMNS
        ],
        "route_contract": {
            "canonicalization": "python-str-split",
            "original_storage": "preserve-exact",
            "trailing_whitespace": "preserve",
        },
        "required_input_columns": sorted(compiler.required_input_columns),
        "mutation_nodes": compiler.mutation_nodes,
        "tag_mutation_nodes": compiler.tag_mutation_nodes,
        "opcodes": sorted(compiler.opcodes),
        "max_lookback": merge_lookbacks(compiler.nodes),
        "source_map": compiler.source_map,
    }
    program["fingerprint"] = fingerprint_program(program)
    validate_tag_program(program)
    return program


class _TagCompiler(_SignalCompiler):
    """Compile the shared signal/tag frame while classifying tag mutations."""

    def __init__(
        self,
        *,
        path: Path,
        methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
        class_constants: Mapping[str, Any],
    ) -> None:
        super().__init__(path=path, methods=methods, class_constants=class_constants)
        self.tag_mutation_nodes: list[str] = []

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.op, ast.Add):
                self.unsupported(node, "non-additive tag augmented assignment")
            target = node.target
            if self._is_loc_target(target):
                assert isinstance(target, ast.Subscript)
                self._loc_write(target, node.value, node, append=True)
                return
            if isinstance(target, ast.Subscript):
                self.column_write(target, node.value, node, append=True)
                return
            self.unsupported(target, "tag augmented assignment target")
        super().statement(node)

    def expression(self, node: ast.expr) -> str:
        if isinstance(node, ast.JoinedStr):
            return self._format_string(node)
        return super().expression(node)

    def column_write(
        self,
        target: ast.Subscript,
        value_node: ast.expr,
        node: ast.AST,
        *,
        append: bool = False,
    ) -> None:
        if not isinstance(target.value, ast.Name):
            self.unsupported(target, "nested dataframe write")
        dataframe = self.bindings.get(target.value.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(target, "write target is not a dataframe")
        column = _literal_string(target.slice)
        if column is None:
            self.unsupported(target, "dynamic tag output column")
        self._require_output_columns(target, [column])
        value = self.expression(value_node)
        assignment = "string-append" if append else "column-values"
        self._require_assignment_values(value_node, [column], [value], assignment)
        written = self._emit_write(
            node,
            dataframe=dataframe,
            mask=None,
            values=[value],
            columns=[column],
            mode="column",
            assignment=assignment,
        )
        self.bindings[target.value.id] = written
        if column in TAG_COLUMNS:
            self.tag_mutation_nodes.append(written)

    def _loc_write(
        self,
        target: ast.Subscript,
        value_node: ast.expr,
        node: ast.AST,
        *,
        append: bool = False,
    ) -> None:
        assert isinstance(target.value, ast.Attribute)
        owner = target.value.value
        if not isinstance(owner, ast.Name):
            self.unsupported(owner, "nested tag loc write")
        dataframe = self.bindings.get(owner.id)
        if not isinstance(dataframe, str) or self.node_types[dataframe] != "dataframe":
            self.unsupported(owner, "loc write target is not a dataframe")
        if not isinstance(target.slice, ast.Tuple) or len(target.slice.elts) != 2:
            self.unsupported(target, "tag loc selector")
        rows_node, columns_node = target.slice.elts
        columns = _literal_columns(columns_node)
        if columns is None or not columns:
            self.unsupported(columns_node, "dynamic tag loc columns")
        if len(set(columns)) != len(columns):
            self.unsupported(columns_node, "duplicate tag loc columns")
        self._require_output_columns(columns_node, columns)

        mask: str | None = None
        if not _is_full_slice(rows_node):
            mask = self.expression(rows_node)
            if self.node_types[mask] not in {"bool-scalar", "bool-column", "f64-column"}:
                self.unsupported(rows_node, "non-boolean tag loc mask")

        if append and (len(columns) != 1 or columns[0] not in TAG_COLUMNS):
            self.unsupported(target, "tag append must target one tag column")
        assignment = "string-append" if append else "column-values"
        value_nodes: Sequence[ast.expr]
        if not append and len(columns) > 1 and isinstance(value_node, ast.Tuple | ast.List):
            if len(value_node.elts) != len(columns):
                self.unsupported(value_node, "tag loc value arity")
            value_nodes = value_node.elts
        elif not append and len(columns) > 1:
            assignment = "scalar-broadcast"
            value_nodes = [value_node]
        else:
            value_nodes = [value_node]
        values = [self.expression(value) for value in value_nodes]
        self._require_assignment_values(value_node, columns, values, assignment)
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
        if any(column in TAG_COLUMNS for column in columns):
            self.tag_mutation_nodes.append(written)

    def subscript(self, node: ast.Subscript) -> str:
        value = super().subscript(node)
        column = _literal_string(node.slice)
        if column in TAG_COLUMNS and self.nodes[int(value[1:]) - 1]["op"] == "column-read":
            self.nodes[int(value[1:]) - 1]["value_type"] = "string-column"
            self.node_types[value] = "string-column"
            self.required_input_columns.discard(column)
        return value

    def binary(self, node: ast.BinOp) -> str:
        if isinstance(node.op, ast.Add):
            left = self.expression(node.left)
            right = self.expression(node.right)
            value_types = {self.node_types[left], self.node_types[right]}
            if value_types & _STRING_VALUE_TYPES:
                if not value_types <= _STRING_VALUE_TYPES:
                    self.unsupported(node, "mixed string and numeric tag concatenation")
                value_type = (
                    "string-column" if "string-column" in value_types else "string-scalar"
                )
                return self.emit(
                    node,
                    "binary",
                    value_type,
                    inputs=[left, right],
                    parameters={"operator": "add"},
                    lookback=self.merged_lookback([left, right]),
                )
        return super().binary(node)

    def unsupported(self, node: ast.AST, description: str) -> Never:
        _unsupported(node, description)

    def _format_string(self, node: ast.JoinedStr) -> str:
        inputs: list[str] = []
        segments = [""]
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                segments[-1] += value.value
                continue
            if not isinstance(value, ast.FormattedValue):
                self.unsupported(value, "dynamic formatted tag component")
            if value.conversion not in {-1, ord("s")} or value.format_spec is not None:
                self.unsupported(value, "formatted tag conversion or format specification")
            input_id = self.expression(value.value)
            if self.node_types[input_id] not in {
                "bool-scalar",
                "int-scalar",
                "f64-scalar",
                "string-scalar",
            }:
                self.unsupported(value, "non-scalar formatted tag value")
            inputs.append(input_id)
            segments.append("")
        return self.emit(
            node,
            "format-string",
            "string-scalar",
            inputs=inputs,
            parameters={"segments": segments},
            lookback=self.merged_lookback(inputs),
        )

    def _require_output_columns(self, node: ast.AST, columns: Sequence[str]) -> None:
        if self.current_function not in {"f1", "f2"}:
            self.unsupported(node, "dataframe output mutation inside a helper")
        for column in columns:
            phase = OUTPUT_PHASES.get(column)
            if phase is None:
                self.unsupported(node, f"non-signal/tag dataframe output {column!r}")
            if phase != self.current_phase:
                self.unsupported(node, f"{column} mutation during the {self.current_phase} phase")

    def _require_assignment_values(
        self,
        node: ast.AST,
        columns: Sequence[str],
        values: Sequence[str],
        assignment: str,
    ) -> None:
        if assignment == "scalar-broadcast":
            wants_tag = any(column in TAG_COLUMNS for column in columns)
            wants_numeric = any(column not in TAG_COLUMNS for column in columns)
            if wants_tag and wants_numeric:
                self.unsupported(node, "mixed signal/tag scalar broadcast")
            expected = _STRING_VALUE_TYPES if wants_tag else _NUMERIC_VALUE_TYPES
            if self.node_types[values[0]] not in expected:
                self.unsupported(node, "scalar broadcast value type")
            return
        if len(columns) != len(values):
            self.unsupported(node, "tag assignment value arity")
        for column, value in zip(columns, values, strict=True):
            value_type = self.node_types[value]
            if column in TAG_COLUMNS:
                excluded = {"null"} if assignment == "string-append" else set()
                allowed = _STRING_VALUE_TYPES - excluded
                if value_type not in allowed:
                    self.unsupported(node, "non-string tag assignment value")
            elif value_type not in _NUMERIC_VALUE_TYPES:
                self.unsupported(node, "non-numeric signal assignment value")


def _selected_strategy(analysis: dict[str, Any]) -> dict[str, Any]:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise TagProgramCompileError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise TagProgramCompileError("tag program compilation requires one selected strategy")
    return analysis["strategies"][0]


def _unsupported(node: ast.AST, description: str) -> Never:
    line = getattr(node, "lineno", 1)
    column = getattr(node, "col_offset", 0)
    raise TagProgramCompileError(
        f"strategy.py:{line}:{column}: tag-program-v1 does not support {description}"
    )
