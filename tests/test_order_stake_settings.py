"""Frozen exchange limits are explicit, normalized, and fail closed."""

import math

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.order_stake_settings import order_stake_policy


def inputs():
    return (
        {"trading_mode": "futures", "exchange": {"pair_whitelist": ["A/U"]}},
        {
            "markets": {
                "A/U": {
                    "contractSize": 0.01,
                    "limits": {"amount": {"max": 10}, "cost": {"max": 1000}},
                }
            }
        },
    )


def test_contract_size_normalizes_both_cost_and_amount():
    config, snapshot = inputs()
    assert order_stake_policy(config, snapshot) == {
        "pair_limits": {"A/U": {"maximum_amount": 0.1, "maximum_cost": 10.0}}
    }
    config["trading_mode"] = "spot"
    assert order_stake_policy(config, snapshot)["pair_limits"]["A/U"] == {
        "maximum_amount": 10.0,
        "maximum_cost": 1000.0,
    }


@pytest.mark.parametrize("value", [True, -1, math.inf, math.nan, "1", 10**1000])
@pytest.mark.parametrize("field", ["amount", "cost", "contractSize"])
def test_invalid_bounds_fail_closed(value, field):
    config, snapshot = inputs()
    market = snapshot["markets"]["A/U"]
    if field == "contractSize":
        market[field] = value
    else:
        market["limits"][field]["max"] = value
    with pytest.raises(StrategyAnalysisError):
        order_stake_policy(config, snapshot)


def test_unbounded_is_explicit_and_missing_bound_is_rejected():
    config, snapshot = inputs()
    snapshot["markets"]["A/U"]["limits"]["cost"]["max"] = None
    assert order_stake_policy(config, snapshot)["pair_limits"]["A/U"]["maximum_cost"] is None
    del snapshot["markets"]["A/U"]["limits"]["amount"]["max"]
    with pytest.raises(StrategyAnalysisError, match="maximum amount"):
        order_stake_policy(config, snapshot)
