"""Value types, lookback accounting, and identity helpers for indicator programs."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Never

from .errors import StrategyAnalysisError


class IndicatorProgramCompileError(StrategyAnalysisError):
    """Indicator source cannot be represented by the causal v1 DAG."""


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


def _cast_result_type(source_type: str, target: str) -> str:
    suffix = "column" if source_type.endswith("-column") else "scalar"
    prefix = {"bool": "bool", "float": "f64", "int": "int"}[target]
    return f"{prefix}-{suffix}"


def _literal_parameters(value: Any) -> dict[str, Any]:
    if isinstance(value, float) and not math.isfinite(value):
        special = "nan" if math.isnan(value) else "+infinity" if value > 0 else "-infinity"
        return {"special": special}
    return {"value": value}


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


def _array_call_result_type(
    callable_name: str,
    inputs: Sequence[str],
    node_types: Mapping[str, str],
) -> str:
    if callable_name == "np.isnan" and len(inputs) == 1:
        input_type = node_types[inputs[0]]
        return "bool-column" if _is_column(input_type) else "bool-scalar"
    if callable_name == "np.full" and len(inputs) == 2:
        fill_type = node_types[inputs[1]]
        return {
            "bool-scalar": "bool-column",
            "int-scalar": "int-column",
            "f64-scalar": "f64-column",
            "string-scalar": "string-column",
        }.get(fill_type, "dynamic")
    return _array_result_type(inputs, node_types)


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
