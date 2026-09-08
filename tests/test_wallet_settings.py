from __future__ import annotations

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.wallet_settings import starting_wallet_balance, wallet_policy


def test_stake_wallet_mapping_and_separate_capital_budget():
    config = {
        "dry_run_wallet": {"BTC": 0.2, "USDT": 10000},
        "stake_currency": "USDT",
        "available_capital": 3000,
        "tradable_balance_ratio": 0.75,
    }
    assert starting_wallet_balance(config) == 10000
    assert wallet_policy(config) == {
        "entry_minimum_stoploss_ratio": -0.05,
        "amend_last_stake_amount": False,
        "last_stake_amount_min_ratio": 0.5,
        "available_capital": 3000,
        "initial_asset_balances": {"BTC": 0.2},
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"stake_amount": "unlimited", "max_open_trades": -1},
        {"stake_amount": "unlimited", "max_open_trades": float("inf")},
        {"available_capital": -1},
        {"available_capital": None},
        {"last_stake_amount_min_ratio": 1.01},
        {"last_stake_amount_min_ratio": float("nan")},
        {"amend_last_stake_amount": 1},
        {"max_open_trades": 1.5, "stake_amount": 100},
    ],
)
def test_invalid_wallet_policy_fails_before_native_execution(settings):
    with pytest.raises(StrategyAnalysisError):
        wallet_policy(settings)


def test_multicurrency_cross_margin_is_not_silently_treated_as_isolated():
    with pytest.raises(StrategyAnalysisError, match="cross-margin"):
        starting_wallet_balance(
            {
                "dry_run_wallet": {"USDT": 10000, "BTC": 1},
                "stake_currency": "USDT",
                "margin_mode": "cross",
            }
        )
