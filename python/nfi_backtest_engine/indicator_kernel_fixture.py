"""Generate deterministic, bit-exact TA-Lib kernel oracle fixtures.

The operation and parameter surface comes from an indicator inventory.  Expected
columns always come from the pinned Python TA-Lib binding; this module never
contains hand-authored expected indicator values.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import talib
from talib import abstract

SCHEMA_VERSION = "nfi-indicator-kernel-fixture-v1"
DEFAULT_ROWS = 2_200


class KernelFixtureError(ValueError):
    """Raised when an inventory cannot produce an exact kernel fixture."""


def generate_talib_kernel_fixture(
    inventory_path: Path,
    *,
    rows: int = DEFAULT_ROWS,
) -> dict[str, Any]:
    """Return a deterministic fixture for every TA-Lib operation in an inventory."""
    if rows < 600:
        raise KernelFixtureError("at least 600 rows are required to cover NFI's longest window")
    inventory = cast(dict[str, Any], json.loads(inventory_path.read_text(encoding="utf-8")))
    columns = _input_columns(rows)
    cases: list[dict[str, Any]] = []
    operations = cast(list[dict[str, Any]], inventory.get("operations", []))
    for operation in operations:
        if operation.get("family") != "talib":
            continue
        callable_name = _required_string(operation, "callable")
        name = callable_name.removeprefix("talib.")
        if name == callable_name or not hasattr(talib, name):
            raise KernelFixtureError(
                f"inventory callable is not provided by TA-Lib: {callable_name}"
            )
        function = abstract.Function(name)
        input_names = _flatten_input_names(cast(Mapping[str, object], function.input_names))
        parameter_sets = _parameter_sets(operation, cast(Mapping[str, object], function.parameters))
        output_names = [str(item) for item in cast(Sequence[object], function.output_names)]
        native_function = cast(Any, getattr(talib, name))
        for parameters in parameter_sets:
            inputs = [columns[column] for column in input_names]
            raw_outputs = native_function(*inputs, **parameters)
            outputs = raw_outputs if isinstance(raw_outputs, tuple) else (raw_outputs,)
            if len(outputs) != len(output_names):
                raise KernelFixtureError(
                    f"{callable_name} returned {len(outputs)} columns, expected {len(output_names)}"
                )
            identity = _canonical_json({"name": name, "arguments": parameters})
            cases.append(
                {
                    "id": f"{name.lower()}-{hashlib.sha256(identity).hexdigest()[:12]}",
                    "family": "talib",
                    "name": name,
                    "arguments": parameters,
                    "input_columns": input_names,
                    "outputs": [
                        {
                            "name": output_name,
                            "values": [_f64_token(float(value)) for value in np.asarray(output)],
                        }
                        for output_name, output in zip(output_names, outputs, strict=True)
                    ],
                }
            )
    cases.extend(_rolling_cases(operations, columns["close"], rows))
    if not cases:
        raise KernelFixtureError("inventory contains no TA-Lib operations")
    cases.sort(key=lambda item: str(item["id"]))
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "inventory_fingerprint": _required_string(inventory, "fingerprint"),
            "upstream": inventory.get("upstream"),
            "strategy_sha256": cast(dict[str, Any], inventory.get("source", {})).get("sha256"),
            "python_talib_version": talib.__version__,
            "ta_lib_version": talib.__ta_version__.decode("ascii").split(maxsplit=1)[0],
            "pandas_version": pd.__version__,
            "numpy_version": np.__version__,
        },
        "rows": rows,
        "inputs": {
            name: [_f64_token(float(value)) for value in values]
            for name, values in sorted(columns.items())
        },
        "cases": cases,
    }
    document["fingerprint"] = hashlib.sha256(_canonical_json(document)).hexdigest()
    return document


def write_talib_kernel_fixture(inventory_path: Path, output_path: Path, *, rows: int) -> None:
    """Generate and atomically replace a formatted fixture document."""
    document = generate_talib_kernel_fixture(inventory_path, rows=rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)


def _input_columns(rows: int) -> dict[str, np.ndarray[Any, np.dtype[np.float64]]]:
    open_values: list[float] = []
    high_values: list[float] = []
    low_values: list[float] = []
    close_values: list[float] = []
    volume_values: list[float] = []
    for index in range(rows):
        if index < 40:
            close = 100.0
        elif 220 <= index < 340:
            close = 127.5
        else:
            trend = (index // 31) * 0.125
            cycle = ((index * 17) % 97) * 0.25
            shock = -12.0 if index % 113 == 0 else (9.0 if index % 79 == 0 else 0.0)
            close = 90.0 + trend + cycle + shock
        open_value = close + ((index % 9) - 4) * 0.125
        high = max(open_value, close) + 0.25 + (index % 5) * 0.125
        low = min(open_value, close) - 0.25 - (index % 7) * 0.125
        volume = 0.0 if index % 29 == 0 or 220 <= index < 245 else 500.0 + ((index * 43) % 401)
        open_values.append(open_value)
        high_values.append(high)
        low_values.append(low)
        close_values.append(close)
        volume_values.append(volume)
    return {
        "open": np.asarray(open_values, dtype=np.float64),
        "high": np.asarray(high_values, dtype=np.float64),
        "low": np.asarray(low_values, dtype=np.float64),
        "close": np.asarray(close_values, dtype=np.float64),
        "volume": np.asarray(volume_values, dtype=np.float64),
    }


def _flatten_input_names(input_names: Mapping[str, object]) -> list[str]:
    flattened: list[str] = []
    for value in input_names.values():
        if isinstance(value, str):
            flattened.append(value)
        elif isinstance(value, Sequence):
            flattened.extend(str(item) for item in value)
        else:
            raise KernelFixtureError(f"unsupported TA-Lib input description: {value!r}")
    return flattened


def _rolling_cases(
    operations: Sequence[Mapping[str, Any]],
    values: np.ndarray[Any, np.dtype[np.float64]],
    rows: int,
) -> list[dict[str, Any]]:
    windows: set[int] = set()
    reducers: set[str] = set()
    for operation in operations:
        callable_name = operation.get("callable")
        if callable_name == "pandas.rolling":
            for occurrence in cast(Sequence[Mapping[str, Any]], operation.get("occurrences", [])):
                arguments = cast(Sequence[Mapping[str, Any]], occurrence.get("arguments", []))
                if not arguments:
                    raise KernelFixtureError("pandas.rolling occurrence has no window")
                literal = arguments[0].get("literal")
                if not isinstance(literal, int) or isinstance(literal, bool) or literal < 1:
                    raise KernelFixtureError("pandas.rolling window is not a positive literal")
                windows.add(literal)
        elif callable_name in {"pandas.max", "pandas.mean", "pandas.min", "pandas.sum"}:
            reducers.add(str(callable_name).removeprefix("pandas."))
    if not windows and not reducers:
        return []
    if not windows or not reducers:
        raise KernelFixtureError("rolling windows and reducers must both be inventoried")
    if rows < max(windows):
        raise KernelFixtureError(
            f"fixture rows {rows} do not cover longest rolling window {max(windows)}"
        )
    cases = []
    series = pd.Series(values, copy=False)
    for window in sorted(windows):
        for reducer in sorted(reducers):
            rolling = series.rolling(window=window, min_periods=window, center=False)
            output = getattr(rolling, reducer)()
            arguments = {"center": False, "min_periods": window, "window": window}
            identity = _canonical_json({"name": f"rolling.{reducer}", "arguments": arguments})
            cases.append(
                {
                    "id": f"rolling-{reducer}-{hashlib.sha256(identity).hexdigest()[:12]}",
                    "family": "pandas",
                    "name": f"rolling.{reducer}",
                    "arguments": arguments,
                    "input_columns": ["close"],
                    "outputs": [
                        {
                            "name": "real",
                            "values": [_f64_token(float(value)) for value in output.to_numpy()],
                        }
                    ],
                }
            )
    return cases


def _parameter_sets(
    operation: Mapping[str, Any],
    defaults: Mapping[str, object],
) -> list[dict[str, int | float]]:
    parameter_sets: dict[bytes, dict[str, int | float]] = {}
    for occurrence in cast(Sequence[Mapping[str, Any]], operation.get("occurrences", [])):
        parameters = {name: _numeric_parameter(value) for name, value in defaults.items()}
        for argument in cast(Sequence[Mapping[str, Any]], occurrence.get("arguments", [])):
            name = _required_string(argument, "name")
            if name.startswith("#"):
                continue
            literal = argument.get("literal")
            if not isinstance(literal, int | float) or isinstance(literal, bool):
                callable_name = _required_string(operation, "callable")
                raise KernelFixtureError(
                    f"non-literal TA-Lib parameter {name} in {callable_name}"
                )
            parameters[name] = literal
        parameter_sets[_canonical_json(parameters)] = parameters
    if not parameter_sets:
        parameter_sets[_canonical_json(defaults)] = {
            name: _numeric_parameter(value) for name, value in defaults.items()
        }
    return [parameter_sets[key] for key in sorted(parameter_sets)]


def _numeric_parameter(value: object) -> int | float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise KernelFixtureError(f"TA-Lib parameter is not numeric: {value!r}")
    return value


def _f64_token(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return f"0x{struct.unpack('>Q', struct.pack('>d', value))[0]:016x}"


def _required_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise KernelFixtureError(f"missing non-empty string {key}")
    return value


def _canonical_json(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
