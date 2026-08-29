"""Stable public facade for indicator-program-v1 compilation."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._indicator_ast import _declared_class_constants, _effective_backtest_config
from ._indicator_contract import (
    IndicatorProgramCompileError as IndicatorProgramCompileError,
)
from ._indicator_contract import (
    _fingerprint,
    _numeric_identifier_key,
    _program_lookback,
    _unsupported,
)
from .errors import SpecValidationError
from .indicator_compiler_arrays import ArraysMixin
from .indicator_compiler_bindings import BindingsLoweringMixin
from .indicator_compiler_calls import CallsMixin
from .indicator_compiler_core import CompilerState, CoreMixin
from .indicator_compiler_dataframes import GuardsMixin, ProvidersMixin
from .indicator_compiler_emission import EmissionMixin
from .indicator_compiler_expressions import ExpressionsMixin
from .indicator_compiler_indicators import IndicatorsMixin
from .indicator_compiler_informative import InformativeLoweringMixin
from .indicator_compiler_operators import OperatorsMixin
from .indicator_compiler_patterns import PatternsMixin
from .indicator_compiler_reducers import ReducersMixin
from .indicator_compiler_signal_patterns import SignalPatternsMixin
from .indicator_compiler_statements import StatementsMixin
from .indicator_compiler_static_values import StaticCallsMixin, StaticValuesMixin
from .indicator_compiler_windows import WindowsLoweringMixin
from .specs import INDICATOR_PROGRAM_SCHEMA, validate_schema
from .strategy_ir import analyze_strategy

INDICATOR_PROGRAM_VERSION = "indicator-program-v1"


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
    module_functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
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


class _Compiler(
    CoreMixin,
    StatementsMixin,
    PatternsMixin,
    SignalPatternsMixin,
    ExpressionsMixin,
    OperatorsMixin,
    CallsMixin,
    ArraysMixin,
    IndicatorsMixin,
    WindowsLoweringMixin,
    InformativeLoweringMixin,
    BindingsLoweringMixin,
    ProvidersMixin,
    GuardsMixin,
    ReducersMixin,
    StaticValuesMixin,
    StaticCallsMixin,
    EmissionMixin,
    CompilerState,
):
    """Concrete compiler composed from independent semantic concerns."""


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
