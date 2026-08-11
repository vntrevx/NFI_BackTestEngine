"""Independent Python reference executor for signal-program-v1."""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype

from ..errors import StrategyAnalysisError
from .validation import validate_signal_program


class SignalProgramExecutionError(StrategyAnalysisError):
    """A validated signal program cannot execute with the supplied frame."""


def execute_signal_program(
    program: Mapping[str, Any],
    dataframe: pd.DataFrame,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Execute raw entry then exit mutations while preserving Pandas null semantics."""
    validate_signal_program(program)
    runtime = _Runtime(program)
    frame = dataframe.copy(deep=True)
    metadata_value = dict(metadata or {})
    for entrypoint in program["entrypoints"]:
        value = runtime.function(entrypoint["function"], [frame, metadata_value])
        if not isinstance(value, pd.DataFrame):
            raise SignalProgramExecutionError(
                f"signal {entrypoint['phase']} entrypoint did not return a DataFrame"
            )
        frame = value
    return frame


class _Runtime:
    def __init__(self, program: Mapping[str, Any]) -> None:
        self.functions = {item["id"]: item for item in program["functions"]}
        self.nodes = {item["id"]: item for item in program["nodes"]}

    def function(self, function_id: str, arguments: Sequence[Any]) -> Any:
        function = self.functions[function_id]
        if len(arguments) != len(function["parameters"]):
            raise SignalProgramExecutionError(
                f"signal function {function_id} argument count differs"
            )
        parameters = {
            parameter["node"]: value
            for parameter, value in zip(function["parameters"], arguments, strict=True)
        }
        values: dict[str, Any] = {}
        for node_id in function["node_ids"]:
            node = self.nodes[node_id]
            try:
                values[node_id] = self._node(node, values, parameters)
            except SignalProgramExecutionError:
                raise
            except Exception as exc:
                raise SignalProgramExecutionError(
                    f"signal node {node_id} ({node['op']}) failed: {exc}"
                ) from exc
        return values[function["return_node"]]

    def _node(
        self,
        node: Mapping[str, Any],
        values: Mapping[str, Any],
        parameters: Mapping[str, Any],
    ) -> Any:
        opcode = node["op"]
        if opcode == "parameter":
            return parameters[node["id"]]
        inputs = [values[input_id] for input_id in node["inputs"]]
        options = node["parameters"]
        if opcode == "literal":
            return options.get("value")
        if opcode == "column-read":
            frame = _require_frame(inputs[0], node["id"])
            column = options["column"]
            if column not in frame:
                raise SignalProgramExecutionError(
                    f"signal node {node['id']} requires missing column {column!r}"
                )
            return frame[column]
        if opcode == "metadata-read":
            metadata = inputs[0]
            if not isinstance(metadata, Mapping):
                raise SignalProgramExecutionError(
                    f"signal node {node['id']} metadata input is invalid"
                )
            key = options["key"]
            if key not in metadata:
                raise SignalProgramExecutionError(
                    f"signal node {node['id']} requires missing metadata key {key!r}"
                )
            return metadata[key]
        if opcode == "binary":
            return _binary(options["operator"], inputs[0], inputs[1])
        if opcode == "compare":
            return _compare(options["operator"], inputs[0], inputs[1])
        if opcode == "logical":
            return _logical(options["operator"], inputs)
        if opcode == "unary":
            return _unary(options["operator"], inputs[0])
        if opcode == "select":
            return np.where(inputs[0], inputs[1], inputs[2])
        if opcode == "shift":
            value = inputs[0]
            periods = int(options["periods"])
            if isinstance(value, pd.Series):
                return value.shift(periods)
            result = np.full(len(value), np.nan, dtype=np.float64)
            if periods == 0:
                return np.asarray(value).copy()
            result[periods:] = np.asarray(value)[:-periods]
            return result
        if opcode == "cast":
            return _cast(inputs[0], options["target"])
        if opcode == "scalar-call":
            return _scalar_call(options["name"], inputs)
        if opcode == "array-call":
            return _array_call(options["name"], inputs, options.get("arguments", {}))
        if opcode == "function-call":
            return self.function(options["function"], inputs)
        if opcode == "frame-write":
            return _frame_write(node, inputs)
        if opcode == "instrumentation":
            return 0.0 if options["name"] == "time.perf_counter" else None
        if opcode == "return":
            return inputs[0]
        raise SignalProgramExecutionError(
            f"signal node {node['id']} uses unsupported opcode {opcode!r}"
        )


def _frame_write(node: Mapping[str, Any], inputs: Sequence[Any]) -> pd.DataFrame:
    options = node["parameters"]
    frame = _require_frame(inputs[0], node["id"]).copy(deep=True)
    offset = 1
    mask: pd.Series | np.ndarray[Any, np.dtype[np.bool_]] | None = None
    if options["rows"] == "mask":
        mask = _require_mask(inputs[offset], frame.index, node["id"])
        offset += 1
    values = list(inputs[offset:])
    columns = options["columns"]
    assignment = options["assignment"]
    assigned: Any
    if assignment == "scalar-broadcast":
        if len(values) != 1:
            raise SignalProgramExecutionError(
                f"signal node {node['id']} scalar broadcast has multiple values"
            )
        assigned = values[0]
    else:
        if len(values) != len(columns):
            raise SignalProgramExecutionError(
                f"signal node {node['id']} column/value arity differs"
            )
        assigned = values[0] if len(columns) == 1 else values

    if options["mode"] == "column":
        if mask is not None or len(columns) != 1:
            raise SignalProgramExecutionError(
                f"signal node {node['id']} has an invalid direct-column contract"
            )
        frame[columns[0]] = assigned
    elif options["mode"] == "loc":
        rows: Any = slice(None) if mask is None else mask
        selector: Any = columns[0] if len(columns) == 1 else columns
        frame.loc[rows, selector] = assigned
    else:
        raise SignalProgramExecutionError(
            f"signal node {node['id']} has unknown assignment mode {options['mode']!r}"
        )
    return frame


def _require_mask(value: Any, index: pd.Index, node_id: str) -> pd.Series | np.ndarray[Any, Any]:
    if isinstance(value, pd.Series):
        if not value.index.equals(index):
            value = value.reindex(index)
        if not is_bool_dtype(value.dtype):
            raise SignalProgramExecutionError(
                f"signal node {node_id} mask dtype is not boolean: {value.dtype}"
            )
        return value
    array = np.asarray(value)
    if array.ndim != 1 or len(array) != len(index) or not is_bool_dtype(array.dtype):
        raise SignalProgramExecutionError(
            f"signal node {node_id} mask is not a row-aligned boolean vector"
        )
    return array


def _require_frame(value: Any, node_id: str) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame):
        raise SignalProgramExecutionError(f"signal node {node_id} input is not a DataFrame")
    return value


def _binary(name: str, left: Any, right: Any) -> Any:
    operation = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
        "floor-divide": operator.floordiv,
        "modulo": operator.mod,
        "power": operator.pow,
    }.get(name)
    if operation is None:
        raise SignalProgramExecutionError(f"unknown signal binary operator {name!r}")
    return operation(left, right)


def _compare(name: str, left: Any, right: Any) -> Any:
    operation = {
        "equal": operator.eq,
        "not-equal": operator.ne,
        "less-than": operator.lt,
        "less-than-or-equal": operator.le,
        "greater-than": operator.gt,
        "greater-than-or-equal": operator.ge,
    }.get(name)
    if operation is None:
        raise SignalProgramExecutionError(f"unknown signal comparison {name!r}")
    return operation(left, right)


def _logical(name: str, values: Sequence[Any]) -> Any:
    if not values:
        raise SignalProgramExecutionError("signal logical operation has no inputs")
    operation = operator.and_ if name == "and" else operator.or_ if name == "or" else None
    if operation is None:
        raise SignalProgramExecutionError(f"unknown signal logical operator {name!r}")
    result = values[0]
    for value in values[1:]:
        result = operation(result, value)
    return result


def _unary(name: str, value: Any) -> Any:
    if name == "negate":
        return -value
    if name == "positive":
        return +value
    if name in {"not", "invert"}:
        return ~value
    raise SignalProgramExecutionError(f"unknown signal unary operator {name!r}")


def _cast(value: Any, target: str) -> Any:
    dtype = {"bool": bool, "float": float, "int": int, "array": None}.get(target)
    if target == "array":
        return np.asarray(value)
    if dtype is None:
        raise SignalProgramExecutionError(f"unknown signal cast target {target!r}")
    if isinstance(value, pd.Series):
        return value.astype(dtype)
    return np.asarray(value).astype(dtype) if isinstance(value, np.ndarray) else dtype(value)


def _scalar_call(name: str, values: Sequence[Any]) -> Any:
    functions = {"abs": abs, "bool": bool, "float": float, "int": int, "max": max, "min": min}
    function = functions.get(name)
    if function is None:
        raise SignalProgramExecutionError(f"unknown signal scalar call {name!r}")
    return function(*values)


def _array_call(name: str, values: Sequence[Any], arguments: Mapping[str, Any]) -> Any:
    function = getattr(np, name, None)
    if function is None or not callable(function):
        raise SignalProgramExecutionError(f"unknown signal NumPy call {name!r}")
    return function(*values, **arguments)
