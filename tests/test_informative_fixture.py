from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine.informative_fixture import (  # isort: skip
    FIXTURE_PATH,
    canonical_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def test_committed_fixture_has_pinned_source_identity_and_valid_fingerprint() -> None:
    stored = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert stored["fingerprint"] == canonical_sha256(stored)
    assert stored["source"] == {
        "version": "2026.5.1",
        "commit": "6fa470939cc74bf0672e0e348a4d9b293072e43c",
        "strategy_helper_sha256": (
            "46a15179738d83a39148ac96f5ee2f2d50c4514332d059c4458a9d7d3d0e4812"
        ),
        "strategy_helper": "freqtrade/strategy/strategy_helper.py",
        "timeframe_to_minutes": "ccxt.Exchange.parse_timeframe(timeframe) // 60",
    }


def test_fixture_covers_the_compact_informative_contract() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    cases = {str(case["name"]): case for case in fixture["cases"]}

    assert {
        "boundary_ffill_false",
        "boundary_ffill_true",
        "equal_timeframe",
        "empty_informative",
        "missing_informative_rows",
        "duplicate_cartesian",
        "duplicate_cartesian_ffill_true",
        "leading_repair",
        "leading_repair_uses_last_historical_row",
        "unsorted_ffill_false",
        "unsorted_ffill_true",
        "unsorted_informative_ffill_false",
        "unsorted_informative_ffill_true",
        "custom_date_column",
        "suffix_naming",
        "cross_pair_sentinel",
        "f64_and_pandas_null_encoding",
        "informative_f64_and_pandas_null_encoding",
        "faster_timeframe_failure",
        "month_boundary",
    } == cases.keys()
    assert cases["faster_timeframe_failure"]["error"]["type"] == "ValueError"

    duplicate_output = cases["duplicate_cartesian_ffill_true"]["output"]
    assert len(duplicate_output["rows"]) == 4

    leading_output = cases["leading_repair"]["output"]
    assert [row[3] for row in leading_output["rows"]] == [
        "f64:0x4024000000000000",
        "f64:0x4024000000000000",
        "f64:0x4034000000000000",
    ]

    multi_history = cases["leading_repair_uses_last_historical_row"]["output"]
    multi_history_rows = [
        dict(zip(multi_history["columns"], row, strict=True)) for row in multi_history["rows"]
    ]
    historical_row = {
        "date_1h": "2024-01-01T01:00:00Z",
        "info_1h": "f64:0x4034000000000000",
        "row_marker_1h": "f64:0x4069000000000000",
    }
    assert [
        {column: row[column] for column in historical_row} for row in multi_history_rows[:3]
    ] == [historical_row, historical_row, historical_row]
    assert {
        column: multi_history_rows[3][column] for column in historical_row
    } == {
        "date_1h": "2024-01-01T02:00:00Z",
        "info_1h": "f64:0x403e000000000000",
        "row_marker_1h": "f64:0x4072c00000000000",
    }

    unsorted = cases["unsorted_informative_ffill_true"]
    assert [row[1] for row in unsorted["informative"]["rows"]] == [
        "f64:0x4034000000000000",
        "f64:0x4024000000000000",
    ]
    assert [row[3] for row in unsorted["output"]["rows"]] == [
        "f64:0x4024000000000000",
        "f64:0x4024000000000000",
        "f64:0x4034000000000000",
    ]

    custom_date = cases["custom_date_column"]
    assert custom_date["call"]["date_column"] == "candle_open"
    assert custom_date["output"]["columns"] == [
        "date",
        "base",
        "candle_open_1h",
        "custom_info_1h",
    ]

    cross_pair = cases["cross_pair_sentinel"]
    assert cross_pair["base_pair"] != cross_pair["informative_pair"]
    assert cross_pair["output"]["rows"][0][3] == "f64:0x40b0928000000000"


def test_fixture_distinguishes_ieee_floats_from_pandas_nulls() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    cases = {str(case["name"]): case for case in fixture["cases"]}
    encoded = cases["f64_and_pandas_null_encoding"]
    columns = encoded["base"]["columns"]
    row = dict(zip(columns, encoded["base"]["rows"][0], strict=True))

    assert row == {
        "date": "2024-01-01T00:00:00Z",
        "float_nan": "f64:0x7ff8000000000000",
        "positive_infinity": "f64:0x7ff0000000000000",
        "negative_infinity": "f64:0xfff0000000000000",
        "positive_zero": "f64:0x0000000000000000",
        "negative_zero": "f64:0x8000000000000000",
        "python_none": None,
        "pandas_na": None,
    }

    raw = (ROOT / FIXTURE_PATH).read_text(encoding="utf-8")
    assert ":NaN" not in raw
    assert ":Infinity" not in raw


def test_fixture_preserves_informative_special_values_on_exact_and_unmatched_rows() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    cases = {str(case["name"]): case for case in fixture["cases"]}
    encoded = cases["informative_f64_and_pandas_null_encoding"]
    columns = encoded["output"]["columns"]
    unmatched = dict(zip(columns, encoded["output"]["rows"][0], strict=True))
    exact = dict(zip(columns, encoded["output"]["rows"][1], strict=True))

    informative_columns = [column for column in columns if column.startswith("info_")]
    assert unmatched["date_5m"] is None
    assert {unmatched[column] for column in informative_columns} == {
        "f64:0x7ff8000000000000"
    }
    assert exact == {
        "date": "2024-01-01T00:00:00Z",
        "base": "f64:0x4038000000000000",
        "date_5m": "2024-01-01T00:00:00Z",
        "info_float_nan_5m": "f64:0x7ff8000000000000",
        "info_positive_infinity_5m": "f64:0x7ff0000000000000",
        "info_negative_infinity_5m": "f64:0xfff0000000000000",
        "info_positive_zero_5m": "f64:0x0000000000000000",
        "info_negative_zero_5m": "f64:0x8000000000000000",
        "info_python_none_5m": None,
        "info_pandas_na_5m": None,
    }
