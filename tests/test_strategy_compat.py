from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine.informative_fixture import FIXTURE_PATH, execute_cases
from nfi_backtest_engine.strategy_compat import (
    merge_informative_pair,
    timeframe_minutes,
    timeframe_seconds,
)
from pandas.testing import assert_frame_equal

ROOT = Path(__file__).resolve().parents[1]


def _frame(dates: list[str], values: list[float], column: str = "close") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates, utc=True),
            column: values,
        }
    )


def test_informative_merge_exposes_slow_candle_only_at_its_close_boundary() -> None:
    base = _frame(
        ["2026-01-01 00:50", "2026-01-01 00:55", "2026-01-01 01:00"],
        [1.0, 2.0, 3.0],
    )
    informative = _frame(["2026-01-01 00:00"], [100.0], column="signal")

    result = merge_informative_pair(base, informative, "5m", "1h", ffill=False)

    assert pd.isna(result.loc[0, "signal_1h"])
    assert result.loc[1, "signal_1h"] == 100.0
    assert pd.isna(result.loc[2, "signal_1h"])


def test_informative_merge_forward_fills_only_historical_values() -> None:
    base = _frame(
        ["2026-01-01 01:00", "2026-01-01 01:05", "2026-01-01 01:55"],
        [1.0, 2.0, 3.0],
    )
    informative = _frame(
        ["2026-01-01 00:00", "2026-01-01 01:00"],
        [100.0, 200.0],
        column="signal",
    )

    result = merge_informative_pair(base, informative, "5m", "1h", ffill=True)

    assert result["signal_1h"].tolist() == [100.0, 100.0, 200.0]


def test_informative_merge_preserves_freqtrade_duplicate_cartesian_behavior() -> None:
    base = _frame(["2026-01-01 00:55", "2026-01-01 00:55"], [1.0, 2.0])
    informative = _frame(
        ["2026-01-01 00:00", "2026-01-01 00:00"],
        [100.0, 200.0],
        column="signal",
    )

    result = merge_informative_pair(base, informative, "5m", "1h", ffill=False)

    assert len(result) == 4
    assert result["signal_1h"].tolist() == [100.0, 200.0, 100.0, 200.0]


def test_informative_merge_supports_suffix_and_month_visibility() -> None:
    base = _frame(["2026-02-28 23:55"], [1.0])
    informative = _frame(["2026-02-01 00:00"], [100.0], column="signal")

    result = merge_informative_pair(
        base,
        informative,
        "5m",
        "1M",
        ffill=False,
        append_timeframe=False,
        suffix="btc",
    )

    expected = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-02-28 23:55"], utc=True),
            "close": [1.0],
            "date_btc": pd.to_datetime(["2026-02-01 00:00"], utc=True),
            "signal_btc": [100.0],
        }
    )
    assert_frame_equal(result, expected)


def test_informative_merge_rejects_faster_frame_and_conflicting_suffix() -> None:
    frame = _frame(["2026-01-01 00:00"], [1.0])

    with pytest.raises(ValueError, match="faster timeframe"):
        merge_informative_pair(frame, frame, "1h", "5m")
    with pytest.raises(ValueError, match="append_timeframe"):
        merge_informative_pair(frame, frame, "5m", "1h", suffix="btc")


def test_timeframe_minutes_uses_freqtrade_ccxt_semantics() -> None:
    assert timeframe_seconds("30s") == 30
    assert timeframe_minutes("30s") == 0
    assert timeframe_minutes("5m") == 5
    assert timeframe_minutes("1h") == 60
    assert timeframe_minutes("1M") == 43_200


def test_compatibility_merge_is_exact_against_pinned_freqtrade_oracle() -> None:
    oracle = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert execute_cases(merge_informative_pair) == oracle["cases"]
