"""Compile a bounded causal indicator subset into indicator-program-v1."""

from __future__ import annotations

import ast
import hashlib
import json
import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from ._indicator_ast import (
    _AGE_FILTER_TEMPLATE,
    _FIRST_FIRE_TEMPLATE,
    _INSIDE_BAR_TEMPLATE,
    _NATIVE_HELPER_TEMPLATES,
    _OPENING_RANGE_TEMPLATE,
    _assigned_name,
    _ast_equal,
    _cast_target,
    _dataframe_column_target,
    _declared_class_constants,
    _effective_backtest_config,
    _flatten_binary_values,
    _helper_bodies_equal,
    _indicator_output_names,
    _indicator_signature,
    _is_absolute_difference_write,
    _is_array_index_nan_write,
    _is_json_value,
    _is_shift_slice_assignment,
    _literal_string,
    _loop_target_names,
    _normalized_static_value,
    _parameter_type,
    _qualified_name,
    _recognized_tag_appender,
    _safe_expression,
    _small_static_candidate,
    _template_function,
    _template_statements,
)
from ._indicator_contract import (
    IndicatorProgramCompileError,
    _add_finite_lookback,
    _array_call_result_type,
    _array_result_type,
    _boolean_result_type,
    _cast_result_type,
    _fingerprint,
    _literal_parameters,
    _literal_type,
    _location,
    _merge_lookbacks,
    _merge_value_types,
    _numeric_identifier_key,
    _numeric_result_type,
    _program_lookback,
    _unsupported,
    _zero_lookback,
)
from .errors import SpecValidationError
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
_INFORMATIVE_MERGE_PARAMETERS = (
    "dataframe",
    "informative",
    "timeframe",
    "timeframe_inf",
    "ffill",
    "append_timeframe",
    "date_column",
    "suffix",
)
_INFORMATIVE_MERGE_DEFAULTS: Mapping[str, Any] = {
    "ffill": True,
    "append_timeframe": True,
    "date_column": "date",
    "suffix": None,
}


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


def compile_indicator_program(
    source: str | Path,
    *,
    class_name: str | None = None,
    config: Mapping[str, Any] | None = None,
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
    module_functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    entrypoint = methods.get("populate_indicators")
    if entrypoint is None:
        raise IndicatorProgramCompileError("strategy does not define populate_indicators")
    if isinstance(entrypoint, ast.AsyncFunctionDef):
        _unsupported(entrypoint, "async indicator entrypoint")

    constants = strategy.get("constants", {})
    class_constants = _declared_class_constants(
        class_node,
        constants if isinstance(constants, Mapping) else {},
    )
    instance_constants: dict[str, Any] = {
        "dp": {"runmode": {"value": "backtest"}},
    }
    if config is not None:
        instance_constants["config"] = _effective_backtest_config(config)
    compiler = _Compiler(
        path=path,
        methods=methods,
        class_constants=class_constants,
        instance_constants=instance_constants,
        module_functions=module_functions,
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

    def compile_method(
        self,
        name: str,
        *,
        kind: str = "helper",
        static_arguments: Mapping[str, Any] | None = None,
        callable_arguments: Mapping[str, _CallableRef] | None = None,
    ) -> str:
        normalized_static = {
            key: _normalized_static_value(value)
            for key, value in (static_arguments or {}).items()
        }
        callable_signature = {
            key: value.name for key, value in (callable_arguments or {}).items()
        }
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

    def _next_function_id(self) -> str:
        identifiers = [*self.method_ids.values(), *self.specialized_method_ids.values()]
        next_index = max((int(identifier[1:]) for identifier in identifiers), default=0) + 1
        return f"f{next_index}"

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                callable_ref = self.callable_reference(node.value)
                lambda_ref = self.lambda_reference(node.value)
                sequence_ref = self.sequence_reference(node.value)
                provider_ref = self.data_provider_reference(node.value)
                bundle_ref = self.column_bundle_reference(node.value)
                mapping_ref = self.mapping_reference(node.value)
                static_ref = self.static_reference(node.value)
                self.bindings[target.id] = (
                    callable_ref
                    or lambda_ref
                    or sequence_ref
                    or provider_ref
                    or bundle_ref
                    or mapping_ref
                    or static_ref
                    or self.expression(node.value)
                )
                return
            if isinstance(target, ast.Subscript):
                if self.mapping_write(target, node.value):
                    return
                self.column_write(target, node.value, node)
                return
            if isinstance(target, ast.Tuple | ast.List) and isinstance(node.value, ast.Call):
                self.multi_output_assignment(target, node.value)
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
            if self.append_sequence(node.value):
                return
            self.expression(node.value)
            return
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return
        if isinstance(node, ast.If):
            if self.frame_empty_guard(node):
                return
            if self.frame_drop_guard(node):
                return
            condition = self.static_value(node.test)
            if not isinstance(condition, bool):
                self.unsupported(node, "dynamic indicator control-flow if")
            selected = node.body if condition else node.orelse
            self.statements(selected)
            return
        if isinstance(node, ast.Assert):
            found, condition = self.try_static_value(node.test)
            if not found:
                self.unsupported(node, "dynamic indicator assertion")
            if not bool(condition):
                self.unsupported(node, "failed static indicator assertion")
            return
        if isinstance(node, ast.For):
            iterations = self.static_loop_iterations(node)
            if iterations is None:
                self.unsupported(node.iter, "dynamic indicator loop iterable")
            target_names = _loop_target_names(node.target)
            previous = {name: self.bindings.get(name) for name in target_names}
            present = {name for name in target_names if name in self.bindings}
            for values in iterations:
                for name, value in zip(target_names, values, strict=True):
                    self.bindings[name] = value
                self.statements(node.body)
            if node.orelse:
                self.statements(node.orelse)
            for name in target_names:
                if name in present:
                    prior = previous[name]
                    assert prior is not None
                    self.bindings[name] = prior
                else:
                    self.bindings.pop(name, None)
            return
        if isinstance(node, ast.Pass):
            return
        self.unsupported(node, f"indicator statement {type(node).__name__}")

    def statements(self, statements: Sequence[ast.stmt]) -> None:
        index = 0
        while index < len(statements):
            consumed = self.absolute_difference_block(statements, index)
            if not consumed:
                consumed = self.age_filter_block(statements, index)
            if not consumed:
                consumed = self.opening_range_block(statements, index)
            if not consumed:
                consumed = self.inside_bar_block(statements, index)
            if not consumed:
                consumed = self.first_fire_block(statements, index)
            if consumed:
                index += consumed
                continue
            self.statement(statements[index])
            index += 1

    def absolute_difference_block(
        self,
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

    def age_filter_block(
        self,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_AGE_FILTER_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (
            len(candidate) == len(expected)
            and isinstance(candidate[0], ast.Assign)
            and len(candidate[0].targets) == 1
            and _dataframe_column_target(candidate[0].targets[0])
            == ("df", "bt_agefilter_ok")
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
        self,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_OPENING_RANGE_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (
            len(candidate) == len(expected)
            and _assigned_name(candidate[0]) == "_or_day"
        ):
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

    def inside_bar_block(
        self,
        statements: Sequence[ast.stmt],
        index: int,
    ) -> int:
        expected = _template_statements(_INSIDE_BAR_TEMPLATE)
        candidate = statements[index : index + len(expected)]
        if not (
            len(candidate) == len(expected)
            and _assigned_name(candidate[0]) == "_ib_hr"
        ):
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
        self,
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
            if isinstance(binding, _LambdaRef):
                self.unsupported(node, "lambda used as an indicator value")
            if isinstance(binding, _SequenceBinding):
                self.unsupported(node, "sequence used as an indicator value")
            if isinstance(binding, _DataProviderRef):
                self.unsupported(node, "data provider used as an indicator value")
            if isinstance(binding, _ColumnBundleBinding):
                self.unsupported(node, "column bundle used as an indicator value")
            if isinstance(binding, _MappingBinding):
                self.unsupported(node, "mapping used as an indicator value")
            if isinstance(binding, _StaticBinding):
                return self.literal(node, binding.value)
            if node.id in self.class_constants:
                return self.literal(node, self.class_constants[node.id])
            self.unsupported(node, f"unknown indicator value {node.id}")
        if isinstance(node, ast.Attribute):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            self.unsupported(node, "attribute value")
        if isinstance(node, ast.Subscript):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
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
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            return self.select(node, node.test, node.body, node.orelse)
        if isinstance(node, ast.Call):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
            return self.call(node)
        if isinstance(node, ast.JoinedStr | ast.List | ast.Tuple | ast.Set | ast.Dict):
            found, value = self.try_static_value(node)
            if found:
                return self.literal(node, value)
        self.unsupported(node, f"indicator expression {type(node).__name__}")

    def subscript(self, node: ast.Subscript) -> str:
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and not isinstance(node.slice.value, bool)
        ):
            callable_name = self.resolved_callable(node.value.func)
            if _indicator_output_names(callable_name) is not None:
                return self.indicator_output(node.value, node.slice.value, node)
            indexed_string = self.indexed_string_call(
                node.value,
                index=node.slice.value,
                source_node=node,
            )
            if indexed_string is not None:
                return indexed_string
        base = self.expression(node.value)
        base_type = self.node_types[base]
        found_key, key = self.try_static_value(node.slice)
        if not found_key or not isinstance(key, str):
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

    def indexed_string_call(
        self,
        call: ast.Call,
        *,
        index: int,
        source_node: ast.AST,
    ) -> str | None:
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
            "partition",
            "split",
            "rsplit",
        }:
            return None
        if len(call.args) != 1 or call.keywords:
            self.unsupported(call, "indexed string call signature")
        found, separator = self.try_static_value(call.args[0])
        if not found or not isinstance(separator, str) or not separator:
            self.unsupported(call.args[0], "indexed string separator")
        base = self.expression(call.func.value)
        if self.node_types[base] != "string-scalar":
            self.unsupported(call.func.value, "indexed string source type")
        return self.emit(
            source_node,
            "string-split-index",
            "string-scalar",
            inputs=[base],
            parameters={
                "method": call.func.attr,
                "separator": separator,
                "index": index,
            },
            lookback=self.lookback(base),
        )

    def binary(self, node: ast.BinOp) -> str:
        logical = _LOGICAL_BINARY_OPS.get(type(node.op))
        if logical is not None:
            return self.logical(
                node,
                type(node.op),
                values=_flatten_binary_values(node, type(node.op)),
            )
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
            if isinstance(operator_node, ast.In | ast.NotIn):
                found, collection = self.try_static_value(right_node)
                if not found or not isinstance(collection, list | tuple | Mapping):
                    self.unsupported(right_node, "dynamic membership collection")
                left = self.expression(left_node)
                values = list(collection)
                if not all(_is_json_value(value) for value in values):
                    self.unsupported(right_node, "non-JSON membership collection")
                comparisons.append(
                    self.emit(
                        node,
                        "membership",
                        _boolean_result_type(self.node_types[left]),
                        inputs=[left],
                        parameters={
                            "values": [_normalized_static_value(value) for value in values],
                            "negated": isinstance(operator_node, ast.NotIn),
                        },
                        lookback=self.lookback(left),
                    )
                )
                left_node = right_node
                continue
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
        lambda_value = self._inline_lambda_call(node)
        if lambda_value is not None:
            return lambda_value
        callable_name = self.resolved_callable(node.func)
        frame_source = self.frame_source_call(node)
        if frame_source is not None:
            return frame_source
        sequence_value = self.reduce_sequence_call(node, callable_name)
        if sequence_value is not None:
            return sequence_value
        tag_value = self.append_tag_call(node, callable_name)
        if tag_value is not None:
            return tag_value
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
        concatenated = self.concat_column_bundle(node, callable_name)
        if concatenated is not None:
            return concatenated
        if callable_name == "len":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "len signature")
            value = self.expression(node.args[0])
            if self.node_types[value] != "dataframe":
                self.unsupported(node.args[0], "len source type")
            return self.emit(
                node,
                "row-count",
                "int-scalar",
                inputs=[value],
                lookback=self.lookback(value),
            )
        if callable_name.startswith("self."):
            method_name = callable_name.removeprefix("self.")
            method = self.methods.get(method_name)
            if method is None or isinstance(method, ast.AsyncFunctionDef):
                self.unsupported(node, "indicator helper call target")
            bound = _bind_helper_arguments(node, method, self)
            shift = _normalized_shift_helper(method, bound, self)
            if shift is not None:
                source_node, periods = shift
                source = self.expression(source_node)
                if not self.node_types[source].endswith("-column"):
                    self.unsupported(source_node, "shift helper source type")
                return self.emit(
                    node,
                    "shift",
                    self.node_types[source],
                    inputs=[source],
                    parameters={"periods": periods},
                    lookback=_add_finite_lookback(self.lookback(source), periods),
                )
            projection = _normalized_frame_projection_helper(method, bound, self)
            if projection is not None:
                source_node, parameters = projection
                source = self.expression(source_node)
                if self.node_types[source] != "dataframe":
                    self.unsupported(source_node, "frame projection source type")
                return self.emit(
                    node,
                    "frame-project",
                    "dataframe",
                    inputs=[source],
                    parameters=parameters,
                    lookback=self.lookback(source),
                )
            native_indicator = _normalized_native_indicator_helper(method, bound, self)
            if native_indicator is not None:
                name, argument_nodes, parameters = native_indicator
                inputs = [self.expression(argument) for argument in argument_nodes]
                if any(not self.node_types[value].endswith("-column") for value in inputs):
                    self.unsupported(node, "native indicator input type")
                return self.emit(
                    node,
                    "indicator-call",
                    "f64-column",
                    inputs=inputs,
                    parameters={
                        "family": "native",
                        "name": name,
                        "arguments": parameters,
                    },
                    lookback={
                        "kind": "function-defined",
                        "candles": parameters.get("timeperiod", 1) - 1,
                        "expression": name,
                        "causal": True,
                    },
                )
            static_arguments: dict[str, Any] = {}
            callable_arguments: dict[str, _CallableRef] = {}
            dynamic_arguments: list[ast.expr] = []
            for name, argument in bound:
                if isinstance(argument, ast.Name):
                    argument_binding = self.bindings.get(argument.id)
                    if isinstance(argument_binding, _CallableRef):
                        callable_arguments[name] = argument_binding
                        continue
                found, value = self.try_static_value(argument)
                if found:
                    static_arguments[name] = value
                else:
                    dynamic_arguments.append(argument)
            function_id = self.compile_method(
                method_name,
                static_arguments=static_arguments,
                callable_arguments=callable_arguments,
            )
            if len(dynamic_arguments) != self.function_arities[function_id]:
                self.unsupported(node, "indicator helper call signature")
            inputs = [self.expression(argument) for argument in dynamic_arguments]
            value_type = self.function_return_types[function_id]
            if not inputs and function_id in self.function_static_returns:
                return self.literal(node, self.function_static_returns[function_id])
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
            return self.indicator_call(node)
        if callable_name.startswith("np."):
            return self.array_call(node, callable_name)
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

    def multi_output_assignment(self, target: ast.Tuple | ast.List, node: ast.Call) -> None:
        callable_name = self.resolved_callable(node.func)
        inlined = self.inline_tuple_helper_call(node, callable_name)
        if inlined is not None:
            if len(inlined) != len(target.elts):
                self.unsupported(node, "indicator helper tuple output arity")
            for element, value in zip(target.elts, inlined, strict=True):
                if not isinstance(element, ast.Name):
                    self.unsupported(element, "indicator helper tuple assignment target")
                self.bindings[element.id] = value
            return
        output_names = _indicator_output_names(callable_name)
        if output_names is None or len(output_names) != len(target.elts):
            self.unsupported(node, "indicator tuple output contract")
        inputs, parameters = self.indicator_call_parts(node, callable_name)
        for element, output_name in zip(target.elts, output_names, strict=True):
            if not isinstance(element, ast.Name):
                self.unsupported(element, "indicator tuple assignment target")
            self.bindings[element.id] = self.emit_indicator_call(
                node,
                callable_name,
                inputs,
                parameters,
                output=output_name,
            )

    def inline_tuple_helper_call(
        self,
        call: ast.Call,
        callable_name: str,
    ) -> list[str] | None:
        if not callable_name.startswith("self."):
            return None
        method = self.methods.get(callable_name.removeprefix("self."))
        if method is None or isinstance(method, ast.AsyncFunctionDef):
            return None
        body = method.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if not (
            body
            and isinstance(body[-1], ast.Return)
            and isinstance(body[-1].value, ast.Tuple | ast.List)
            and all(isinstance(statement, ast.Assign | ast.AnnAssign) for statement in body[:-1])
        ):
            return None
        bound = _bind_helper_arguments(call, method, self)
        argument_bindings: dict[str, Binding] = {}
        for name, argument in bound:
            if isinstance(argument, ast.Name):
                existing = self.bindings.get(argument.id)
                if isinstance(existing, _CallableRef):
                    argument_bindings[name] = existing
                    continue
            found, value = self.try_static_value(argument)
            argument_bindings[name] = (
                _StaticBinding(value) if found else self.expression(argument)
            )
        previous = self.bindings
        self.bindings = argument_bindings
        try:
            self.statements(body[:-1])
            returned = body[-1]
            assert isinstance(returned, ast.Return)
            assert isinstance(returned.value, ast.Tuple | ast.List)
            return [self.expression(value) for value in returned.value.elts]
        finally:
            self.bindings = previous

    def array_call(self, node: ast.Call, callable_name: str) -> str:
        name = callable_name.removeprefix("np.")
        if name == "full_like":
            if len(node.args) != 2 or node.keywords:
                self.unsupported(node, "numpy full_like signature")
            inputs = [self.expression(argument) for argument in node.args]
            value_type = self.node_types[inputs[0]]
            if not value_type.endswith("-column"):
                self.unsupported(node.args[0], "numpy full_like template type")
            return self.emit(
                node,
                "array-call",
                value_type,
                inputs=inputs,
                parameters={"family": "numpy", "name": name, "arguments": {}},
                lookback=self.merged_lookback(inputs),
            )
        if name == "divide":
            if len(node.args) != 2:
                self.unsupported(node, "numpy divide signature")
            options: dict[str, ast.expr] = {}
            for keyword in node.keywords:
                if keyword.arg not in {"out", "where"}:
                    self.unsupported(keyword.value, "numpy divide keyword arguments")
                if keyword.arg in options:
                    self.unsupported(keyword.value, f"duplicate numpy divide {keyword.arg}")
                options[str(keyword.arg)] = keyword.value
            if set(options) != {"out", "where"}:
                self.unsupported(node, "numpy divide requires explicit out and where")
            inputs = [
                self.expression(node.args[0]),
                self.expression(node.args[1]),
                self.expression(options["out"]),
                self.expression(options["where"]),
            ]
            if any(self.node_types[item] != "f64-column" for item in inputs[:3]):
                self.unsupported(node, "numpy divide numeric column types")
            if self.node_types[inputs[3]] != "bool-column":
                self.unsupported(options["where"], "numpy divide where mask type")
            return self.emit(
                node,
                "array-call",
                "f64-column",
                inputs=inputs,
                parameters={"family": "numpy", "name": name, "arguments": {}},
                lookback=self.merged_lookback(inputs),
            )
        inputs = [self.expression(argument) for argument in node.args]
        parameters = _literal_keyword_arguments(node, self)
        return self.emit(
            node,
            "array-call",
            _array_call_result_type(callable_name, inputs, self.node_types),
            inputs=inputs,
            parameters={"family": "numpy", "name": name, "arguments": parameters},
            lookback=self.merged_lookback(inputs),
        )

    def indicator_output(self, call: ast.Call, index: int, source_node: ast.AST) -> str:
        callable_name = self.resolved_callable(call.func)
        output_names = _indicator_output_names(callable_name)
        if output_names is None or index < 0 or index >= len(output_names):
            self.unsupported(source_node, "indicator output index")
        inputs, parameters = self.indicator_call_parts(call, callable_name)
        return self.emit_indicator_call(
            source_node,
            callable_name,
            inputs,
            parameters,
            output=output_names[index],
        )

    def indicator_call(self, node: ast.Call) -> str:
        callable_name = self.resolved_callable(node.func)
        output_names = _indicator_output_names(callable_name)
        if output_names is not None and len(output_names) != 1:
            self.unsupported(node, "multi-output indicator requires tuple assignment or subscript")
        inputs, parameters = self.indicator_call_parts(node, callable_name)
        return self.emit_indicator_call(node, callable_name, inputs, parameters)

    def indicator_call_parts(
        self,
        node: ast.Call,
        callable_name: str,
    ) -> tuple[list[str], dict[str, Any]]:
        if not callable_name.startswith(("ta.", "qtpylib.")):
            self.unsupported(node, "indexed value is not an indicator call")
        signature = _indicator_signature(callable_name)
        if signature is None:
            return (
                [self.expression(argument) for argument in node.args],
                _literal_keyword_arguments(node, self),
            )
        input_count, parameter_names = signature
        if len(node.args) < input_count or len(node.args) > input_count + len(parameter_names):
            self.unsupported(node, "indicator positional signature")
        inputs = [self.expression(argument) for argument in node.args[:input_count]]
        arguments = _literal_keyword_arguments(node, self)
        if any(name not in parameter_names for name in arguments):
            self.unsupported(node, "unknown indicator keyword argument")
        for name, argument in zip(parameter_names, node.args[input_count:], strict=False):
            if name in arguments:
                self.unsupported(argument, "duplicate indicator argument")
            arguments[name] = _required_static(argument, self)
        return inputs, arguments

    def emit_indicator_call(
        self,
        source_node: ast.AST,
        callable_name: str,
        inputs: Sequence[str],
        arguments: Mapping[str, Any],
        *,
        output: str | None = None,
    ) -> str:
        family, _, name = callable_name.partition(".")
        parameters: dict[str, Any] = {
            "family": family,
            "name": name,
            "arguments": dict(arguments),
        }
        if output is not None:
            parameters["output"] = output
        return self.emit(
            source_node,
            "indicator-call",
            "f64-column",
            inputs=inputs,
            parameters=parameters,
            lookback={
                "kind": "library-defined",
                "candles": None,
                "expression": _safe_expression(source_node),
                "causal": True,
            },
        )

    def method_call(self, node: ast.Call, callable_name: str) -> str:
        if not isinstance(node.func, ast.Attribute):  # pragma: no cover - caller guards it
            self.unsupported(node, "indicator method call")
        method = node.func.attr
        base = self.expression(node.func.value)
        if method == "astype":
            if len(node.args) != 1 or node.keywords:
                self.unsupported(node, "astype signature")
            target = _cast_target(node.args[0])
            if target is None:
                self.unsupported(node.args[0], "dynamic astype target")
            return self.emit(
                node,
                "cast",
                _cast_result_type(self.node_types[base], target),
                inputs=[base],
                parameters={"target": target},
                lookback=self.lookback(base),
            )
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
                lookback={
                    "kind": "recursive",
                    "candles": None,
                    "expression": _safe_expression(node),
                    "causal": bool(self.lookback(base)["causal"]),
                },
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
        arguments = _bind_informative_merge_arguments(node, self)
        base = self.expression(arguments["dataframe"])
        informative = self.expression(arguments["informative"])
        if self.node_types[base] != "dataframe" or self.node_types[informative] != "dataframe":
            self.unsupported(node, "informative merge dataframe inputs")

        timeframe_node = arguments["timeframe"]
        informative_timeframe_node = arguments["timeframe_inf"]
        base_timeframe = _required_static(timeframe_node, self)
        informative_timeframe = _required_static(informative_timeframe_node, self)
        if not isinstance(base_timeframe, str) or not isinstance(informative_timeframe, str):
            self.unsupported(node, "dynamic informative merge timeframe")

        ffill = _static_informative_option(arguments, "ffill", self)
        append_timeframe = _static_informative_option(arguments, "append_timeframe", self)
        date_column = _static_informative_option(arguments, "date_column", self)
        suffix = _static_informative_option(arguments, "suffix", self)
        if not isinstance(ffill, bool):
            self.unsupported(arguments.get("ffill", node), "non-boolean informative merge ffill")
        if not isinstance(append_timeframe, bool):
            self.unsupported(
                arguments.get("append_timeframe", node),
                "non-boolean informative merge append_timeframe",
            )
        if not isinstance(date_column, str):
            self.unsupported(
                arguments.get("date_column", node),
                "non-string informative merge date_column",
            )
        if suffix is not None and not isinstance(suffix, str):
            self.unsupported(arguments.get("suffix", node), "non-string informative merge suffix")
        if suffix and append_timeframe:
            self.unsupported(
                arguments.get("suffix", node),
                "informative merge suffix conflicts with append_timeframe",
            )
        normalized_suffix = suffix or None
        if not append_timeframe and normalized_suffix is None:
            self.unsupported(
                arguments.get("append_timeframe", node),
                "informative merge without an output suffix",
            )

        base_minutes = _freqtrade_timeframe_minutes(timeframe_node, base_timeframe, self)
        informative_minutes = _freqtrade_timeframe_minutes(
            informative_timeframe_node,
            informative_timeframe,
            self,
        )
        if base_minutes > informative_minutes:
            self.unsupported(
                informative_timeframe_node,
                "faster informative timeframe would create rows",
            )
        lookback = self.merged_lookback([base, informative])
        if ffill:
            lookback = {
                "kind": "recursive",
                "candles": None,
                "expression": _safe_expression(node),
                "causal": bool(lookback["causal"]),
            }
        merged = self.emit(
            node,
            "informative-merge",
            "dataframe",
            inputs=[base, informative],
            parameters={
                "base_timeframe": base_timeframe,
                "informative_timeframe": informative_timeframe,
                "ffill": ffill,
                "append_timeframe": append_timeframe,
                "date_column": date_column,
                "suffix": normalized_suffix,
            },
            lookback=lookback,
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

    @staticmethod
    def lambda_reference(node: ast.expr) -> _LambdaRef | None:
        if not isinstance(node, ast.Lambda):
            return None
        if (
            node.args.posonlyargs
            or node.args.kwonlyargs
            or node.args.vararg is not None
            or node.args.kwarg is not None
            or node.args.defaults
        ):
            return None
        return _LambdaRef(tuple(argument.arg for argument in node.args.args), node.body)

    def _inline_lambda_call(self, node: ast.Call) -> str | None:
        if not isinstance(node.func, ast.Name):
            return None
        binding = self.bindings.get(node.func.id)
        if not isinstance(binding, _LambdaRef):
            return None
        if node.keywords or len(node.args) != len(binding.parameters):
            self.unsupported(node, "lambda call signature")
        previous = {name: self.bindings.get(name) for name in binding.parameters}
        present = {name for name in binding.parameters if name in self.bindings}
        for name, argument in zip(binding.parameters, node.args, strict=True):
            found, value = self.try_static_value(argument)
            self.bindings[name] = (
                _StaticBinding(value) if found else self.expression(argument)
            )
        try:
            return self.expression(binding.body)
        finally:
            for name in binding.parameters:
                if name in present:
                    prior = previous[name]
                    assert prior is not None
                    self.bindings[name] = prior
                else:
                    self.bindings.pop(name, None)

    def static_reference(self, node: ast.expr) -> _StaticBinding | None:
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

    def mapping_write(self, target: ast.Subscript, value_node: ast.expr) -> bool:
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

    def static_loop_iterations(self, node: ast.For) -> list[tuple[Binding, ...]] | None:
        target_names = _loop_target_names(node.target)
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
                return [
                    (_StaticBinding(key), value)
                    for key, value in mapping.items.items()
                ]
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

    def column_bundle_reference(self, node: ast.expr) -> _ColumnBundleBinding | None:
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

    def concat_column_bundle(self, node: ast.Call, callable_name: str) -> str | None:
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
        if set(options) != {"axis", "copy"} or options["axis"] != (True, 1) or options[
            "copy"
        ] != (True, False):
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

    def frame_source_call(self, call: ast.Call) -> str | None:
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

    def frame_empty_guard(self, node: ast.If) -> bool:
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

    def frame_drop_guard(self, node: ast.If) -> bool:
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
            and isinstance(node.body[0], ast.Assign)
            and len(node.body[0].targets) == 1
            and isinstance(node.body[0].targets[0], ast.Name)
            and isinstance(node.body[0].value, ast.Call)
            and isinstance(node.body[0].value.func, ast.Attribute)
            and node.body[0].value.func.attr == "drop"
            and isinstance(node.body[0].value.func.value, ast.Name)
        ):
            return False
        dataframe_name = node.test.comparators[0].value.id
        assignment = node.body[0]
        assert isinstance(assignment, ast.Assign)
        assert isinstance(assignment.targets[0], ast.Name)
        assert isinstance(assignment.value, ast.Call)
        assert isinstance(assignment.value.func, ast.Attribute)
        assert isinstance(assignment.value.func.value, ast.Name)
        if (
            assignment.targets[0].id != dataframe_name
            or assignment.value.func.value.id != dataframe_name
            or assignment.value.args
            or len(assignment.value.keywords) != 1
            or assignment.value.keywords[0].arg != "columns"
        ):
            return False
        found_test, test_column = self.try_static_value(node.test.left)
        found_drop, drop_column = self.try_static_value(assignment.value.keywords[0].value)
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

    def append_sequence(self, call: ast.Call) -> bool:
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

    def reduce_sequence_call(self, call: ast.Call, callable_name: str) -> str | None:
        function = self.module_functions.get(callable_name)
        operator_name = _recognized_sequence_reducer(function) if function is not None else None
        if operator_name is None:
            return None
        if len(call.args) != 1 or call.keywords or not isinstance(call.args[0], ast.Name):
            self.unsupported(call, "sequence reducer signature")
        binding = self.bindings.get(call.args[0].id)
        if not isinstance(binding, _SequenceBinding):
            self.unsupported(call.args[0], "sequence reducer input")
        dynamic: list[str] = []
        static_values: list[bool] = []
        for item in binding.items:
            if isinstance(item, _StaticBinding):
                if not isinstance(item.value, bool):
                    self.unsupported(call.args[0], "non-Boolean sequence reducer value")
                static_values.append(item.value)
            elif isinstance(item, str):
                if self.node_types[item] not in {"bool-scalar", "bool-column"}:
                    self.unsupported(call.args[0], "non-Boolean sequence reducer value")
                dynamic.append(item)
            else:
                self.unsupported(call.args[0], "nested sequence reducer value")
        absorbing = operator_name != "and"
        if absorbing in static_values:
            return self.literal(call, absorbing)
        if not dynamic:
            return self.literal(call, not absorbing)
        if len(dynamic) == 1:
            return dynamic[0]
        return self.emit(
            call,
            "logical",
            _boolean_result_type(*(self.node_types[item] for item in dynamic)),
            inputs=dynamic,
            parameters={"operator": operator_name},
            lookback=self.merged_lookback(dynamic),
        )

    def append_tag_call(self, call: ast.Call, callable_name: str) -> str | None:
        function = self.module_functions.get(callable_name)
        if function is None or not _recognized_tag_appender(function):
            return None
        if len(call.args) != 3 or call.keywords or not isinstance(call.args[0], ast.Name):
            self.unsupported(call, "tag append helper signature")
        target_name = call.args[0].id
        target = self.bindings.get(target_name)
        if not isinstance(target, str) or self.node_types[target] != "string-column":
            self.unsupported(call.args[0], "tag append target")
        mask = self.expression(call.args[1])
        tag = self.expression(call.args[2])
        if self.node_types[mask] not in {"bool-scalar", "bool-column"}:
            self.unsupported(call.args[1], "tag append mask")
        if self.node_types[tag] != "string-scalar":
            self.unsupported(call.args[2], "tag append value")
        result = self.emit(
            call,
            "masked-string-append",
            "string-column",
            inputs=[target, mask, tag],
            lookback=self.merged_lookback([target, mask, tag]),
        )
        self.bindings[target_name] = result
        return result

    def try_static_value(self, node: ast.expr) -> tuple[bool, Any]:
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
                    return True, {
                        key: value.value for key, value in binding.items.items()
                    }
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
                sequence_items
                if isinstance(node, ast.List | ast.Set)
                else tuple(sequence_items),
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

    def _try_static_call(self, node: ast.Call) -> tuple[bool, Any]:
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

    def static_value(self, node: ast.expr) -> Any:
        found, value = self.try_static_value(node)
        return value if found else None

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
            parameters=_literal_parameters(value),
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

    def node_static_value(self, node_id: str) -> tuple[bool, Any]:
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


def _bind_informative_merge_arguments(
    node: ast.Call,
    compiler: _Compiler,
) -> dict[str, ast.expr]:
    if len(node.args) > len(_INFORMATIVE_MERGE_PARAMETERS):
        compiler.unsupported(node, "informative merge signature")
    arguments: dict[str, ast.expr] = dict(
        zip(_INFORMATIVE_MERGE_PARAMETERS, node.args, strict=False)
    )
    for keyword in node.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded informative merge keyword arguments")
        if keyword.arg not in _INFORMATIVE_MERGE_PARAMETERS:
            compiler.unsupported(
                keyword.value,
                f"unknown informative merge keyword {keyword.arg}",
            )
        if keyword.arg in arguments:
            compiler.unsupported(
                keyword.value,
                f"duplicate informative merge argument {keyword.arg}",
            )
        arguments[keyword.arg] = keyword.value
    missing = [name for name in _INFORMATIVE_MERGE_PARAMETERS[:4] if name not in arguments]
    if missing:
        compiler.unsupported(node, f"informative merge missing arguments: {missing}")
    return arguments


def _bind_helper_arguments(
    call: ast.Call,
    method: ast.FunctionDef,
    compiler: _Compiler,
) -> list[tuple[str, ast.expr]]:
    if method.args.vararg is not None or method.args.kwarg is not None:
        compiler.unsupported(call, "variadic indicator helper signature")
    positional = [*method.args.posonlyargs, *method.args.args]
    if positional and positional[0].arg == "self":
        positional = positional[1:]
    names = [argument.arg for argument in positional]
    keyword_only = [argument.arg for argument in method.args.kwonlyargs]
    if len(call.args) > len(positional):
        compiler.unsupported(call, "indicator helper call signature")
    bound: dict[str, ast.expr] = {
        name: value for name, value in zip(names, call.args, strict=False)
    }
    for keyword in call.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded indicator helper arguments")
        if keyword.arg not in {*names, *keyword_only}:
            compiler.unsupported(keyword.value, f"unknown indicator helper argument {keyword.arg}")
        if keyword.arg in bound:
            compiler.unsupported(
                keyword.value,
                f"duplicate indicator helper argument {keyword.arg}",
            )
        bound[keyword.arg] = keyword.value

    positional_defaults = [None] * (len(positional) - len(method.args.defaults)) + list(
        method.args.defaults
    )
    for argument, default in zip(positional, positional_defaults, strict=True):
        if argument.arg not in bound:
            if default is None:
                compiler.unsupported(call, "indicator helper call signature")
            bound[argument.arg] = default
    for argument, default in zip(method.args.kwonlyargs, method.args.kw_defaults, strict=True):
        if argument.arg not in bound:
            if default is None:
                compiler.unsupported(call, "indicator helper call signature")
            bound[argument.arg] = default
    return [(name, bound[name]) for name in [*names, *keyword_only]]


def _bind_pair_dataframe_arguments(
    call: ast.Call,
    compiler: _Compiler,
) -> dict[str, ast.expr]:
    names = ("pair", "timeframe")
    if len(call.args) > len(names):
        compiler.unsupported(call, "get_pair_dataframe signature")
    bound: dict[str, ast.expr] = dict(zip(names, call.args, strict=False))
    for keyword in call.keywords:
        if keyword.arg is None:
            compiler.unsupported(keyword.value, "expanded get_pair_dataframe arguments")
        if keyword.arg not in names:
            compiler.unsupported(
                keyword.value,
                f"unknown get_pair_dataframe argument {keyword.arg}",
            )
        if keyword.arg in bound:
            compiler.unsupported(
                keyword.value,
                f"duplicate get_pair_dataframe argument {keyword.arg}",
            )
        bound[str(keyword.arg)] = keyword.value
    if set(bound) != set(names):
        compiler.unsupported(call, "get_pair_dataframe requires pair and timeframe")
    return bound


def _normalized_shift_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: _Compiler,
) -> tuple[ast.expr, int] | None:
    """Recognize the canonical allocate-and-slice causal shift implementation."""
    parameters = [*method.args.posonlyargs, *method.args.args]
    if parameters and parameters[0].arg == "self":
        parameters = parameters[1:]
    if len(parameters) != 2 or len(method.body) != 4:
        return None
    source_name, periods_name = (argument.arg for argument in parameters)
    allocate, warmup, shifted, returned = method.body
    if not (
        isinstance(allocate, ast.Assign)
        and len(allocate.targets) == 1
        and isinstance(allocate.targets[0], ast.Name)
        and isinstance(allocate.value, ast.Call)
        and _qualified_name(allocate.value.func) == "np.empty_like"
        and len(allocate.value.args) == 1
        and isinstance(allocate.value.args[0], ast.Name)
        and allocate.value.args[0].id == source_name
        and not allocate.value.keywords
    ):
        return None
    output_name = allocate.targets[0].id
    if not _is_shift_slice_assignment(
        warmup,
        output_name=output_name,
        source_name=source_name,
        periods_name=periods_name,
        warmup=True,
    ) or not _is_shift_slice_assignment(
        shifted,
        output_name=output_name,
        source_name=source_name,
        periods_name=periods_name,
        warmup=False,
    ):
        return None
    if not (
        isinstance(returned, ast.Return)
        and isinstance(returned.value, ast.Name)
        and returned.value.id == output_name
    ):
        return None
    arguments = dict(bound)
    found, periods = compiler.try_static_value(arguments[periods_name])
    if not found or not isinstance(periods, int) or isinstance(periods, bool) or periods <= 0:
        compiler.unsupported(arguments[periods_name], "shift helper periods")
    return arguments[source_name], periods


def _recognized_sequence_reducer(function: ast.FunctionDef) -> str | None:
    calls = {
        _qualified_name(node.func)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }
    matched = {
        operator_name
        for qualified, operator_name in (
            ("np.logical_and.reduce", "and"),
            ("np.logical_or.reduce", "or"),
        )
        if qualified in calls
    }
    return next(iter(matched)) if len(matched) == 1 else None


def _normalized_frame_projection_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: _Compiler,
) -> tuple[ast.expr, dict[str, Any]] | None:
    parameters = [*method.args.posonlyargs, *method.args.args]
    if parameters and parameters[0].arg == "self":
        parameters = parameters[1:]
    if len(parameters) != 2:
        return None
    frame_name, keep_name = (parameter.arg for parameter in parameters)
    assignments = [
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and isinstance(statement.value, ast.ListComp)
    ]
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    assert isinstance(assignment.targets[0], ast.Name)
    assert isinstance(assignment.value, ast.ListComp)
    output_name = assignment.targets[0].id
    comprehension = assignment.value
    if not (
        isinstance(comprehension.elt, ast.Name)
        and len(comprehension.generators) == 1
        and isinstance(comprehension.generators[0].target, ast.Name)
        and comprehension.elt.id == comprehension.generators[0].target.id
        and isinstance(comprehension.generators[0].iter, ast.Attribute)
        and comprehension.generators[0].iter.attr == "columns"
        and isinstance(comprehension.generators[0].iter.value, ast.Name)
        and comprehension.generators[0].iter.value.id == frame_name
        and len(comprehension.generators[0].ifs) == 1
    ):
        return None
    column_name = comprehension.elt.id
    always_keep: str | None = None
    candidates_name: str | None = None
    keeps_requested = False
    for comparison in (
        item
        for item in ast.walk(comprehension.generators[0].ifs[0])
        if isinstance(item, ast.Compare)
    ):
        if len(comparison.ops) != 1 or len(comparison.comparators) != 1:
            return None
        right = comparison.comparators[0]
        if not isinstance(comparison.left, ast.Name) or comparison.left.id != column_name:
            continue
        if isinstance(comparison.ops[0], ast.Eq) and isinstance(right, ast.Constant):
            if not isinstance(right.value, str):
                return None
            always_keep = right.value
        elif (
            isinstance(comparison.ops[0], ast.NotIn)
            and isinstance(right, ast.Attribute)
            and isinstance(right.value, ast.Name)
            and right.value.id == "self"
        ):
            candidates_name = right.attr
        elif (
            isinstance(comparison.ops[0], ast.In)
            and isinstance(right, ast.Name)
            and right.id == keep_name
        ):
            keeps_requested = True
    returned = next(
        (
            statement
            for statement in method.body
            if isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Subscript)
            and isinstance(statement.value.value, ast.Name)
            and statement.value.value.id == frame_name
            and isinstance(statement.value.slice, ast.Name)
            and statement.value.slice.id == output_name
        ),
        None,
    )
    if (
        always_keep is None
        or candidates_name is None
        or not keeps_requested
        or returned is None
        or candidates_name not in compiler.class_constants
    ):
        return None
    candidates = compiler.class_constants[candidates_name]
    if not isinstance(candidates, list | tuple) or not all(
        isinstance(value, str) for value in candidates
    ):
        compiler.unsupported(method, "frame projection candidate columns")
    arguments = dict(bound)
    found_keep, keep = compiler.try_static_value(arguments[keep_name])
    if not found_keep or keep is None:
        keep_values: list[str] = []
    elif isinstance(keep, list | tuple) and all(isinstance(value, str) for value in keep):
        keep_values = list(keep)
    else:
        compiler.unsupported(arguments[keep_name], "frame projection keep columns")
    return arguments[frame_name], {
        "always_keep": [always_keep],
        "drop_candidates": list(candidates),
        "keep": keep_values,
    }


def _normalized_native_indicator_helper(
    method: ast.FunctionDef,
    bound: Sequence[tuple[str, ast.expr]],
    compiler: _Compiler,
) -> tuple[str, list[ast.expr], dict[str, Any]] | None:
    matched = next(
        (
            name
            for name, source in _NATIVE_HELPER_TEMPLATES.items()
            if _helper_bodies_equal(method, _template_function(source))
        ),
        None,
    )
    if matched is None:
        return None
    arguments = dict(bound)
    if matched == "chaikin-money-flow":
        found, period = compiler.try_static_value(arguments["timeperiod"])
        if not found or not isinstance(period, int) or isinstance(period, bool) or period <= 0:
            compiler.unsupported(arguments["timeperiod"], "chaikin timeperiod")
        return matched, [arguments[name] for name in ("high", "low", "close", "volume")], {
            "timeperiod": period
        }
    return matched, [arguments["arr"]], {}


def _static_informative_option(
    arguments: Mapping[str, ast.expr],
    name: str,
    compiler: _Compiler,
) -> Any:
    node = arguments.get(name)
    if node is None:
        return _INFORMATIVE_MERGE_DEFAULTS[name]
    return _required_static(node, compiler)


def _freqtrade_timeframe_minutes(
    node: ast.expr,
    timeframe: str,
    compiler: _Compiler,
) -> int:
    """Mirror pinned CCXT parsing used by Freqtrade's timeframe helper."""
    try:
        amount = int(timeframe[:-1])
        scale = {
            "y": 31_536_000,
            "M": 2_592_000,
            "w": 604_800,
            "d": 86_400,
            "h": 3_600,
            "m": 60,
            "s": 1,
        }[timeframe[-1]]
    except (IndexError, KeyError, ValueError):
        compiler.unsupported(node, f"invalid informative merge timeframe {timeframe!r}")
    return amount * scale // 60


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
