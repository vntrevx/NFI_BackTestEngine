"""Generate the source-boundary proof for upstream X7 Signal 47 protection."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from . import _rust
from .signal_program import compile_signal_program, execute_signal_program
from .strategy_compat import (
    VectorDataProvider,
    load_strategy_class,
    prepare_worker_config,
)
from .tag_program import compile_tag_program, execute_tag_program
from .vector_worker import _advise_signals

FIXTURE_PATH = Path("benchmarks/evidence/m22/latest-x7-signal47-boundary.json")
SCHEMA_VERSION = "latest-x7-signal47-boundary-v1"
STRATEGY_CLASS = "NostalgiaForInfinityX7"
SIGNAL_COLUMNS = ("enter_long", "enter_short", "exit_long", "exit_short")
TAG_COLUMNS = ("enter_tag", "exit_tag")
CHANGED_EXPRESSION = (
    "((rsi_3_4h_gt_20) | (rsi_3_1d_gt_50) | "
    "(aroonu_14_1d_lt_80) | (roc_9_1d_lt_30))"
)

_SCALAR_OVERRIDES = {
    "volume": 1.0,
    "num_empty_288": 0.0,
    "protections_long_global": 1.0,
    "RSI_3": 10.0,
    "RSI_3_15m": 60.0,
    "RSI_3_1h": 60.0,
    "RSI_3_4h": 20.0,
    "RSI_3_1d": 50.0,
    "AROONU_14_1d": 80.0,
    "AROONU_14_15m": 30.0,
    "AROONU_14_1h": 0.0,
    "AROONU_14_4h": 0.0,
    "STOCHRSIk_14_14_3_3_15m": 50.0,
    "STOCHRSIk_14_14_3_3_1h": 50.0,
    "STOCHRSIk_14_14_3_3_4h": 50.0,
    "STOCHRSIk_14_14_3_3_1d": 50.0,
    "RSI_14": 50.0,
    "RSI_14_15m": 40.0,
    "RSI_14_1h": 50.0,
    "RSI_14_4h": 50.0,
    "AROONU_14": 60.0,
    "STOCHRSIk_14_14_3_3": 50.0,
    "OBV_change_pct": 1.0,
    "EMA_26": 1.0,
    "EMA_12_4h": 1.0,
    "EMA_200_4h": 2.0,
    "close": 100.0,
    "close_min_48": 90.0,
    "close_max_48": 110.0,
    "CMF_20_1h": 0.0,
    "CMF_20_4h": 0.0,
    "BBP_20_2.0_4h": 0.5,
    "WILLR_14_1h": -50.0,
    "ROC_9_4h": 0.0,
    "ROC_9_1h": 0.0,
    "BBL_20_2.0": 90.0,
    "BBU_20_2.0": 110.0,
    "BBL_20_2.0_1h": 90.0,
    "BBU_20_2.0_1h": 110.0,
}
_SERIES_OVERRIDES = {
    "EMA_12": [0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0],
}
_CASES = {"new_protection_rejects": 30.0, "one_term_passes": 29.0}


def generate_fixture(
    current_source: Path,
    baseline_source: Path,
    *,
    current_commit: str,
    baseline_commit: str,
) -> dict[str, Any]:
    """Execute both source revisions and both independent program runtimes."""
    _validate_sources(current_source, baseline_source)
    modes = {
        mode: _qualify_mode(current_source, baseline_source, mode)
        for mode in ("spot", "futures")
    }
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": _source_identity(current_source, current_commit),
        "baseline": _source_identity(baseline_source, baseline_commit),
        "strategy_class": STRATEGY_CLASS,
        "changed_route": {
            "side": "long",
            "signal": "47",
            "expression": CHANGED_EXPRESSION,
            "expression_sha256": hashlib.sha256(CHANGED_EXPRESSION.encode()).hexdigest(),
        },
        "source_diff": _source_method_diff(current_source, baseline_source),
        "input_contract": {
            "rows": 8,
            "default_numeric_value": 0.0,
            "scalar_overrides": _SCALAR_OVERRIDES,
            "series_overrides": _SERIES_OVERRIDES,
            "case_roc_9_1d": _CASES,
        },
        "modes": modes,
        "claims": {
            "source_wrapper_python_program_exact": True,
            "source_wrapper_rust_signal_exact": True,
            "source_wrapper_rust_tag_exact": True,
            "spot_and_futures_exact": True,
            "runtime_signal_number_branch_added": False,
        },
    }
    document["fingerprint"] = canonical_sha256(document)
    return document


def write_fixture(
    current_source: Path,
    baseline_source: Path,
    *,
    current_commit: str,
    baseline_commit: str,
    output: Path = FIXTURE_PATH,
) -> None:
    """Regenerate the committed evidence document."""
    document = generate_fixture(
        current_source,
        baseline_source,
        current_commit=current_commit,
        baseline_commit=baseline_commit,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def canonical_sha256(document: Mapping[str, Any]) -> str:
    """Hash a fixture independently of its own fingerprint field."""
    identity = dict(document)
    identity.pop("fingerprint", None)
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _qualify_mode(current: Path, baseline: Path, mode: str) -> dict[str, Any]:
    config = {"max_open_trades": 6, "stake_currency": "USDT", "trading_mode": mode}
    programs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for label, source in (("current", current), ("baseline", baseline)):
        programs[label] = (
            compile_signal_program(
                source,
                class_name=STRATEGY_CLASS,
                trading_mode=mode,
                config=config,
            ),
            compile_tag_program(
                source,
                class_name=STRATEGY_CLASS,
                trading_mode=mode,
                config=config,
            ),
        )
    required = sorted(
        {
            column
            for program_pair in programs.values()
            for program in program_pair
            for column in cast(list[str], program["required_input_columns"])
        }
    )
    cases: dict[str, Any] = {}
    for case_name, roc_9_1d in _CASES.items():
        frame = _boundary_frame(required, roc_9_1d)
        case: dict[str, Any] = {}
        for label, source in (("current", current), ("baseline", baseline)):
            signal_program, tag_program = programs[label]
            source_output = _source_output(source, frame, mode)
            python_signal = execute_signal_program(
                signal_program,
                frame,
                metadata={"pair": _pair(mode)},
            )
            python_tag = execute_tag_program(
                tag_program,
                frame,
                metadata={"pair": _pair(mode)},
            )
            numeric = _numeric_columns(frame)
            rust_signal = _rust.execute_numeric_mutation_program(
                _compact_json(signal_program),
                numeric,
                {"pair": _pair(mode)},
                list(SIGNAL_COLUMNS),
            )
            rust_tag = _rust.execute_numeric_mutation_program(
                _compact_json(tag_program),
                numeric,
                {"pair": _pair(mode)},
                [*SIGNAL_COLUMNS, *TAG_COLUMNS],
            )
            expected = _encode_columns(source_output, (*SIGNAL_COLUMNS, *TAG_COLUMNS))
            _assert_exact(expected, _encode_columns(python_signal, SIGNAL_COLUMNS), SIGNAL_COLUMNS)
            _assert_exact(
                expected,
                _encode_columns(python_tag, (*SIGNAL_COLUMNS, *TAG_COLUMNS)),
                (*SIGNAL_COLUMNS, *TAG_COLUMNS),
            )
            _assert_exact(expected, _bridge_values(rust_signal), SIGNAL_COLUMNS)
            _assert_exact(
                expected,
                _bridge_values(rust_tag),
                (*SIGNAL_COLUMNS, *TAG_COLUMNS),
            )
            case[label] = expected
        cases[case_name] = case
    return {
        "required_input_column_count": len(required),
        "required_input_columns_sha256": hashlib.sha256("\n".join(required).encode()).hexdigest(),
        "programs": {
            label: {
                "signal_fingerprint": pair[0]["fingerprint"],
                "signal_node_count": len(cast(list[Any], pair[0]["nodes"])),
                "tag_fingerprint": pair[1]["fingerprint"],
                "tag_node_count": len(cast(list[Any], pair[1]["nodes"])),
            }
            for label, pair in programs.items()
        },
        "cases": cases,
    }


def _boundary_frame(required: list[str], roc_9_1d: float) -> pd.DataFrame:
    rows = 8
    frame = pd.DataFrame(
        {name: np.zeros(rows, dtype=np.float64) for name in required}
    )
    for name, value in _SCALAR_OVERRIDES.items():
        frame[name] = value
    frame["ROC_9_1d"] = roc_9_1d
    for name, values in _SERIES_OVERRIDES.items():
        frame[name] = np.asarray(values, dtype=np.float64)
    return frame


def _source_output(source: Path, frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    strategy_class = load_strategy_class(source, STRATEGY_CLASS)
    config = prepare_worker_config(
        {
            "exchange": {"name": "binance"},
            "max_open_trades": 6,
            "stake_currency": "USDT",
            "trading_mode": mode,
        },
        user_data_directory=Path(".nfi/signal47-fixture-worker"),
    )
    strategy = strategy_class(config)
    strategy_runtime = cast(Any, strategy)
    pair = _pair(mode)
    strategy_runtime.dp = VectorDataProvider({}, [pair])
    strategy_runtime.long_entry_signal_params = {"long_entry_condition_47_enable": True}
    strategy_runtime.short_entry_signal_params = {}
    return _advise_signals(strategy_runtime, frame.copy(deep=True), {"pair": pair})


def _source_identity(source: Path, commit: str) -> dict[str, str]:
    return {
        "commit": commit,
        "strategy_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def _validate_sources(current: Path, baseline: Path) -> None:
    current_text = current.read_text(encoding="utf-8")
    baseline_text = baseline.read_text(encoding="utf-8")
    if CHANGED_EXPRESSION not in current_text or CHANGED_EXPRESSION in baseline_text:
        raise ValueError("the supplied sources do not isolate the Signal 47 protection change")


def _source_method_diff(current: Path, baseline: Path) -> dict[str, Any]:
    current_methods = _method_inventory(current)
    baseline_methods = _method_inventory(baseline)
    if set(current_methods) != set(baseline_methods):
        raise ValueError("the source revisions expose different method inventories")
    changed = sorted(
        name
        for name in current_methods
        if current_methods[name] != baseline_methods[name]
    )
    if changed != ["populate_entry_trend", "version"]:
        raise ValueError(f"the source revisions changed unexpected methods: {changed}")
    unchanged = {
        name: current_methods[name] for name in sorted(current_methods) if name not in changed
    }
    return {
        "changed_methods": {
            name: {
                "baseline_ast_sha256": baseline_methods[name],
                "current_ast_sha256": current_methods[name],
            }
            for name in changed
        },
        "unchanged_method_count": len(unchanged),
        "unchanged_methods_fingerprint": hashlib.sha256(
            _compact_json(unchanged).encode()
        ).hexdigest(),
        "callback_and_stateful_methods_changed": False,
    }


def _method_inventory(source: Path) -> dict[str, str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    methods: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            normalized = ast.dump(node, include_attributes=False)
            methods[node.name] = hashlib.sha256(normalized.encode()).hexdigest()
    return methods


def _pair(mode: str) -> str:
    return "TEST/USDT:USDT" if mode == "futures" else "TEST/USDT"


def _numeric_columns(frame: pd.DataFrame) -> dict[str, list[float | None]]:
    return {
        str(name): [None if pd.isna(value) else float(value) for value in frame[name]]
        for name in frame.columns
    }


def _encode_columns(frame: pd.DataFrame, names: tuple[str, ...]) -> dict[str, list[Any]]:
    return {
        name: [
            None
            if pd.isna(value)
            else value.item()
            if hasattr(value, "item")
            else value
            for value in frame[name]
        ]
        for name in names
    }


def _bridge_values(output: Mapping[str, Any]) -> dict[str, list[Any]]:
    return {
        name: cast(list[Any], cast(Mapping[str, Any], value)["values"])
        for name, value in output.items()
    }


def _assert_exact(
    expected: Mapping[str, list[Any]],
    actual: Mapping[str, list[Any]],
    names: tuple[str, ...],
) -> None:
    for name in names:
        if actual[name] != expected[name]:
            raise ValueError(f"Signal 47 qualification differs for {name}")


def _compact_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
