"""Build the independent Python side of the M21 vector-shadow proof."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from .indicator_program import compile_indicator_program, validate_indicator_program
from .signal_fixture import (
    FIXTURE_PATH as SIGNAL_FIXTURE_PATH,
)
from .signal_fixture import (
    canonical_sha256 as signal_fixture_sha256,
)
from .signal_fixture import (
    decode_frame,
)
from .signal_program import compile_signal_program, execute_signal_program
from .tag_fixture import FIXTURE_PATH as TAG_FIXTURE_PATH
from .tag_fixture import assert_fixture_identity as assert_tag_fixture_identity
from .tag_program import compile_tag_program, execute_tag_program
from .vector_worker import _materialize_execution_signals

INDICATOR_CONTRACT_PATH = Path(
    "benchmarks/reference/strategies/VectorShadowIndicatorContract.py"
)
SIGNAL_CONTRACT_PATH = Path("benchmarks/reference/strategies/SignalProgramContract.py")
TAG_CONTRACT_PATH = Path("benchmarks/reference/strategies/TagProgramContract.py")
OUTPUT_ROOT = Path("benchmarks/reference/vector-shadow")
FIXTURE_PATH = OUTPUT_ROOT / "freqtrade-2026.5.1.json"
INDICATOR_PROGRAM_PATH = OUTPUT_ROOT / "indicator-program.json"
SIGNAL_PROGRAM_PATH = OUTPUT_ROOT / "signal-program.json"
TAG_PROGRAM_PATH = OUTPUT_ROOT / "tag-program.json"
_SIGNAL_COLUMNS = ("enter_long", "enter_short", "exit_long", "exit_short")
_TAG_COLUMNS = ("enter_tag", "exit_tag")


def generate_bundle() -> tuple[dict[str, object], dict[str, dict[str, Any]]]:
    """Generate program contracts plus a path-independent exact shadow fixture."""
    repository = _repository_root()
    indicator_program = compile_indicator_program(
        repository / INDICATOR_CONTRACT_PATH,
        class_name="VectorShadowIndicatorContract",
    )
    signal_program = compile_signal_program(
        repository / SIGNAL_CONTRACT_PATH,
        class_name="SignalProgramContract",
    )
    tag_program = compile_tag_program(
        repository / TAG_CONTRACT_PATH,
        class_name="TagProgramContract",
    )
    programs = {
        "indicator": _portable_program(indicator_program, INDICATOR_CONTRACT_PATH),
        "signal": _portable_program(signal_program, SIGNAL_CONTRACT_PATH),
        "tag": _portable_program(tag_program, TAG_CONTRACT_PATH),
    }

    indicator_input = _indicator_input()
    indicator_output = _execute_indicator_contract(
        repository / INDICATOR_CONTRACT_PATH,
        indicator_input,
    )
    signal_oracle = _load_json(repository / SIGNAL_FIXTURE_PATH)
    tag_oracle = _load_json(repository / TAG_FIXTURE_PATH)
    _validate_official_oracles(signal_oracle, tag_oracle)
    signal_input = decode_frame(_mapping(signal_oracle["input"], "signal input"))
    tag_input = decode_frame(_mapping(tag_oracle["input"], "tag input"))
    signal_output = execute_signal_program(
        signal_program,
        signal_input,
        metadata={"pair": "ETH/USDT"},
    )
    tag_output = execute_tag_program(
        tag_program,
        tag_input,
        metadata={"pair": "ETH/USDT"},
    )
    official_signal_output = decode_frame(
        _mapping(signal_oracle["output"], "official signal output")
    )
    official_tag_output = decode_frame(_mapping(tag_oracle["output"], "official tag output"))
    _assert_exact_columns(signal_output, official_signal_output, _SIGNAL_COLUMNS, "signal")
    _assert_exact_columns(
        tag_output,
        official_tag_output,
        (*_SIGNAL_COLUMNS, *_TAG_COLUMNS),
        "tag",
    )
    materialized = _materialize_execution_signals(tag_output)
    execution_columns = tuple(f"nfi_exec_{column}" for column in (*_SIGNAL_COLUMNS, *_TAG_COLUMNS))
    execution_start_index = 1

    program_paths = {
        "indicator": INDICATOR_PROGRAM_PATH,
        "signal": SIGNAL_PROGRAM_PATH,
        "tag": TAG_PROGRAM_PATH,
    }
    fixture: dict[str, object] = {
        "schema_version": "vector-shadow-fixture-v1",
        "source": {
            "freqtrade_version": tag_oracle["source"]["version"],
            "freqtrade_commit": tag_oracle["source"]["commit"],
            "freqtrade_interface_sha256": tag_oracle["source"]["interface_sha256"],
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "signal_oracle_fingerprint": signal_oracle["fingerprint"],
            "tag_oracle_fingerprint": tag_oracle["fingerprint"],
        },
        "programs": {
            name: {
                "path": str(path),
                "fingerprint": programs[name]["fingerprint"],
                "sha256": _document_sha256(programs[name]),
            }
            for name, path in program_paths.items()
        },
        "cases": {
            "indicator": {
                "input": encode_frame(indicator_input),
                "outputs": ["delta", "previous_close", "mean3", "selected"],
                "expected": encode_frame(
                    indicator_output.loc[
                        :, ["delta", "previous_close", "mean3", "selected"]
                    ]
                ),
            },
            "signal": {
                "input": encode_frame(signal_input),
                "outputs": list(_SIGNAL_COLUMNS),
                "expected": encode_frame(official_signal_output.loc[:, list(_SIGNAL_COLUMNS)]),
            },
            "tag": {
                "input": encode_frame(tag_input),
                "outputs": [*_SIGNAL_COLUMNS, *_TAG_COLUMNS],
                "expected": encode_frame(
                    official_tag_output.loc[:, [*_SIGNAL_COLUMNS, *_TAG_COLUMNS]]
                ),
            },
            "execution": {
                "source": "tag",
                "source_row_shift": 1,
                "execution_start_index": execution_start_index,
                "outputs": list(execution_columns),
                "expected": encode_frame(materialized.loc[:, list(execution_columns)]),
                "enabled_indexes": {
                    column: _enabled_indexes(
                        cast(pd.Series, materialized[f"nfi_exec_{column}"]),
                        execution_start_index,
                    )
                    for column in _SIGNAL_COLUMNS
                },
            },
        },
    }
    fixture["fingerprint"] = canonical_sha256(fixture)
    return fixture, programs


def _validate_official_oracles(
    signal_oracle: Mapping[str, Any], tag_oracle: Mapping[str, Any]
) -> None:
    if signal_oracle.get("fingerprint") != signal_fixture_sha256(signal_oracle):
        raise ValueError("official signal oracle fingerprint differs")
    assert_tag_fixture_identity(tag_oracle)
    for key in ("version", "commit", "interface_sha256", "method_sha256", "pandas"):
        if signal_oracle["source"][key] != tag_oracle["source"][key]:
            raise ValueError(f"official Signal and Tag oracle {key} differs")
    expected_call_order = ["advise_entry", "advise_exit"]
    if signal_oracle.get("call_order") != expected_call_order or tag_oracle.get(
        "call_order"
    ) != expected_call_order:
        raise ValueError("official vector wrapper call order differs")


def _assert_exact_columns(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    columns: tuple[str, ...],
    label: str,
) -> None:
    actual_encoded = encode_frame(actual.loc[:, list(columns)])
    expected_encoded = encode_frame(expected.loc[:, list(columns)])
    if actual_encoded != expected_encoded:
        raise ValueError(f"Python {label} runtime differs from the official Freqtrade oracle")


def write_bundle(root: Path | None = None) -> dict[str, object]:
    """Write canonical fixture and program documents below the repository."""
    repository = root or _repository_root()
    fixture, programs = generate_bundle()
    destinations = {
        "indicator": repository / INDICATOR_PROGRAM_PATH,
        "signal": repository / SIGNAL_PROGRAM_PATH,
        "tag": repository / TAG_PROGRAM_PATH,
    }
    for name, destination in destinations.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_canonical_json(programs[name]) + "\n", encoding="utf-8")
    fixture_destination = repository / FIXTURE_PATH
    fixture_destination.parent.mkdir(parents=True, exist_ok=True)
    fixture_destination.write_text(_canonical_json(fixture) + "\n", encoding="utf-8")
    return fixture


def encode_frame(frame: pd.DataFrame) -> dict[str, object]:
    """Encode nulls separately from exact f64 bits and preserve raw strings."""
    columns = []
    for name in frame.columns:
        series = frame[name]
        if not isinstance(series, pd.Series):  # pragma: no cover - pandas contract
            raise TypeError(f"shadow frame column is not one-dimensional: {name}")
        kind, values = _encode_series(series)
        columns.append({"name": str(name), "type": kind, "values": values})
    return {"rows": len(frame), "columns": columns}


def decode_exact_frame(document: Mapping[str, object]) -> pd.DataFrame:
    """Decode the bit-exact fixture representation for Python assertions."""
    raw_columns = document.get("columns")
    if not isinstance(raw_columns, list):
        raise TypeError("shadow frame columns must be a list")
    result: dict[str, object] = {}
    for raw in raw_columns:
        column = _mapping(raw, "shadow column")
        name = str(column["name"])
        kind = column["type"]
        values = column["values"]
        if not isinstance(values, list):
            raise TypeError("shadow column values must be a list")
        if kind == "f64":
            result[name] = [
                None if value is None else _f64_from_token(str(value)) for value in values
            ]
        elif kind == "i64":
            result[name] = pd.array(values, dtype="Int64")
        elif kind == "bool":
            result[name] = pd.array(values, dtype="boolean")
        elif kind == "text":
            result[name] = pd.array(values, dtype="string")
        else:
            raise TypeError(f"unknown shadow column type: {kind!r}")
    return pd.DataFrame(result)


def canonical_sha256(document: Mapping[str, object]) -> str:
    """Return the canonical fixture hash without its self-reference."""
    identity = {key: value for key, value in document.items() if key != "fingerprint"}
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _execute_indicator_contract(path: Path, frame: pd.DataFrame) -> pd.DataFrame:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "VectorShadowIndicatorContract"
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == "populate_indicators"
    )
    method = copy.deepcopy(method)
    method.decorator_list = []
    method.returns = None
    for argument in [*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs]:
        argument.annotation = None
    generated = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ClassDef(
                    name="PinnedIndicatorStrategy",
                    bases=[],
                    keywords=[],
                    body=[method],
                    decorator_list=[],
                    type_params=[],
                )
            ],
            type_ignores=[],
        )
    )
    namespace: dict[str, object] = {"np": np, "pd": pd}
    exec(compile(generated, str(path), "exec"), namespace)  # noqa: S102 - committed source oracle
    strategy = namespace["PinnedIndicatorStrategy"]
    if not isinstance(strategy, type):  # pragma: no cover - generated above
        raise TypeError("indicator strategy did not compile")
    output = strategy().populate_indicators(frame.copy(deep=True), {"pair": "ETH/USDT"})
    if not isinstance(output, pd.DataFrame):
        raise TypeError("indicator strategy did not return a DataFrame")
    return output


def _indicator_input() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0, 11.0, 10.0, 13.0, 12.0, 14.0, 13.0, 15.0],
            "close": [11.0, 10.0, 12.0, 12.0, 14.0, 13.0, 15.0, 16.0],
        },
        dtype="float64",
    )


def _encode_series(series: pd.Series) -> tuple[str, list[object]]:
    dtype = str(series.dtype)
    if dtype.startswith("float"):
        return "f64", [_f64_token(float(value)) for value in series]
    if dtype.startswith(("int", "Int")):
        return "i64", [None if pd.isna(value) else int(value) for value in series]
    if dtype in {"bool", "boolean"}:
        return "bool", [None if pd.isna(value) else bool(value) for value in series]
    values = []
    for value in series.astype("object"):
        if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
            values.append(None)
        elif isinstance(value, str):
            values.append(value)
        else:
            raise TypeError(f"shadow text column contains {type(value).__name__}")
    return "text", values


def _f64_token(value: float) -> str:
    return f"0x{np.float64(value).view(np.uint64).item():016x}"


def _f64_from_token(value: str) -> float:
    if len(value) != 18 or not value.startswith("0x"):
        raise ValueError(f"invalid f64 token: {value!r}")
    return np.uint64(int(value[2:], 16)).view(np.float64).item()


def _enabled_indexes(series: pd.Series, start: int) -> list[int]:
    enabled = series.eq(1).fillna(False)
    return [int(index) for index in range(start, len(series)) if bool(enabled.iloc[index])]


def _portable_program(program: dict[str, Any], source: Path) -> dict[str, Any]:
    result = copy.deepcopy(program)
    result["source"]["path"] = str(source)
    if result["schema_version"] == "indicator-program-v1":
        validate_indicator_program(result)
    return result


def _document_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256((_canonical_json(document) + "\n").encode()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON document is not an object: {path}")
    return value


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{description} must be an object")
    return value


def _canonical_json(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
