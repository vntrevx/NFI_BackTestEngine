"""Stable product-level defaults shared by setup and release evidence.

Keeping these values in one small module prevents the setup wizard, performance gate,
and documentation tests from quietly drifting to different definitions of a
long-horizon NFI run.
"""

from __future__ import annotations

from datetime import UTC, datetime

DEFAULT_BACKTEST_YEARS = 5
MIN_RELEASE_BACKTEST_DAYS = DEFAULT_BACKTEST_YEARS * 365
MIN_RELEASE_PAIR_COUNT = 80
TARGET_SCREENING_SPEEDUP = 10.0
MIN_CERTIFICATION_REPETITIONS = 3
MAX_CERTIFICATION_REPETITIONS = 5
DEFAULT_CERTIFICATION_REPETITIONS = MIN_CERTIFICATION_REPETITIONS
DEFAULT_CERTIFICATION_WARMUPS = 1
CERTIFICATION_SPREAD_THRESHOLD = 0.05
DEFAULT_CERTIFICATION_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_FULL_X7_TIMEOUT_SECONDS = 48 * 60 * 60
# Full X7 uses these informative timeframes in addition to its base 5m stream.
# Keeping the release surface named and versioned prevents a newer strategy from
# silently reducing the certified workload.
FULL_X7_RELEASE_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")


def default_long_timerange(
    now: datetime,
    *,
    years: int = DEFAULT_BACKTEST_YEARS,
) -> str:
    """Return the previous ``years`` complete UTC calendar years.

    Complete calendar years are reproducible and avoid making today's partial year look
    like a full validation period. The duration may include leap days; the release gate
    intentionally uses a minimum day count separately.
    """
    if years < 1:
        raise ValueError("backtest years must be positive")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    stop_year = now.astimezone(UTC).year
    start_year = stop_year - years
    return f"{start_year:04d}0101-{stop_year:04d}0101"
