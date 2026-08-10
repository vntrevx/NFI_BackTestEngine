"""Compile a bounded causal indicator subset into indicator-program-v1."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from .errors import SpecValidationError, StrategyAnalysisError
from .specs import INDICATOR_PROGRAM_SCHEMA, validate_schema
from .strategy_ir import analyze_strategy

INDICATOR_PROGRAM_VERSION = "indicator-program-v1"

_BINARY_OPS = {
    ast.Add: "add",
    ast.Sub: "subtract",
    ast.Mult: "multiply",
    ast.Div: "divide",
    ast.FloorDiv: "floor-divide",
    ast.Mod: "modulo",
    ast.Pow: "power",
}
_LOGICAL_BINARY_OPS = {
    ast.BitAnd: "and",
    ast.BitOr: "or",
}
_COMPARE_OPS = {
    ast.Eq: "equal",
    ast.NotEq: "not-equal",
    ast.Lt: "less-than",
    ast.LtE: "less-than-or-equal",
    ast.Gt: "greater-than",
    ast.GtE: "greater-than-or-equal",
}
_UNARY_OPS = {
    ast.USub: "negate",
    ast.UAdd: "positive",
    ast.Not: "not",
    ast.Invert: "invert",
}
_SCALAR_CALLS = {"abs", "bool", "float", "int", "max", "min"}
_WINDOW_REDUCERS = {"max", "mean", "min", "std", "sum"}


class IndicatorProgramCompileError(StrategyAnalysisError):
    """Indicator source cannot be represented by the causal v1 DAG."""


@dataclass(frozen=True)
class _CallableRef:
    name: str


Binding = str | _CallableRef


def compile_indicator_program(
    source: str | Path,
    *,
    class_name: str | None = None,
) -> dict[str, Any]:
    """Compile ``populate_indicators`` and its bounded helpers without execution."""
    path = Path(source).resolve()
    analysis = analyze_strategy(path, class_name=class_name)
    strategy = _selected_strategy(analysis)
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != analysis["source"]["sha256"]:
        raise IndicatorProgramCompileError("indicator source changed after static analysis")
    try:
        tree = ast.parse(source_bytes.decode("utf-8"), filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - analyzed above
        raise IndicatorProgramCompileError("indicator source no longer parses") from exc
    class_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == strategy["name"]
        ),
        None,
    )
    if class_node is None:  # pragma: no cover - analyze_strategy selected it
        raise IndicatorProgramCompileError("selected strategy class disappeared")
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    entrypoint = methods.get("populate_indicators")
    if entrypoint is None:
        raise IndicatorProgramCompileError("strategy does not define populate_indicators")
    if isinstance(entrypoint, ast.AsyncFunctionDef):
        _unsupported(entrypoint, "async indicator entrypoint")

    constants = strategy.get("constants", {})
    compiler = _Compiler(
        path=path,
        methods=methods,
        class_constants=constants if isinstance(constants, Mapping) else {},
    )
    entrypoint_id = compiler.compile_method("populate_indicators", kind="entrypoint")
    program: dict[str, Any] = {
        "schema_version": INDICATOR_PROGRAM_VERSION,
        "source": {
            "path": str(path),
            "sha256": analysis["source"]["sha256"],
        },
        "selected_class": strategy["name"],
        "entrypoint": entrypoint_id,
        "functions": sorted(compiler.functions, key=_numeric_identifier_key),
        "nodes": compiler.nodes,
        "required_input_columns": sorted(compiler.required_input_columns),
        "produced_columns": sorted(compiler.produced_columns),
        "informative_nodes": compiler.informative_nodes,
        "opcodes": sorted(compiler.opcodes),
        "max_lookback": _program_lookback(compiler.nodes),
        "source_map": compiler.source_map,
    }
    program["fingerprint"] = _fingerprint(program)
    validate_indicator_program(program)
    return program


def validate_indicator_program(program: Any) -> None:
    """Validate schema plus DAG references, source order, and content identity."""
    validate_schema(program, INDICATOR_PROGRAM_SCHEMA)
    if not isinstance(program, Mapping):  # pragma: no cover - schema owns it
        return
    nodes = program["nodes"]
    expected_node_ids = [f"n{index}" for index in range(1, len(nodes) + 1)]
    actual_node_ids = [node["id"] for node in nodes]
    if actual_node_ids != expected_node_ids:
        raise SpecValidationError("indicator-program-v1 node IDs are not canonical")
    node_positions = {identifier: index for index, identifier in enumerate(actual_node_ids)}
    for index, node in enumerate(nodes):
        for input_id in node["inputs"]:
            position = node_positions.get(input_id)
            if position is None or position >= index:
                raise SpecValidationError(
                    f"indicator-program-v1 node {node['id']} has a non-prior input {input_id}"
                )

    functions = {function["id"]: function for function in program["functions"]}
    if program["entrypoint"] not in functions:
        raise SpecValidationError("indicator-program-v1 entrypoint is missing")
    owned_nodes: set[str] = set()
    for function in program["functions"]:
        node_ids = function["node_ids"]
        for source_order, node_id in enumerate(node_ids):
            node = nodes[node_positions[node_id]]
            if node["function"] != function["id"] or node["source_order"] != source_order:
                raise SpecValidationError(
                    f"indicator-program-v1 function {function['id']} node ownership differs"
                )
            if node_id in owned_nodes:
                raise SpecValidationError(
                    f"indicator-program-v1 node {node_id} has multiple function owners"
                )
            owned_nodes.add(node_id)
        if function["return_node"] not in node_ids:
            raise SpecValidationError(
                f"indicator-program-v1 function {function['id']} return node is external"
            )
    if owned_nodes != set(actual_node_ids):
        raise SpecValidationError("indicator-program-v1 function node ownership is incomplete")

    if set(program["source_map"]) != set(actual_node_ids):
        raise SpecValidationError("indicator-program-v1 source map does not cover every node")
    if program["opcodes"] != sorted({node["op"] for node in nodes}):
        raise SpecValidationError("indicator-program-v1 opcode inventory differs from nodes")
    informative = [node["id"] for node in nodes if node["op"] == "informative-merge"]
    if program["informative_nodes"] != informative:
        raise SpecValidationError("indicator-program-v1 informative node inventory differs")
    if program["required_input_columns"] != sorted(program["required_input_columns"]):
        raise SpecValidationError("indicator-program-v1 input columns are not canonical")
    if program["produced_columns"] != sorted(program["produced_columns"]):
        raise SpecValidationError("indicator-program-v1 output columns are not canonical")
    if program["max_lookback"] != _program_lookback(nodes):
        raise SpecValidationError("indicator-program-v1 aggregate lookback differs")

    identity = dict(program)
    fingerprint = identity.pop("fingerprint")
    if fingerprint != _fingerprint(identity):
        raise SpecValidationError("indicator-program-v1 fingerprint differs")


def _selected_strategy(analysis: dict[str, Any]) -> dict[str, Any]:
    errors = [item for item in analysis["diagnostics"] if item["severity"] == "error"]
    if errors:
        first = errors[0]
        location = first["location"]
        raise IndicatorProgramCompileError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    if len(analysis["strategies"]) != 1:
        raise IndicatorProgramCompileError(
            "indicator program compilation requires one selected strategy"
        )
    return analysis["strategies"][0]


class _Compiler:
    def __init__(
        self,
        *,
        path: Path,
        methods: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
        class_constants: Mapping[str, Any],
    ) -> None:
        self.path = path
        self.methods = methods
        self.class_constants = class_constants
        self.method_ids: dict[str, str] = {}
        self.compiling: set[str] = set()
        self.compiled: set[str] = set()
        self.function_return_types: dict[str, str] = {}
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

    def compile_method(self, name: str, *, kind: str = "helper") -> str:
        if name in self.compiled:
            return self.method_ids[name]
        node = self.methods.get(name)
        if node is None:
            raise IndicatorProgramCompileError(f"indicator helper was not found: {name}")
        if isinstance(node, ast.AsyncFunctionDef):
            self.unsupported(node, "async indicator helper")
        if name in self.compiling:
            self.unsupported(node, "recursive indicator helper")
        function_id = self.method_ids.setdefault(name, f"f{len(self.method_ids) + 1}")
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
        for statement in node.body:
            self.statement(statement)
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

        self.current_function, self.current_nodes, self.bindings, self.return_node = previous_state
        self.function_return_types[function_id] = return_type
        self.function_arities[function_id] = len(parameter_records)
        self.function_lookbacks[function_id] = return_lookback
        self.functions.append(function_record)
        self.compiling.remove(name)
        self.compiled.add(name)
        return function_id

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                callable_ref = self.callable_reference(node.value)
                self.bindings[target.id] = callable_ref or self.expression(node.value)
                return
            if isinstance(target, ast.Subscript):
                self.column_write(target, node.value, node)
                return
            self.unsupported(node, "indicator assignment target")
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            if not isinstance(node.target, ast.Name):
                self.unsupported(node, "annotated indicator assignment target")
            callable_ref = self.callable_reference(node.value)
            self.bindings[node.target.id] = callable_ref or self.expression(node.value)
            return
        if isinstance(node, ast.Return) and node.value is not None:
            value = self.expression(node.value)
            self.return_node = self.emit(
                node,
                "return",
                self.node_types[value],
                inputs=[value],
                lookback=self.lookback(value),
            )
            return
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            self.expression(node.value)
            return
        if isinstance(node, ast.If):
            condition = self.static_value(node.test)
            if not isinstance(condition, bool):
                self.unsupported(node, "dynamic indicator control-flow if")
            selected = node.body if condition else node.orelse
            for statement in selected:
                self.statement(statement)
            return
        if isinstance(node, ast.Pass):
            return
        self.unsupported(node, f"indicator statement {type(node).__name__}")

    def column_write(self, target: ast.Subscript, value_node: ast.expr, node: ast.AST) -> None:
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

    def expression(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return self.emit(
                node,
                "literal",
                _literal_type(node.value),
                parameters={"value": node.value},
            )
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, str):
                return binding
            if isinstance(binding, _CallableRef):
                self.unsupported(node, "callable used as an indicator value")
            if node.id in self.class_constants:
                return self.literal(node, self.class_constants[node.id])
            self.unsupported(node, f"unknown indicator value {node.id}")
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in self.class_constants
            ):
                return self.literal(node, self.class_constants[node.attr])
            self.unsupported(node, "attribute value")
        if isinstance(node, ast.Subscript):
            return self.subscript(node)
        if isinstance(node, ast.BinOp):
            return self.binary(node)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.BoolOp):
            return self.logical(node, type(node.op))
        if isinstance(node, ast.UnaryOp):
            operator = _UNARY_OPS.get(type(node.op))
            if operator is None:
                self.unsupported(node, "unary indicator operator")
            value = self.expression(node.operand)
            value_type = "bool-column" if operator in {"not", "invert"} else self.node_types[value]
            return self.emit(
                node,
                "unary",
                value_type,
                inputs=[value],
                parameters={"operator": operator},
                lookback=self.lookback(value),
            )
        if isinstance(node, ast.IfExp):
            return self.select(node, node.test, node.body, node.orelse)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.List | ast.Tuple | ast.Dict):
            value = _static_value(node, self.class_constants)
            if value is not None:
                return self.literal(node, value)
        self.unsupported(node, f"indicator expression {type(node).__name__}")

    def subscript(self, node: ast.Subscript) -> str:
        base = self.expression(node.value)
        base_type = self.node_types[base]
        key = _literal_string(node.slice)
        if key is None:
            self.unsupported(node, "dynamic dataframe or metadata subscript")
        if base_type == "dataframe":
            value_type = "timestamp-column" if key == "date" else "f64-column"
            if key not in self.produced_columns:
                self.required_input_columns.add(key)
            return self.emit(
                node,
                "column-read",
                value_type,
                inputs=[base],
                parameters={"column": key},
                lookback=self.lookback(base),
            )
        if base_type == "metadata":
            return self.emit(
                node,
                "metadata-read",
                "string-scalar",
                inputs=[base],
                parameters={"key": key},
                lookback=self.lookback(base),
            )
        self.unsupported(node, "subscript source type")

    def binary(self, node: ast.BinOp) -> str:
        logical = _LOGICAL_BINARY_OPS.get(type(node.op))
        if logical is not None:
            return self.logical(node, type(node.op), values=[node.left, node.right])
        operator = _BINARY_OPS.get(type(node.op))
        if operator is None:
            self.unsupported(node, "binary indicator operator")
        left = self.expression(node.left)
        right = self.expression(node.right)
        value_type = _numeric_result_type(self.node_types[left], self.node_types[right])
        return self.emit(
            node,
            "binary",
            value_type,
            inputs=[left, right],
            parameters={"operator": operator},
            lookback=self.merged_lookback([left, right]),
        )

    def compare(self, node: ast.Compare) -> str:
        left_node = node.left
        comparisons = []
        for operator_node, right_node in zip(node.ops, node.comparators, strict=True):
            operator = _COMPARE_OPS.get(type(operator_node))
            if operator is None:
                self.unsupported(node, "comparison indicator operator")
            left = self.expression(left_node)
            right = self.expression(right_node)
            value_type = _boolean_result_type(self.node_types[left], self.node_types[right])
            comparisons.append(
                self.emit(
                    node,
                    "compare",
                    value_type,
                    inputs=[left, right],
                    parameters={"operator": operator},
                    lookback=self.merged_lookback([left, right]),
                )
            )
            left_node = right_node
        if len(comparisons) == 1:
            return comparisons[0]
        return self.emit(
            node,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in comparisons)),
            inputs=comparisons,
            parameters={"operator": "and"},
            lookback=self.merged_lookback(comparisons),
        )

    def logical(
        self,
        node: ast.AST,
        operator_type: type[ast.operator] | type[ast.boolop],
        *,
        values: Sequence[ast.expr] | None = None,
    ) -> str:
        operator = {
            ast.And: "and",
            ast.Or: "or",
            ast.BitAnd: "and",
            ast.BitOr: "or",
        }.get(operator_type)
        if operator is None:
            self.unsupported(node, "logical indicator operator")
        expressions = values or (node.values if isinstance(node, ast.BoolOp) else ())
        inputs = [self.expression(value) for value in expressions]
        return self.emit(
            node,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in inputs)),
            inputs=inputs,
            parameters={"operator": operator},
            lookback=self.merged_lookback(inputs),
        )

    def select(
        self,
        node: ast.AST,
        condition_node: ast.expr,
        true_node: ast.expr,
        false_node: ast.expr,
    ) -> str:
        condition = self.expression(condition_node)
        true_value = self.expression(true_node)
        false_value = self.expression(false_node)
        value_type = _merge_value_types(
            self.node_types[true_value],
            self.node_types[false_value],
        )
        return self.emit(
            node,
            "select",
            value_type,
            inputs=[condition, true_value, false_value],
            lookback=self.merged_lookback([condition, true_value, false_value]),
        )

    def call(self, node: ast.Call) -> str:
        window = self.window_call(node)
        if window is not None:
            return window
        callable_name = self.resolved_callable(node.func)
        if callable_name == "time.perf_counter":
            return self.emit(
                node,
                "instrumentation",
                "f64-scalar",
                parameters={"name": callable_name},
            )
        if callable_name.startswith("log."):
            return self.emit(
                node,
                "instrumentation",
                "null",
                parameters={"name": callable_name},
            )
        if callable_name.startswith("self."):
            method_name = callable_name.removeprefix("self.")
            function_id = self.compile_method(method_name)
            if node.keywords or len(node.args) != self.function_arities[function_id]:
                self.unsupported(node, "indicator helper call signature")
            inputs = [self.expression(argument) for argument in node.args]
            value_type = self.function_return_types[function_id]
            return self.emit(
                node,
                "function-call",
                value_type,
                inputs=inputs,
                parameters={"function": function_id},
                lookback=_merge_lookbacks(
                    [
                        *(self.lookback(input_id) for input_id in inputs),
                        self.function_lookbacks[function_id],
                    ]
                ),
            )
        if callable_name in {"merge_informative_pair", "freqtrade.merge_informative_pair"}:
            return self.informative_merge(node)
        if callable_name == "np.where":
            if len(node.args) != 3 or node.keywords:
                self.unsupported(node, "numpy where signature")
            return self.select(node, node.args[0], node.args[1], node.args[2])
        if callable_name in {"pd.Series", "pd.DataFrame"}:
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "pandas cast signature")
            value = self.expression(node.args[0])
            return self.emit(
                node,
                "cast",
                self.node_types[value],
                inputs=[value],
                parameters={"target": callable_name.removeprefix("pd.").lower()},
                lookback=self.lookback(value),
            )
        if callable_name.startswith("ta.") or callable_name.startswith("qtpylib."):
            family, _, name = callable_name.partition(".")
            inputs = [self.expression(argument) for argument in node.args]
            parameters = _literal_keyword_arguments(node, self)
            return self.emit(
                node,
                "indicator-call",
                "f64-column",
                inputs=inputs,
                parameters={"family": family, "name": name, "arguments": parameters},
                lookback={
                    "kind": "library-defined",
                    "candles": None,
                    "expression": _safe_expression(node),
                    "causal": True,
                },
            )
        if callable_name.startswith("np."):
            inputs = [self.expression(argument) for argument in node.args]
            parameters = _literal_keyword_arguments(node, self)
            return self.emit(
                node,
                "array-call",
                _array_result_type(inputs, self.node_types),
                inputs=inputs,
                parameters={
                    "family": "numpy",
                    "name": callable_name.removeprefix("np."),
                    "arguments": parameters,
                },
                lookback=self.merged_lookback(inputs),
            )
        if callable_name in _SCALAR_CALLS:
            if node.keywords:
                self.unsupported(node, "scalar indicator keyword arguments")
            inputs = [self.expression(argument) for argument in node.args]
            return self.emit(
                node,
                "scalar-call",
                _array_result_type(inputs, self.node_types),
                inputs=inputs,
                parameters={"name": callable_name},
                lookback=self.merged_lookback(inputs),
            )
        if isinstance(node.func, ast.Attribute):
            return self.method_call(node, callable_name)
        self.unsupported(node, f"indicator call {callable_name}")

    def method_call(self, node: ast.Call, callable_name: str) -> str:
        if not isinstance(node.func, ast.Attribute):  # pragma: no cover - caller guards it
            self.unsupported(node, "indicator method call")
        method = node.func.attr
        base = self.expression(node.func.value)
        if method == "shift":
            periods = _integer_argument(node, "periods", default=1, compiler=self)
            if periods < 0:
                self.unsupported(node, "negative shift would look ahead")
            return self.emit(
                node,
                "shift",
                self.node_types[base],
                inputs=[base],
                parameters={"periods": periods},
                lookback=_add_finite_lookback(self.lookback(base), periods),
            )
        if method == "ffill":
            if node.args or node.keywords:
                self.unsupported(node, "parameterized forward fill")
            return self.emit(
                node,
                "fill",
                self.node_types[base],
                inputs=[base],
                parameters={"direction": "forward"},
                lookback=self.lookback(base),
            )
        if method in {"bfill", "backfill"}:
            self.unsupported(node, "backward fill would look ahead")
        if method == "to_numpy":
            parameters = _literal_keyword_arguments(node, self)
            return self.emit(
                node,
                "cast",
                self.node_types[base],
                inputs=[base],
                parameters={"target": "array", "arguments": parameters},
                lookback=self.lookback(base),
            )
        self.unsupported(node, f"indicator method call {callable_name}")

    def window_call(self, node: ast.Call) -> str | None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _WINDOW_REDUCERS:
            return None
        window_call = node.func.value
        if not isinstance(window_call, ast.Call) or not isinstance(window_call.func, ast.Attribute):
            return None
        kind = window_call.func.attr
        if kind not in {"rolling", "ewm", "expanding"}:
            return None
        if node.args or node.keywords:
            self.unsupported(node, "window reducer arguments")
        base = self.expression(window_call.func.value)
        parameters = _window_parameters(kind, window_call, self)
        if parameters.get("center") is True:
            self.unsupported(window_call, "centered rolling window would look ahead")
        if kind == "rolling":
            window = parameters.get("window")
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                self.unsupported(window_call, "rolling window must be a positive static integer")
            lookback = _add_finite_lookback(self.lookback(base), window - 1)
        else:
            lookback = {
                "kind": "recursive",
                "candles": None,
                "expression": _safe_expression(window_call),
                "causal": True,
            }
        return self.emit(
            node,
            "window",
            self.node_types[base],
            inputs=[base],
            parameters={"kind": kind, "reducer": node.func.attr, **parameters},
            lookback=lookback,
        )

    def informative_merge(self, node: ast.Call) -> str:
        if len(node.args) < 4:
            self.unsupported(node, "informative merge signature")
        base = self.expression(node.args[0])
        informative = self.expression(node.args[1])
        if self.node_types[base] != "dataframe" or self.node_types[informative] != "dataframe":
            self.unsupported(node, "informative merge dataframe inputs")
        base_timeframe = _static_value(node.args[2], self.class_constants)
        informative_timeframe = _static_value(node.args[3], self.class_constants)
        if not isinstance(base_timeframe, str) or not isinstance(informative_timeframe, str):
            self.unsupported(node, "dynamic informative merge timeframe")
        keyword_values = _keyword_value_map(node, self)
        if keyword_values.get("ffill", False) is not False:
            self.unsupported(node, "informative merge must not fill before source-ordered fill")
        merged = self.emit(
            node,
            "informative-merge",
            "dataframe",
            inputs=[base, informative],
            parameters={
                "base_timeframe": base_timeframe,
                "informative_timeframe": informative_timeframe,
                "ffill": False,
            },
            lookback=self.merged_lookback([base, informative]),
        )
        self.informative_nodes.append(merged)
        return merged

    def callable_reference(self, node: ast.expr) -> _CallableRef | None:
        name = _qualified_name(node)
        if name is None:
            return None
        if name.startswith(("ta.", "qtpylib.", "np.", "pd.")):
            return _CallableRef(name)
        if name.startswith("self.") and name.removeprefix("self.") in self.methods:
            return _CallableRef(name)
        return None

    def static_value(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, str):
                record = self.nodes[_numeric_identifier_key({"id": binding}) - 1]
                if record["op"] == "literal":
                    return record["parameters"].get("value")
        return _static_value(node, self.class_constants)

    def resolved_callable(self, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node.id)
            if isinstance(binding, _CallableRef):
                return binding.name
            return node.id
        name = _qualified_name(node)
        return name or "<dynamic>"

    def literal(self, node: ast.AST, value: Any) -> str:
        if not _is_json_value(value):
            self.unsupported(node, "non-JSON indicator literal")
        return self.emit(
            node,
            "literal",
            _literal_type(value),
            parameters={"value": value},
        )

    def emit(
        self,
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

    def lookback(self, node_id: str) -> dict[str, Any]:
        return dict(self.nodes[_numeric_identifier_key({"id": node_id}) - 1]["lookback"])

    def merged_lookback(self, node_ids: Sequence[str]) -> dict[str, Any]:
        return _merge_lookbacks([self.lookback(node_id) for node_id in node_ids])

    def unsupported(self, node: ast.AST, description: str) -> Never:
        _unsupported(node, description)


def _window_parameters(
    kind: str,
    node: ast.Call,
    compiler: _Compiler,
) -> dict[str, Any]:
    values = _keyword_value_map(node, compiler)
    if node.args:
        if kind == "rolling":
            values.setdefault("window", _required_static(node.args[0], compiler))
        elif kind == "ewm":
            values.setdefault("com", _required_static(node.args[0], compiler))
    allowed = {
        "rolling": {"window", "min_periods", "center", "closed"},
        "ewm": {"com", "span", "halflife", "alpha", "min_periods", "adjust", "ignore_na"},
        "expanding": {"min_periods"},
    }[kind]
    unknown = set(values) - allowed
    if unknown:
        compiler.unsupported(node, f"unsupported {kind} parameters: {sorted(unknown)}")
    if kind == "rolling":
        values.setdefault("center", False)
        values.setdefault("min_periods", None)
    if kind == "ewm":
        values.setdefault("adjust", True)
        values.setdefault("ignore_na", False)
        values.setdefault("min_periods", 0)
    if kind == "expanding":
        values.setdefault("min_periods", 1)
    return values


def _literal_keyword_arguments(
    node: ast.Call,
    compiler: _Compiler,
) -> dict[str, Any]:
    return _keyword_value_map(node, compiler)


def _keyword_value_map(
    node: ast.Call,
    compiler: _Compiler,
) -> dict[str, Any]:
    result = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded indicator keyword arguments")
        result[keyword.arg] = _required_static(keyword.value, compiler)
    return result


def _integer_argument(
    node: ast.Call,
    name: str,
    *,
    default: int,
    compiler: _Compiler,
) -> int:
    value: Any = default
    if node.args:
        value = _required_static(node.args[0], compiler)
    for keyword in node.keywords:
        if keyword.arg == name:
            value = _required_static(keyword.value, compiler)
        elif keyword.arg is not None:
            compiler.unsupported(keyword.value, f"unsupported {name} keyword {keyword.arg}")
    if not isinstance(value, int) or isinstance(value, bool):
        compiler.unsupported(node, f"{name} must be a static integer")
    return value


def _required_static(
    node: ast.expr,
    compiler: _Compiler,
) -> Any:
    value = compiler.static_value(node)
    if value is None and not (isinstance(node, ast.Constant) and node.value is None):
        compiler.unsupported(node, "dynamic indicator parameter")
    if not _is_json_value(value):
        compiler.unsupported(node, "non-JSON indicator parameter")
    return value


def _static_value(node: ast.expr, constants: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in constants
    ):
        return constants[node.attr]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _static_value(node.operand, constants)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return -value
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return None
    return value if _is_json_value(value) else None


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _parameter_type(name: str) -> str:
    lowered = name.lower()
    if lowered in {"df", "dataframe", "informative", "frame", "info"} or "dataframe" in lowered:
        return "dataframe"
    if lowered == "metadata":
        return "metadata"
    return "dynamic"


def _literal_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool-scalar"
    if isinstance(value, int):
        return "int-scalar"
    if isinstance(value, float):
        return "f64-scalar"
    if isinstance(value, str):
        return "string-scalar"
    return "json-scalar"


def _is_column(value_type: str) -> bool:
    return value_type.endswith("-column")


def _numeric_result_type(left: str, right: str) -> str:
    if _is_column(left) or _is_column(right):
        return "f64-column"
    if left == right == "int-scalar":
        return "int-scalar"
    return "f64-scalar"


def _boolean_result_type(*value_types: str) -> str:
    if any(_is_column(value_type) for value_type in value_types):
        return "bool-column"
    return "bool-scalar"


def _merge_value_types(left: str, right: str) -> str:
    if left == right:
        return left
    if _is_column(left) or _is_column(right):
        return "f64-column"
    if {left, right} <= {"int-scalar", "f64-scalar"}:
        return "f64-scalar"
    return "dynamic"


def _array_result_type(inputs: Sequence[str], node_types: Mapping[str, str]) -> str:
    types = [node_types[node] for node in inputs]
    if any(_is_column(value_type) for value_type in types):
        return "f64-column"
    return "dynamic"


def _zero_lookback() -> dict[str, Any]:
    return {
        "kind": "finite",
        "candles": 0,
        "expression": None,
        "causal": True,
    }


def _add_finite_lookback(lookback: Mapping[str, Any], candles: int) -> dict[str, Any]:
    if lookback["kind"] == "finite" and isinstance(lookback["candles"], int):
        return {
            "kind": "finite",
            "candles": lookback["candles"] + candles,
            "expression": None,
            "causal": bool(lookback["causal"]),
        }
    return {
        "kind": "mixed",
        "candles": None,
        "expression": f"{lookback['kind']}+{candles}",
        "causal": bool(lookback["causal"]),
    }


def _merge_lookbacks(lookbacks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not lookbacks:
        return _zero_lookback()
    causal = all(bool(item["causal"]) for item in lookbacks)
    if all(item["kind"] == "finite" and isinstance(item["candles"], int) for item in lookbacks):
        return {
            "kind": "finite",
            "candles": max(int(item["candles"]) for item in lookbacks),
            "expression": None,
            "causal": causal,
        }
    kinds = sorted({str(item["kind"]) for item in lookbacks})
    return {
        "kind": kinds[0] if len(kinds) == 1 else "mixed",
        "candles": None,
        "expression": "+".join(kinds),
        "causal": causal,
    }


def _program_lookback(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return _merge_lookbacks([node["lookback"] for node in nodes])


def _location(node: ast.AST) -> dict[str, Any]:
    return {
        "path": "strategy.py",
        "line": getattr(node, "lineno", 1),
        "column": getattr(node, "col_offset", 0),
        "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "end_column": getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    }


def _unsupported(node: ast.AST, description: str) -> Never:
    location = _location(node)
    raise IndicatorProgramCompileError(
        f"strategy.py:{location['line']}:{location['column']}: "
        f"indicator-program-v1 does not support {description}"
    )


def _safe_expression(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except RecursionError:
        return f"<ast-sha256:{_iterative_ast_sha256(node)}>"


def _iterative_ast_sha256(node: ast.AST) -> str:
    digest = hashlib.sha256()
    stack: list[tuple[str, Any]] = [("node", node)]
    while stack:
        kind, value = stack.pop()
        digest.update(kind.encode())
        digest.update(b"\0")
        if isinstance(value, ast.AST):
            digest.update(type(value).__name__.encode())
            for name, child in reversed(list(ast.iter_fields(value))):
                stack.append(("field", name))
                stack.append(("value", child))
        elif isinstance(value, list):
            digest.update(str(len(value)).encode())
            for child in reversed(value):
                stack.append(("value", child))
        else:
            digest.update(repr(value).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _numeric_identifier_key(record: Mapping[str, Any]) -> int:
    return int(str(record["id"])[1:])


def _fingerprint(program: Mapping[str, Any]) -> str:
    identity = copy.deepcopy(dict(program))
    source = identity.get("source")
    if isinstance(source, dict):
        source.pop("path", None)
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
