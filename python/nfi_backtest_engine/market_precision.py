"""Historical exchange precision derived with Freqtrade's OHLCV rule."""

from __future__ import annotations

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
    counts = pd.DataFrame(
        {
            column: _series_column(frame, column).map(_fractional_digit_count)
            for column in _PRICE_COLUMNS
        }
    )
    row_maximum = counts.max(axis=1, skipna=True)
    month_keys = pd.DataFrame(
        {
            "year": dates.dt.year,
            "month": dates.dt.month,
            "digits": row_maximum,
        }
    )
    monthly = cast(
        pd.Series,
        month_keys.groupby(["year", "month"], sort=True, observed=True)["digits"].max(),
    )
    monthly = monthly.dropna()

    changes: list[dict[str, Any]] = []
    previous_step: float | None = None
    for month_key, raw_digits in monthly.items():
        # Pandas types a MultiIndex key as any Hashable even though this
        # groupby is structurally fixed to (year, month). Validate the shape
        # before unpacking so a future grouping change fails clearly.
        if not isinstance(month_key, tuple) or len(month_key) != 2:
            raise ValueError("historical price precision produced an invalid month key")
        year, month = month_key
        digits = int(raw_digits)
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
