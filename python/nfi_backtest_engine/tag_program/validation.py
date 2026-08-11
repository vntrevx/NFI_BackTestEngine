"""Semantic validation and content identity for tag-program-v1."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import SpecValidationError
from ..signal_program.validation import fingerprint_program, merge_lookbacks
from ..specs import TAG_PROGRAM_SCHEMA, validate_schema

TAG_COLUMNS = ("enter_tag", "exit_tag")
OUTPUT_PHASES = {
    "enter_long": "entry",
    "enter_short": "entry",
    "enter_tag": "entry",
    "exit_long": "exit",
    "exit_short": "exit",
    "exit_tag": "exit",
}
_NUMERIC_VALUE_TYPES = {
    "bool-scalar",
    "int-scalar",
    "f64-scalar",
    "bool-column",
    "f64-column",
}
_STRING_VALUE_TYPES = {"null", "string-scalar", "string-column"}


def validate_tag_program(program: Any) -> None:
    """Validate ordered writes, wrapper initialization, raw storage, and identity."""
    validate_schema(program, TAG_PROGRAM_SCHEMA)
    if not isinstance(program, Mapping):  # pragma: no cover - schema owns it
        return

    nodes = program["nodes"]
    actual_node_ids = [node["id"] for node in nodes]
    if actual_node_ids != [f"n{index}" for index in range(1, len(nodes) + 1)]:
        raise SpecValidationError("tag-program-v1 node IDs are not canonical")
    positions = {identifier: index for index, identifier in enumerate(actual_node_ids)}
    node_by_id = {node["id"]: node for node in nodes}
    for index, node in enumerate(nodes):
        for input_id in node["inputs"]:
            position = positions.get(input_id)
            if position is None or position >= index:
                raise SpecValidationError(
                    f"tag-program-v1 node {node['id']} has a non-prior input {input_id}"
                )

    functions = program["functions"]
    if [item["id"] for item in functions] != [
        f"f{index}" for index in range(1, len(functions) + 1)
    ]:
        raise SpecValidationError("tag-program-v1 function IDs are not canonical")
    function_by_id = {item["id"]: item for item in functions}
    if [(item["phase"], item["function"]) for item in program["entrypoints"]] != [
        ("entry", "f1"),
        ("exit", "f2"),
    ]:
        raise SpecValidationError("tag-program-v1 entrypoints are not canonical")
    for entrypoint in program["entrypoints"]:
        function = function_by_id.get(entrypoint["function"])
        expected_name = f"populate_{entrypoint['phase']}_trend"
        if function is None or function["source_name"] != expected_name or function["kind"] != (
            f"entrypoint-{entrypoint['phase']}"
        ):
            raise SpecValidationError(
                f"tag-program-v1 {entrypoint['phase']} entrypoint identity differs"
            )

    owned_nodes: set[str] = set()
    for function in functions:
        for source_order, node_id in enumerate(function["node_ids"]):
            position = positions.get(node_id)
            if position is None:
                raise SpecValidationError(
                    f"tag-program-v1 function {function['id']} references a missing node"
                )
            node = nodes[position]
            if node["function"] != function["id"] or node["source_order"] != source_order:
                raise SpecValidationError(
                    f"tag-program-v1 function {function['id']} node ownership differs"
                )
            if node_id in owned_nodes:
                raise SpecValidationError(
                    f"tag-program-v1 node {node_id} has multiple function owners"
                )
            owned_nodes.add(node_id)
        if function["return_node"] not in function["node_ids"]:
            raise SpecValidationError(
                f"tag-program-v1 function {function['id']} return node is external"
            )
    if owned_nodes != set(actual_node_ids):
        raise SpecValidationError("tag-program-v1 function node ownership is incomplete")

    mutation_nodes = [node["id"] for node in nodes if node["op"] == "frame-write"]
    if program["mutation_nodes"] != mutation_nodes:
        raise SpecValidationError("tag-program-v1 mutation inventory differs from nodes")
    tag_mutation_nodes = [
        node["id"]
        for node in nodes
        if node["op"] == "frame-write"
        and any(column in TAG_COLUMNS for column in node["parameters"].get("columns", []))
    ]
    if program["tag_mutation_nodes"] != tag_mutation_nodes:
        raise SpecValidationError("tag-program-v1 tag mutation inventory differs from nodes")
    if set(program["source_map"]) != set(actual_node_ids):
        raise SpecValidationError("tag-program-v1 source map does not cover every node")
    if program["opcodes"] != sorted({node["op"] for node in nodes}):
        raise SpecValidationError("tag-program-v1 opcode inventory differs from nodes")
    if program["required_input_columns"] != sorted(program["required_input_columns"]):
        raise SpecValidationError("tag-program-v1 input columns are not canonical")
    if program["max_lookback"] != merge_lookbacks(nodes):
        raise SpecValidationError("tag-program-v1 aggregate lookback differs")

    final_by_column: dict[str, str] = {}
    phase_by_function = {item["function"]: item["phase"] for item in program["entrypoints"]}
    for node in nodes:
        if node["op"] == "format-string":
            _validate_format_string(node, node_by_id)
        if node["op"] != "frame-write":
            continue
        _validate_frame_write(node, node_by_id)
        phase = phase_by_function.get(node["function"])
        if phase is None:
            continue
        for column in node["parameters"]["columns"]:
            if OUTPUT_PHASES[column] != phase:
                raise SpecValidationError(
                    f"tag-program-v1 {column} is written during the {phase} phase"
                )
            if column in TAG_COLUMNS:
                final_by_column[column] = node["id"]
    expected_outputs = [
        {
            "column": column,
            "phase": OUTPUT_PHASES[column],
            "wrapper_initializer": "",
            "final_mutation": final_by_column.get(column),
        }
        for column in TAG_COLUMNS
    ]
    if program["tag_outputs"] != expected_outputs:
        raise SpecValidationError("tag-program-v1 final output inventory differs")
    if program["route_contract"] != {
        "canonicalization": "python-str-split",
        "original_storage": "preserve-exact",
        "trailing_whitespace": "preserve",
    }:
        raise SpecValidationError("tag-program-v1 route contract differs")

    identity = dict(program)
    fingerprint = identity.pop("fingerprint")
    if fingerprint != fingerprint_program(identity):
        raise SpecValidationError("tag-program-v1 fingerprint differs")


def _validate_format_string(
    node: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    parameters = node["parameters"]
    segments = parameters.get("segments")
    if (
        set(parameters) != {"segments"}
        or not isinstance(segments, list)
        or len(segments) != len(node["inputs"]) + 1
        or any(not isinstance(segment, str) for segment in segments)
        or node["value_type"] != "string-scalar"
    ):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} format-string contract is invalid"
        )
    allowed = {"bool-scalar", "int-scalar", "f64-scalar", "string-scalar"}
    if any(node_by_id[input_id]["value_type"] not in allowed for input_id in node["inputs"]):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} format-string input type differs"
        )


def _validate_frame_write(
    node: Mapping[str, Any],
    node_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    parameters = node["parameters"]
    if set(parameters) != {"rows", "columns", "mode", "assignment"}:
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} frame-write parameters differ"
        )
    rows = parameters["rows"]
    mode = parameters["mode"]
    assignment = parameters["assignment"]
    columns = parameters["columns"]
    if (
        rows not in {"all", "mask"}
        or mode not in {"column", "loc"}
        or assignment not in {"column-values", "scalar-broadcast", "string-append"}
    ):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} frame-write contract is invalid"
        )
    if (
        not isinstance(columns, list)
        or not columns
        or len(set(columns)) != len(columns)
        or any(column not in OUTPUT_PHASES for column in columns)
    ):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} frame-write columns are invalid"
        )
    if mode == "column" and (rows != "all" or len(columns) != 1):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} direct-column contract is invalid"
        )
    if assignment == "string-append" and (
        len(columns) != 1 or columns[0] not in TAG_COLUMNS
    ):
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} append target is invalid"
        )

    inputs = node["inputs"]
    expected_values = 1 if assignment in {"scalar-broadcast", "string-append"} else len(columns)
    expected_inputs = 1 + int(rows == "mask") + expected_values
    if len(inputs) != expected_inputs:
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} frame-write input arity differs"
        )
    if node_by_id[inputs[0]]["value_type"] != "dataframe":
        raise SpecValidationError(
            f"tag-program-v1 node {node['id']} frame-write base is not a dataframe"
        )
    value_offset = 1
    if rows == "mask":
        if node_by_id[inputs[1]]["value_type"] not in {
            "bool-scalar",
            "bool-column",
            "f64-column",
        }:
            raise SpecValidationError(
                f"tag-program-v1 node {node['id']} frame-write mask type differs"
            )
        value_offset = 2
    value_types = [node_by_id[input_id]["value_type"] for input_id in inputs[value_offset:]]
    if assignment == "scalar-broadcast":
        wants_tag = any(column in TAG_COLUMNS for column in columns)
        wants_numeric = any(column not in TAG_COLUMNS for column in columns)
        allowed = _STRING_VALUE_TYPES if wants_tag else _NUMERIC_VALUE_TYPES
        if (wants_tag and wants_numeric) or value_types[0] not in allowed:
            raise SpecValidationError(
                f"tag-program-v1 node {node['id']} scalar broadcast type differs"
            )
        return
    for column, value_type in zip(columns, value_types, strict=True):
        allowed = _STRING_VALUE_TYPES if column in TAG_COLUMNS else _NUMERIC_VALUE_TYPES
        if assignment == "string-append":
            allowed = allowed - {"null"}
        if value_type not in allowed:
            raise SpecValidationError(
                f"tag-program-v1 node {node['id']} frame-write value type differs"
            )


__all__ = [
    "OUTPUT_PHASES",
    "TAG_COLUMNS",
    "fingerprint_program",
    "merge_lookbacks",
    "validate_tag_program",
]
