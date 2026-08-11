"""Semantic validation and content identity for signal-program-v1."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import SpecValidationError
from ..specs import SIGNAL_PROGRAM_SCHEMA, validate_schema

SIGNAL_COLUMNS = ("enter_long", "enter_short", "exit_long", "exit_short")
SIGNAL_PHASES = {
    "enter_long": "entry",
    "enter_short": "entry",
    "exit_long": "exit",
    "exit_short": "exit",
}
SIGNAL_SIDES = {
    "enter_long": "long",
    "enter_short": "short",
    "exit_long": "long",
    "exit_short": "short",
}
_NUMERIC_VALUE_TYPES = {
    "bool-scalar",
    "int-scalar",
    "f64-scalar",
    "bool-column",
    "int-column",
    "f64-column",
}


def validate_signal_program(program: Any) -> None:
    """Validate schema, ordered references, mutation surface, and fingerprint."""
    validate_schema(program, SIGNAL_PROGRAM_SCHEMA)
    if not isinstance(program, Mapping):  # pragma: no cover - schema owns it
        return

    nodes = program["nodes"]
    expected_node_ids = [f"n{index}" for index in range(1, len(nodes) + 1)]
    actual_node_ids = [node["id"] for node in nodes]
    if actual_node_ids != expected_node_ids:
        raise SpecValidationError("signal-program-v1 node IDs are not canonical")
    positions = {identifier: index for index, identifier in enumerate(actual_node_ids)}
    node_by_id = {node["id"]: node for node in nodes}
    for index, node in enumerate(nodes):
        for input_id in node["inputs"]:
            position = positions.get(input_id)
            if position is None or position >= index:
                raise SpecValidationError(
                    f"signal-program-v1 node {node['id']} has a non-prior input {input_id}"
                )

    expected_function_ids = [f"f{index}" for index in range(1, len(program["functions"]) + 1)]
    actual_function_ids = [function["id"] for function in program["functions"]]
    if actual_function_ids != expected_function_ids:
        raise SpecValidationError("signal-program-v1 function IDs are not canonical")
    functions = {function["id"]: function for function in program["functions"]}
    if [(item["phase"], item["function"]) for item in program["entrypoints"]] != [
        ("entry", "f1"),
        ("exit", "f2"),
    ]:
        raise SpecValidationError("signal-program-v1 entrypoints are not canonical")
    if any(item["function"] not in functions for item in program["entrypoints"]):
        raise SpecValidationError("signal-program-v1 entrypoint function is missing")
    for entrypoint in program["entrypoints"]:
        function = functions[entrypoint["function"]]
        expected_name = f"populate_{entrypoint['phase']}_trend"
        if function["source_name"] != expected_name or function["kind"] != (
            f"entrypoint-{entrypoint['phase']}"
        ):
            raise SpecValidationError(
                f"signal-program-v1 {entrypoint['phase']} entrypoint identity differs"
            )

    owned_nodes: set[str] = set()
    for function in program["functions"]:
        for source_order, node_id in enumerate(function["node_ids"]):
            node = nodes[positions[node_id]]
            if node["function"] != function["id"] or node["source_order"] != source_order:
                raise SpecValidationError(
                    f"signal-program-v1 function {function['id']} node ownership differs"
                )
            if node_id in owned_nodes:
                raise SpecValidationError(
                    f"signal-program-v1 node {node_id} has multiple function owners"
                )
            owned_nodes.add(node_id)
        if function["return_node"] not in function["node_ids"]:
            raise SpecValidationError(
                f"signal-program-v1 function {function['id']} return node is external"
            )
    if owned_nodes != set(actual_node_ids):
        raise SpecValidationError("signal-program-v1 function node ownership is incomplete")

    mutation_nodes = [node["id"] for node in nodes if node["op"] == "frame-write"]
    if program["mutation_nodes"] != mutation_nodes:
        raise SpecValidationError("signal-program-v1 mutation inventory differs from nodes")
    if set(program["source_map"]) != set(actual_node_ids):
        raise SpecValidationError("signal-program-v1 source map does not cover every node")
    if program["opcodes"] != sorted({node["op"] for node in nodes}):
        raise SpecValidationError("signal-program-v1 opcode inventory differs from nodes")
    if program["required_input_columns"] != sorted(program["required_input_columns"]):
        raise SpecValidationError("signal-program-v1 input columns are not canonical")
    if program["max_lookback"] != merge_lookbacks(nodes):
        raise SpecValidationError("signal-program-v1 aggregate lookback differs")

    final_by_column: dict[str, str] = {}
    phase_by_function = {item["function"]: item["phase"] for item in program["entrypoints"]}
    for node in nodes:
        if node["op"] != "frame-write":
            continue
        _validate_frame_write(node, node_by_id)
        phase = phase_by_function.get(node["function"])
        if phase is None:
            continue
        for column in node["parameters"]["columns"]:
            if SIGNAL_PHASES[column] != phase:
                raise SpecValidationError(
                    f"signal-program-v1 {column} is written during the {phase} phase"
                )
            final_by_column[column] = node["id"]
    expected_outputs = [
        {
            "column": column,
            "phase": SIGNAL_PHASES[column],
            "side": SIGNAL_SIDES[column],
            "final_mutation": final_by_column[column],
        }
        for column in SIGNAL_COLUMNS
        if column in final_by_column
    ]
    if program["signal_outputs"] != expected_outputs:
        raise SpecValidationError("signal-program-v1 final output inventory differs")

    identity = dict(program)
    fingerprint = identity.pop("fingerprint")
    if fingerprint != fingerprint_program(identity):
        raise SpecValidationError("signal-program-v1 fingerprint differs")


def _validate_frame_write(
    node: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    parameters = node["parameters"]
    if set(parameters) != {"rows", "columns", "mode", "assignment"}:
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write parameters differ"
        )
    rows = parameters["rows"]
    mode = parameters["mode"]
    assignment = parameters["assignment"]
    columns = parameters["columns"]
    if (
        rows not in {"all", "mask"}
        or mode not in {"column", "loc"}
        or assignment
        not in {
            "column-values",
            "scalar-broadcast",
        }
    ):
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write contract is invalid"
        )
    if (
        not isinstance(columns, list)
        or not columns
        or len(set(columns)) != len(columns)
        or any(column not in SIGNAL_COLUMNS for column in columns)
    ):
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write columns are invalid"
        )
    if mode == "column" and (rows != "all" or len(columns) != 1):
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} direct-column contract is invalid"
        )
    inputs = node["inputs"]
    expected_values = 1 if assignment == "scalar-broadcast" else len(columns)
    expected_inputs = 1 + int(rows == "mask") + expected_values
    if len(inputs) != expected_inputs:
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write input arity differs"
        )
    if node_by_id[inputs[0]]["value_type"] != "dataframe":
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write base is not a dataframe"
        )
    value_offset = 1
    if rows == "mask":
        if node_by_id[inputs[1]]["value_type"] not in {
            "bool-scalar",
            "bool-column",
            "f64-column",
        }:
            raise SpecValidationError(
                f"signal-program-v1 node {node['id']} frame-write mask type differs"
            )
        value_offset = 2
    if any(
        node_by_id[input_id]["value_type"] not in _NUMERIC_VALUE_TYPES
        for input_id in inputs[value_offset:]
    ):
        raise SpecValidationError(
            f"signal-program-v1 node {node['id']} frame-write value type differs"
        )


def fingerprint_program(program: Mapping[str, Any]) -> str:
    """Return a path-independent canonical content hash."""
    identity = copy.deepcopy(dict(program))
    source = identity.get("source")
    if isinstance(source, dict):
        source.pop("path", None)
    return hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def merge_lookbacks(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the same causal lookback contract used by vector nodes."""
    lookbacks = [node["lookback"] for node in nodes]
    if all(item["kind"] == "finite" and isinstance(item["candles"], int) for item in lookbacks):
        return {
            "kind": "finite",
            "candles": max((int(item["candles"]) for item in lookbacks), default=0),
            "expression": None,
            "causal": True,
        }
    kinds = sorted({str(item["kind"]) for item in lookbacks})
    return {
        "kind": kinds[0] if len(kinds) == 1 else "mixed",
        "candles": None,
        "expression": "+".join(kinds),
        "causal": all(bool(item["causal"]) for item in lookbacks),
    }
