"""Generate deterministic evidence from the pinned Freqtrade informative merge helper."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import struct
import sys
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import ccxt
import numpy as np
import pandas as pd

PINNED_SOURCE = Path(".nfi/roadmap-acceptance/M20-05/freqtrade-2026.5.1")
FIXTURE_PATH = Path("benchmarks/reference/informative/freqtrade-2026.5.1.json")
_Merge = Callable[..., pd.DataFrame]


def generate_fixture(source_root: Path | None = None) -> dict[str, object]:
    """Execute the official helper for every compact informative-merge case."""
    root = source_root or _repository_root() / PINNED_SOURCE
    helper = root / "freqtrade/strategy/strategy_helper.py"
    merge = _load_official_merge(helper)
    cases = execute_cases(merge)
    fixture: dict[str, object] = {
        "schema_version": "freqtrade-informative-fixture-v1",
        "source": {
            "version": _source_version(root),
            "commit": _source_commit(root),
            "strategy_helper_sha256": _sha256_file(helper),
            "strategy_helper": "freqtrade/strategy/strategy_helper.py",
            "timeframe_to_minutes": "ccxt.Exchange.parse_timeframe(timeframe) // 60",
        },
        "cases": cases,
    }
    fixture["fingerprint"] = canonical_sha256(fixture)
    return fixture


def execute_cases(merge: _Merge) -> list[dict[str, object]]:
    """Run the canonical case matrix against one merge implementation."""
    return [_execute_case(merge, spec) for spec in _case_specs()]


def write_fixture(destination: Path, source_root: Path | None = None) -> dict[str, object]:
    """Generate and persist canonical fixture evidence."""
    fixture = generate_fixture(source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(fixture) + "\n", encoding="utf-8")
    return fixture


def canonical_sha256(document: Mapping[str, object]) -> str:
    """Hash a fixture while excluding its self-referential fingerprint."""
    identity = {key: value for key, value in document.items() if key != "fingerprint"}
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _execute_case(merge: _Merge, spec: Mapping[str, object]) -> dict[str, object]:
    call = cast(dict[str, object], spec["call"])
    date_column = str(call.get("date_column", "date"))
    base = _frame(spec["base"], date_columns=("date",))
    informative = _frame(spec["informative"], date_columns=(date_column,))
    result: dict[str, object] = {
        "name": spec["name"],
        "base_pair": spec["base_pair"],
        "informative_pair": spec["informative_pair"],
        "call": spec["call"],
        "base": _encode_frame(base),
        "informative": _encode_frame(informative),
    }
    try:
        output = merge(base, informative, **call)
    except Exception as exc:  # The pinned helper's errors are part of the oracle.
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    else:
        result["output"] = _encode_frame(output)
    return result


def _case_specs() -> list[dict[str, object]]:
    base_hour = _rows(
        ("date", "base"),
        (
            ("2024-01-01T00:45:00Z", 45.0),
            ("2024-01-01T00:50:00Z", 50.0),
            ("2024-01-01T00:55:00Z", 55.0),
            ("2024-01-01T01:00:00Z", 60.0),
        ),
    )
    informative_hour = _rows(("date", "info"), (("2024-01-01T00:00:00Z", 10.5),))
    return [
        _case("boundary_ffill_false", base_hour, informative_hour, "5m", "1h", ffill=False),
        _case("boundary_ffill_true", base_hour, informative_hour, "5m", "1h", ffill=True),
        _case(
            "equal_timeframe",
            _rows(("date", "base"), (("2024-01-01T00:00:00Z", 0.0), ("2024-01-01T00:05:00Z", 5.0))),
            _rows(("date", "same"), (("2024-01-01T00:05:00Z", 7.25),)),
            "5m",
            "5m",
            ffill=False,
        ),
        _case(
            "empty_informative",
            base_hour,
            _rows(("date", "info"), ()),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "missing_informative_rows",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T00:55:00Z", 55.0),
                    ("2024-01-01T01:00:00Z", 60.0),
                    ("2024-01-01T01:55:00Z", 115.0),
                ),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T00:00:00Z", 10.0), ("2024-01-01T02:00:00Z", 20.0)),
            ),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "duplicate_cartesian",
            _rows(
                ("date", "base"),
                (("2024-01-01T00:55:00Z", 1.0), ("2024-01-01T00:55:00Z", 2.0)),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T00:00:00Z", 10.0), ("2024-01-01T00:00:00Z", 20.0)),
            ),
            "5m",
            "1h",
            ffill=False,
        ),
        _case(
            "duplicate_cartesian_ffill_true",
            _rows(
                ("date", "base"),
                (("2024-01-01T00:55:00Z", 1.0), ("2024-01-01T00:55:00Z", 2.0)),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T00:00:00Z", 10.0), ("2024-01-01T00:00:00Z", 20.0)),
            ),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "leading_repair",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T01:00:00Z", 60.0),
                    ("2024-01-01T01:05:00Z", 65.0),
                    ("2024-01-01T01:55:00Z", 115.0),
                ),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T00:00:00Z", 10.0), ("2024-01-01T01:00:00Z", 20.0)),
            ),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "leading_repair_uses_last_historical_row",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T02:00:00Z", 120.0),
                    ("2024-01-01T02:05:00Z", 125.0),
                    ("2024-01-01T02:10:00Z", 130.0),
                    ("2024-01-01T02:55:00Z", 175.0),
                ),
            ),
            _rows(
                ("date", "info", "row_marker"),
                (
                    ("2024-01-01T00:00:00Z", 10.0, 100.0),
                    ("2024-01-01T01:00:00Z", 20.0, 200.0),
                    ("2024-01-01T02:00:00Z", 30.0, 300.0),
                ),
            ),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "unsorted_ffill_false",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T01:00:00Z", 60.0),
                    ("2024-01-01T00:55:00Z", 55.0),
                    ("2024-01-01T00:50:00Z", 50.0),
                ),
            ),
            informative_hour,
            "5m",
            "1h",
            ffill=False,
        ),
        _case(
            "unsorted_ffill_true",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T01:00:00Z", 60.0),
                    ("2024-01-01T00:55:00Z", 55.0),
                    ("2024-01-01T00:50:00Z", 50.0),
                ),
            ),
            informative_hour,
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "unsorted_informative_ffill_false",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T00:55:00Z", 55.0),
                    ("2024-01-01T01:55:00Z", 115.0),
                ),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T01:00:00Z", 20.0), ("2024-01-01T00:00:00Z", 10.0)),
            ),
            "5m",
            "1h",
            ffill=False,
        ),
        _case(
            "unsorted_informative_ffill_true",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-01T00:55:00Z", 55.0),
                    ("2024-01-01T01:00:00Z", 60.0),
                    ("2024-01-01T01:55:00Z", 115.0),
                ),
            ),
            _rows(
                ("date", "info"),
                (("2024-01-01T01:00:00Z", 20.0), ("2024-01-01T00:00:00Z", 10.0)),
            ),
            "5m",
            "1h",
            ffill=True,
        ),
        _case(
            "custom_date_column",
            _rows(
                ("date", "base"),
                (("2024-01-01T00:50:00Z", 50.0), ("2024-01-01T00:55:00Z", 55.0)),
            ),
            _rows(("candle_open", "custom_info"), (("2024-01-01T00:00:00Z", 91.0),)),
            "5m",
            "1h",
            ffill=False,
            date_column="candle_open",
        ),
        _case(
            "suffix_naming",
            _rows(("date", "base"), (("2024-01-01T00:55:00Z", 1.0),)),
            _rows(("date", "signal"), (("2024-01-01T00:00:00Z", 88.0),)),
            "5m",
            "1h",
            ffill=False,
            append_timeframe=False,
            suffix="btc",
        ),
        _case(
            "cross_pair_sentinel",
            _rows(("date", "base"), (("2024-01-01T00:55:00Z", 1.0),)),
            _rows(("date", "btc_sentinel"), (("2024-01-01T00:00:00Z", 4242.5),)),
            "5m",
            "1h",
            ffill=False,
            base_pair="ETH/USDT",
            informative_pair="BTC/USDT",
        ),
        _case(
            "f64_and_pandas_null_encoding",
            _rows(
                (
                    "date",
                    "float_nan",
                    "positive_infinity",
                    "negative_infinity",
                    "positive_zero",
                    "negative_zero",
                    "python_none",
                    "pandas_na",
                ),
                (
                    (
                        "2024-01-01T00:00:00Z",
                        float("nan"),
                        float("inf"),
                        float("-inf"),
                        0.0,
                        -0.0,
                        None,
                        pd.NA,
                    ),
                ),
            ),
            _rows(("date", "info"), (("2024-01-01T00:00:00Z", 1.0),)),
            "5m",
            "5m",
            ffill=False,
        ),
        _case(
            "informative_f64_and_pandas_null_encoding",
            _rows(
                ("date", "base"),
                (
                    ("2023-12-31T23:55:00Z", 23.0),
                    ("2024-01-01T00:00:00Z", 24.0),
                ),
            ),
            _rows(
                (
                    "date",
                    "info_float_nan",
                    "info_positive_infinity",
                    "info_negative_infinity",
                    "info_positive_zero",
                    "info_negative_zero",
                    "info_python_none",
                    "info_pandas_na",
                ),
                (
                    (
                        "2024-01-01T00:00:00Z",
                        float("nan"),
                        float("inf"),
                        float("-inf"),
                        0.0,
                        -0.0,
                        None,
                        pd.NA,
                    ),
                ),
            ),
            "5m",
            "5m",
            ffill=False,
        ),
        _case(
            "faster_timeframe_failure",
            _rows(("date", "base"), (("2024-01-01T00:00:00Z", 1.0),)),
            _rows(("date", "info"), (("2024-01-01T00:00:00Z", 1.0),)),
            "1h",
            "5m",
            ffill=False,
        ),
        _case(
            "month_boundary",
            _rows(
                ("date", "base"),
                (
                    ("2024-01-31T23:50:00Z", 50.0),
                    ("2024-01-31T23:55:00Z", 55.0),
                    ("2024-02-01T00:00:00Z", 0.0),
                ),
            ),
            _rows(("date", "monthly"), (("2024-01-01T00:00:00Z", 31.0),)),
            "5m",
            "1M",
            ffill=False,
        ),
    ]


def _case(
    name: str,
    base: dict[str, list[object]],
    informative: dict[str, list[object]],
    timeframe: str,
    informative_timeframe: str,
    *,
    ffill: bool,
    append_timeframe: bool = True,
    suffix: str | None = None,
    date_column: str | None = None,
    base_pair: str = "ETH/USDT",
    informative_pair: str = "ETH/USDT",
) -> dict[str, object]:
    call: dict[str, object] = {
        "timeframe": timeframe,
        "timeframe_inf": informative_timeframe,
        "ffill": ffill,
        "append_timeframe": append_timeframe,
    }
    if suffix is not None:
        call["suffix"] = suffix
    if date_column is not None:
        call["date_column"] = date_column
    return {
        "name": name,
        "base_pair": base_pair,
        "informative_pair": informative_pair,
        "call": call,
        "base": base,
        "informative": informative,
    }


def _rows(
    columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]
) -> dict[str, list[object]]:
    return {column: [row[index] for row in rows] for index, column in enumerate(columns)}


def _frame(columns: object, *, date_columns: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(columns, dict):
        raise TypeError("fixture frame columns must be a dictionary")
    frame = pd.DataFrame(columns)
    for column in date_columns:
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame


def _encode_frame(frame: pd.DataFrame) -> dict[str, object]:
    return {
        "columns": list(frame.columns),
        "rows": [
            [_encode_value(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }


def _encode_value(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return _timestamp(value)
    if isinstance(value, np.floating | float):
        number = float(value)
        if math.isnan(number):
            return "f64:0x7ff8000000000000"
        return f"f64:0x{struct.pack('>d', number).hex()}"
    if isinstance(value, np.integer):
        return int(value)
    return value


def _timestamp(value: pd.Timestamp) -> str:
    value = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
    return value.isoformat().replace("+00:00", "Z")


def _load_official_merge(helper: Path) -> _Merge:
    if not helper.is_file():
        raise FileNotFoundError(f"pinned Freqtrade strategy helper is missing: {helper}")
    module_name = "_nfi_pinned_freqtrade_strategy_helper"
    module_names = ("freqtrade", "freqtrade.exchange", module_name)
    saved = {name: sys.modules.get(name) for name in module_names}
    freqtrade = types.ModuleType("freqtrade")
    exchange = types.ModuleType("freqtrade.exchange")
    exchange.timeframe_to_minutes = _official_timeframe_to_minutes  # type: ignore[attr-defined]
    freqtrade.exchange = exchange  # type: ignore[attr-defined]
    sys.modules["freqtrade"] = freqtrade
    sys.modules["freqtrade.exchange"] = exchange
    try:
        spec = importlib.util.spec_from_file_location(module_name, helper)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load pinned Freqtrade helper: {helper}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        merge = getattr(module, "merge_informative_pair", None)
        if not callable(merge):
            raise ImportError("pinned Freqtrade helper has no merge_informative_pair")
        return cast(_Merge, merge)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _official_timeframe_to_minutes(timeframe: str) -> int:
    return ccxt.Exchange.parse_timeframe(timeframe) // 60


def _source_commit(root: Path) -> str:
    head = root / ".git/HEAD"
    if not head.is_file():
        raise FileNotFoundError(f"pinned Freqtrade commit metadata is missing: {head}")
    reference = head.read_text(encoding="utf-8").strip()
    if not reference.startswith("ref: "):
        return reference
    ref_path = root / ".git" / reference.removeprefix("ref: ")
    if not ref_path.is_file():
        raise FileNotFoundError(f"pinned Freqtrade ref is missing: {ref_path}")
    return ref_path.read_text(encoding="utf-8").strip()


def _source_version(root: Path) -> str:
    version_file = root / "freqtrade/__init__.py"
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__ = "):
            return line.split('"', maxsplit=2)[1]
    raise ValueError(f"pinned Freqtrade version is missing: {version_file}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
