"""Shared closed-boundary parsing for Freqtrade-compatible timeranges."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_timerange_milliseconds(value: str) -> tuple[int, int]:
    """Return closed start/stop boundaries as Unix milliseconds.

    Freqtrade accepts calendar dates, Unix seconds, and Unix milliseconds.
    Research runs require both boundaries because candle seals must describe a
    finite immutable input. The caller decides whether an equal range is useful.
    """
    start_text, separator, stop_text = value.partition("-")
    if not separator or "-" in stop_text:
        raise ValueError("timerange must contain exactly one separator")
    start_ms = _boundary_milliseconds(start_text)
    stop_ms = _boundary_milliseconds(stop_text)
    if start_ms > stop_ms:
        raise ValueError("timerange start is after its stop boundary")
    return start_ms, stop_ms


def _boundary_milliseconds(value: str) -> int:
    if not value.isdigit():
        raise ValueError("timerange boundary must be numeric")
    try:
        if len(value) == 8:
            parsed = datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
            return int(parsed.timestamp() * 1000)
        if len(value) == 10:
            milliseconds = int(value) * 1000
        elif len(value) == 13:
            milliseconds = int(value)
        else:
            raise ValueError("unsupported timerange boundary width")
        # Validate the timestamp against this Python platform instead of
        # allowing a huge integer to fail later inside pandas or Polars.
        datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        return milliseconds
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"invalid timerange boundary: {value!r}") from exc
