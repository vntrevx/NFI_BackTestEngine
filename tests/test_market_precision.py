from __future__ import annotations

import pandas as pd
import pytest
from nfi_backtest_engine import market_precision
from nfi_backtest_engine.market_precision import historic_price_steps


def test_historic_price_steps_emit_only_monthly_changes() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2022-04-01T00:00:00Z",
                    "2022-04-01T00:05:00Z",
                    "2022-05-01T00:00:00Z",
                    "2022-09-01T00:00:00Z",
                ]
            ),
            "open": [15.7849, 15.8505, 16.1234, 4.212],
            "high": [15.9279, 15.8779, 16.2345, 4.222],
            "low": [15.7600, 15.7593, 16.0123, 4.149],
            "close": [15.8472, 15.8705, 16.1111, 4.151],
        }
    )

    assert historic_price_steps(frame) == [
        {"timestamp_ms": 1_648_771_200_000, "step": 0.0001},
        {"timestamp_ms": 1_661_990_400_000, "step": 0.001},
    ]


def test_historic_price_steps_require_ohlc_and_date() -> None:
    frame = pd.DataFrame({"date": pd.to_datetime(["2022-04-01T00:00:00Z"])})

    with pytest.raises(ValueError, match="close, high, low, open"):
        historic_price_steps(frame)


def test_historic_price_steps_formats_each_months_distinct_prices_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2022-04-01T00:00:00Z",
                    "2022-04-01T00:05:00Z",
                    "2022-05-01T00:00:00Z",
                ]
            ),
            "open": [1.25, 1.25, 1.25],
            "high": [1.5, 1.5, 1.5],
            "low": [1.0, 1.0, 1.0],
            "close": [1.25, 1.25, 1.25],
        }
    )
    original = market_precision._fractional_digit_count
    formatted: list[float] = []

    def counted(value: object) -> float:
        formatted.append(float(value))
        return original(value)

    monkeypatch.setattr(market_precision, "_fractional_digit_count", counted)

    assert historic_price_steps(frame) == [
        {"timestamp_ms": 1_648_771_200_000, "step": 0.01}
    ]
    # Three prices are distinct inside each month. Values repeated across candles
    # and OHLC columns do not re-enter the Python formatting path.
    assert len(formatted) == 6
