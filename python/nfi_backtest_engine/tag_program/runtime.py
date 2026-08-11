"""Independent Python reference executor for tag-program-v1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from ..signal_program.runtime import (
    SignalProgramExecutionError,
    _require_frame,
    _require_mask,
)
from ..signal_program.runtime import (
    _Runtime as _SignalRuntime,
)
from .validation import validate_tag_program


class TagProgramExecutionError(SignalProgramExecutionError):
    """A validated tag program cannot execute with the supplied frame."""


def execute_tag_program(
    program: Mapping[str, Any],
    dataframe: pd.DataFrame,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Execute Freqtrade wrapper initialization and ordered strategy mutations."""
    validate_tag_program(program)
    runtime = _Runtime(program)
    frame = dataframe.copy(deep=True)
    metadata_value = dict(metadata or {})
    initializer_by_phase = {
        output["phase"]: (output["column"], output["wrapper_initializer"])
        for output in program["tag_outputs"]
    }
    for entrypoint in program["entrypoints"]:
        column, initializer = initializer_by_phase[entrypoint["phase"]]
        frame.loc[:, column] = initializer
        value = runtime.function(entrypoint["function"], [frame, metadata_value])
        if not isinstance(value, pd.DataFrame):
            raise TagProgramExecutionError(
                f"tag {entrypoint['phase']} entrypoint did not return a DataFrame"
            )
        frame = value
    return frame


def canonical_tag_route(value: str | None) -> tuple[str, ...]:
    """Return NFI's whitespace-token route without altering the stored tag."""
    return () if value is None else tuple(value.split())


class _Runtime(_SignalRuntime):
    def _node(
        self,
        node: Mapping[str, Any],
        values: Mapping[str, Any],
        parameters: Mapping[str, Any],
    ) -> Any:
        if node["op"] == "format-string":
            inputs = [values[input_id] for input_id in node["inputs"]]
            segments = node["parameters"]["segments"]
            if len(segments) != len(inputs) + 1:
                raise TagProgramExecutionError(
                    f"tag node {node['id']} format-string segment count differs"
                )
            result = segments[0]
            for value, suffix in zip(inputs, segments[1:], strict=True):
                result += f"{value}{suffix}"
            return result
        if node["op"] == "frame-write":
            inputs = [values[input_id] for input_id in node["inputs"]]
            try:
                return _frame_write(node, inputs)
            except TagProgramExecutionError:
                raise
            except Exception as exc:
                raise TagProgramExecutionError(
                    f"tag node {node['id']} (frame-write) failed: {exc}"
                ) from exc
        try:
            return super()._node(node, values, parameters)
        except SignalProgramExecutionError as exc:
            raise TagProgramExecutionError(str(exc).replace("signal node", "tag node")) from exc


def _frame_write(node: Mapping[str, Any], inputs: Sequence[Any]) -> pd.DataFrame:
    options = node["parameters"]
    frame = _require_frame(inputs[0], node["id"]).copy(deep=True)
    offset = 1
    mask: Any = None
    if options["rows"] == "mask":
        mask = _require_mask(inputs[offset], frame.index, node["id"])
        offset += 1
    values = list(inputs[offset:])
    columns = options["columns"]
    assignment = options["assignment"]
    if assignment == "scalar-broadcast":
        assigned: Any = values[0]
    else:
        assigned = values[0] if len(columns) == 1 else values

    rows: Any = slice(None) if mask is None else mask
    selector: Any = columns[0] if len(columns) == 1 else columns
    if assignment == "string-append":
        if len(columns) != 1 or len(values) != 1:
            raise TagProgramExecutionError(
                f"tag node {node['id']} has an invalid append contract"
            )
        frame.loc[rows, selector] = frame.loc[rows, selector] + assigned
    elif options["mode"] == "column":
        if mask is not None or len(columns) != 1:
            raise TagProgramExecutionError(
                f"tag node {node['id']} has an invalid direct-column contract"
            )
        frame[columns[0]] = assigned
    elif options["mode"] == "loc":
        frame.loc[rows, selector] = assigned
    else:
        raise TagProgramExecutionError(
            f"tag node {node['id']} has unknown assignment mode {options['mode']!r}"
        )
    return frame
