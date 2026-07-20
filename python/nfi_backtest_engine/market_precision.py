"""Historical exchange precision derived with Freqtrade's OHLCV rule."""

from __future__ import annotations

import math
import re
from typing import Any, cast

import numpy as np
import pandas as pd

_PRICE_COLUMNS = ("open", "high", "low", "close")
_FRACTIONAL_SIGNIFICANT_DIGITS = re.compile(r"\.(\d*[1-9])")


def historic_price_steps(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Return sparse monthly tick-size changes for one analyzed pair.

    Freqtrade derives historical price precision from the maximum number of
    meaningful fractional digits observed in each calendar month. The market's
    current tick size is not sufficient for old candles: APE/USDT, for example,
    changed from four to three fractional digits during the reference year.

    The first month is always emitted. Later records are emitted only when the
    step changes, keeping the simulator input small and lookup cache-friendly.
    """
    missing = {"date", *_PRICE_COLUMNS} - set(frame.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"historical price precision requires columns: {names}")
    date_column = _series_column(frame, "date")
    dates = cast(pd.Series, pd.to_datetime(date_column, utc=True))
    prices = np.column_stack(
        [_series_column(frame, column).to_numpy(copy=False) for column in _PRICE_COLUMNS]
    )
    month_keys = pd.DataFrame(
        {
            "year": dates.dt.year,
            "month": dates.dt.month,
        }
    )

    changes: list[dict[str, Any]] = []
    previous_step: float | None = None
    groups = month_keys.groupby(
        ["year", "month"],
        sort=True,
        observed=True,
    ).indices
    for month_key, row_indices in groups.items():
        if not isinstance(month_key, tuple) or len(month_key) != 2:
            raise ValueError("historical price precision produced an invalid month key")
        year, month = month_key
        # The monthly maximum over row-wise OHLC maxima is the maximum over all
        # OHLC values in that month. Exchange candles repeat prices heavily, so
        # formatting each distinct value once preserves Freqtrade's exact NumPy
        # string rule while avoiding four Python calls for every candle.
        month_values = prices[row_indices].reshape(-1)
        unique_values = pd.unique(month_values[pd.notna(month_values)])
        if len(unique_values) == 0:
            continue
        digits = max(
            (
                int(count)
                for value in unique_values
                if not math.isnan(count := _fractional_digit_count(value))
            ),
            default=None,
        )
        if digits is None:
            continue
        step = 10.0**-digits
        if previous_step is not None and step == previous_step:
            continue
        month_start = pd.Timestamp(
            year=int(year),
            month=int(month),
            day=1,
            tz="UTC",
        )
        changes.append(
            {
                "timestamp_ms": int(month_start.value // 1_000_000),
                "step": step,
            }
        )
        previous_step = step
    return changes


def _series_column(frame: pd.DataFrame, name: str) -> pd.Series:
    column = frame[name]
    if not isinstance(column, pd.Series):
        raise ValueError(f"historical price precision column is duplicated: {name}")
    return column


def _fractional_digit_count(value: Any) -> float:
    """Mirror Freqtrade's NumPy formatting before its regex extraction."""
    rendered = np.format_float_positional(
        float(value),
        precision=14,
        unique=False,
        fractional=False,
        trim="-",
    )
    match = _FRACTIONAL_SIGNIFICANT_DIGITS.search(rendered)
    return float(len(match.group(1))) if match is not None else float("nan")
