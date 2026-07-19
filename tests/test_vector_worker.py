from __future__ import annotations

from pathlib import Path

import pandas as pd
from nfi_backtest_engine.runtime_versions import vector_dependency_versions
from nfi_backtest_engine.vector_worker import (
    _attach_funding_events,
    _bound_indicator_frames,
    _prepare_execution_frame,
    _trim_timerange,
)


def test_vector_dependency_identity_covers_dataframe_runtime() -> None:
    versions = vector_dependency_versions()

    assert set(versions) == {"python", "numpy", "pandas", "pyarrow", "ta_lib"}
    assert all(value for value in versions.values())


def test_indicator_frames_use_freqtrade_timeframe_specific_startup_windows() -> None:
    dates = pd.date_range("2024-01-01T23:00:00Z", periods=19, freq="5min")
    base = pd.DataFrame({"date": dates, "close": range(len(dates))})
    informative = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01T22:00:00Z", periods=11, freq="15min"),
            "close": range(11),
        }
    )

    bounded = _bound_indicator_frames(
        {
            ("APE/USDT", "5m"): base,
            ("APE/USDT", "15m"): informative,
        },
        "1704153600-1704155400",
        startup_candles=2,
    )

    assert bounded[("APE/USDT", "5m")]["date"].tolist() == list(
        pd.date_range("2024-01-01T23:50:00Z", "2024-01-02T00:30:00Z", freq="5min")
    )
    assert bounded[("APE/USDT", "15m")]["date"].tolist() == list(
        pd.date_range("2024-01-01T23:30:00Z", "2024-01-02T00:30:00Z", freq="15min")
    )


def test_trim_timerange_keeps_freqtrade_stop_boundary() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2024-01-01 23:55:00+00:00",
                    "2024-01-02 00:00:00+00:00",
                    "2024-01-02 00:05:00+00:00",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0],
        }
    )

    selected = _trim_timerange(
        frame,
        "20240101-20240102",
        startup_candles=0,
    )

    assert selected["date"].tolist() == [
        pd.Timestamp("2024-01-01 23:55:00+00:00"),
        pd.Timestamp("2024-01-02 00:00:00+00:00"),
    ]


def test_trim_timerange_accepts_freqtrade_unix_second_boundaries() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2022-11-09T22:00:00Z",
                periods=5,
                freq="30min",
            ),
            "open": [1.0] * 5,
        }
    )

    selected = _trim_timerange(
        frame,
        "1668033000-1668036600",
        startup_candles=0,
    )

    assert selected["date"].tolist() == [
        pd.Timestamp("2022-11-09T22:30:00Z"),
        pd.Timestamp("2022-11-09T23:00:00Z"),
        pd.Timestamp("2022-11-09T23:30:00Z"),
    ]


def test_execution_shift_drops_the_pre_start_decision_like_freqtrade() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2022-04-30T23:05:00Z",
                periods=3,
                freq="5min",
            ),
            "enter_long": [1, 0, 0],
            "enter_tag": ["141", None, None],
        }
    )

    prepared = _prepare_execution_frame(
        frame,
        "1651360200-1651360500",
        startup_candles=0,
    )

    assert prepared.execution_start_index == 1
    assert prepared.frame["date"].tolist() == [
        pd.Timestamp("2022-04-30T23:10:00Z"),
        pd.Timestamp("2022-04-30T23:15:00Z"),
    ]
    # Freqtrade trims first, shifts second, and drops the first trimmed row.
    # The pre-start signal can remain in the context row but is never traded.
    assert prepared.frame["nfi_exec_enter_long"].tolist() == [1, 0]
    assert prepared.frame["nfi_exec_enter_tag"].tolist() == ["141", None]


def test_execution_frame_keeps_startup_rows_as_callback_only_context() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2022-04-30T22:55:00Z",
                periods=5,
                freq="5min",
            ),
            "enter_long": [1, 0, 0, 1, 0],
            "enter_tag": ["context-only", None, None, "141", None],
            "feature": [10.0, 11.0, 12.0, 13.0, 14.0],
        }
    )

    prepared = _prepare_execution_frame(
        frame,
        "1651360200-1651360500",
        startup_candles=3,
    )

    assert prepared.execution_start_index == 4
    assert prepared.frame["date"].tolist() == frame["date"].tolist()
    # The prefix may contain shifted signals, but the Rust cursor starts at
    # index 3 so those rows can only serve callback feature lookups.
    assert prepared.frame["nfi_exec_enter_tag"].tolist() == [
        None,
        "context-only",
        None,
        None,
        "141",
    ]


def test_funding_events_use_the_exact_inner_join_without_forward_fill(
    tmp_path: Path,
) -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2022-06-09T23:55:00Z", periods=4, freq="5min"),
            "open": [5.73, 5.72, 5.7, 5.68],
        }
    )
    funding = pd.DataFrame(
        {
            "date": [pd.Timestamp("2022-06-10T00:00:00Z")],
            "open": [0.00002067],
            "high": [0.0],
            "low": [0.0],
            "close": [0.0],
            "volume": [0.0],
        }
    )
    mark = pd.DataFrame(
        {
            "date": [
                pd.Timestamp("2022-06-09T23:00:00Z"),
                pd.Timestamp("2022-06-10T00:00:00Z"),
            ],
            "open": [5.734179, 5.721],
            "high": [5.74, 5.74],
            "low": [5.68, 5.52],
            "close": [5.721, 5.60],
            "volume": [0.0, 0.0],
        }
    )
    funding_path = tmp_path / "APE-1h-funding_rate.feather"
    mark_path = tmp_path / "APE-1h-mark.feather"
    funding.to_feather(funding_path)
    mark.to_feather(mark_path)

    result = _attach_funding_events(
        frame,
        {
            "funding_rate_path": str(funding_path),
            "funding_rate_sha256": "a" * 64,
            "mark_path": str(mark_path),
            "mark_sha256": "b" * 64,
        },
    )

    assert result["nfi_exec_funding_rate"].isna().tolist() == [True, False, True, True]
    assert result["nfi_exec_funding_mark_price"].isna().tolist() == [
        True,
        False,
        True,
        True,
    ]
    assert result.loc[1, "nfi_exec_funding_rate"] == 0.00002067
    assert result.loc[1, "nfi_exec_funding_mark_price"] == 5.721
    assert (
        1691
        * result.loc[1, "nfi_exec_funding_rate"]
        * result.loc[1, "nfi_exec_funding_mark_price"]
        == 0.19996594137
    )
