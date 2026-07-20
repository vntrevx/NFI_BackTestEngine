from __future__ import annotations

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_overrides import effective_stoploss_ratio


def test_config_stoploss_overrides_strategy_literal() -> None:
    assert effective_stoploss_ratio(
        {"stoploss": -0.99},
        {"stoploss": -0.001},
    ) == -0.001


def test_strategy_stoploss_is_used_without_config_override() -> None:
    assert effective_stoploss_ratio({"stoploss": -0.25}, {}) == -0.25


@pytest.mark.parametrize("value", [True, 0.0, -1.0, float("nan")])
def test_invalid_effective_stoploss_fails_closed(value: object) -> None:
    with pytest.raises(StrategyAnalysisError, match="effective strategy stoploss"):
        effective_stoploss_ratio({"stoploss": -0.25}, {"stoploss": value})
