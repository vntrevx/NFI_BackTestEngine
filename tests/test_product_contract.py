from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nfi_backtest_engine.product_contract import (
    DEFAULT_BACKTEST_YEARS,
    MIN_RELEASE_BACKTEST_DAYS,
    MIN_RELEASE_PAIR_COUNT,
    TARGET_SCREENING_SPEEDUP,
    default_long_timerange,
)


def test_product_contract_defaults_to_five_complete_years() -> None:
    assert DEFAULT_BACKTEST_YEARS == 5
    assert default_long_timerange(datetime(2026, 7, 19, tzinfo=UTC)) == (
        "20210101-20260101"
    )


def test_release_evidence_requires_five_years_and_full_pair_universe() -> None:
    assert MIN_RELEASE_BACKTEST_DAYS == 1825
    assert MIN_RELEASE_PAIR_COUNT == 80
    assert TARGET_SCREENING_SPEEDUP == 10.0


def test_default_long_timerange_rejects_a_non_positive_horizon() -> None:
    with pytest.raises(ValueError, match="positive"):
        default_long_timerange(datetime(2026, 7, 19, tzinfo=UTC), years=0)
