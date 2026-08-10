"""Pinned Freqtrade calendar-frequency regression coverage."""

from __future__ import annotations

import pandas as pd
import pytest
from nfi_backtest_engine.vector_worker import (
    _clean_ohlcv_like_freqtrade,
    _freqtrade_resample_frequency,
)


def _ohlcv_frame(dates: list[str]) -> pd.DataFrame:
    values = list(range(10, 10 + len(dates)))
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": values,
            "high": [value + 1 for value in values],
            "low": [value - 1 for value in values],
            "close": values,
            "volume": values,
        }
    )


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        ("1s", "1s"),
        ("30s", "30s"),
        ("2w", "1W-MON"),
        ("1M", "1MS"),
        ("2M", "2MS"),
        ("1y", "1YS"),
    ],
)
def test_resample_frequency_matches_pinned_freqtrade_calendar_anchors(
    timeframe: str,
    expected: str,
) -> None:
    assert _freqtrade_resample_frequency(timeframe) == expected


def test_weekly_cleanup_uses_monday_anchors_for_multiweek_timeframes() -> None:
    result = _clean_ohlcv_like_freqtrade(
        _ohlcv_frame(
            [
                "2024-01-01T00:00:00Z",
                "2024-01-08T00:00:00Z",
                "2024-01-15T00:00:00Z",
            ]
        ),
        pair="GENERIC/USDT",
        timeframe="2w",
    )

    assert result["date"].tolist() == list(
        pd.to_datetime(
            [
                "2024-01-01T00:00:00Z",
                "2024-01-08T00:00:00Z",
                "2024-01-15T00:00:00Z",
            ]
        )
    )


@pytest.mark.parametrize(
    ("timeframe", "dates", "expected_dates"),
    [
        (
            "1M",
            ["2024-01-15T00:00:00Z", "2024-03-15T00:00:00Z"],
            ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z"],
        ),
        (
            "2M",
            ["2024-01-15T00:00:00Z", "2024-03-15T00:00:00Z"],
            ["2024-01-01T00:00:00Z", "2024-03-01T00:00:00Z"],
        ),
        (
            "1y",
            ["2024-01-15T00:00:00Z", "2025-01-15T00:00:00Z"],
            ["2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z"],
        ),
    ],
)
def test_calendar_cleanup_uses_period_start_and_keeps_gap_and_final_candle(
    timeframe: str,
    dates: list[str],
    expected_dates: list[str],
) -> None:
    result = _clean_ohlcv_like_freqtrade(
        _ohlcv_frame(dates),
        pair="GENERIC/USDT",
        timeframe=timeframe,
    )

    assert result["date"].tolist() == list(pd.to_datetime(expected_dates))
    assert result.iloc[-1]["close"] == 11
    if timeframe == "1M":
        assert result.loc[1, ["open", "high", "low", "close", "volume"]].tolist() == [
            10,
            10,
            10,
            10,
            0,
        ]
