"""Pinned Freqtrade wallet allocation vectors, including strict amendment boundaries."""

from __future__ import annotations

import inspect
import itertools
import json
from unittest.mock import patch

import freqtrade
from freqtrade.exceptions import DependencyException
from freqtrade.persistence import Trade
from freqtrade.wallets import Wallets

cases = []
for amend, ratio, free, tied, capital, profit, unlimited in itertools.product(
    (False, True),
    (0.0, 0.5, 1.0),
    (0.0, 49.0, 50.0, 51.0, 200.0),
    (0.0, 100.0, 300.0),
    (None, 0.0, 250.0),
    (-10.0, 0.0, 20.125),
    (False, True),
):
    config = {
        "stake_currency": "USDT",
        "stake_amount": "unlimited" if unlimited else 100.0,
        "tradable_balance_ratio": 0.75,
        "amend_last_stake_amount": amend,
        "last_stake_amount_min_ratio": ratio,
    }
    if capital is not None:
        config["available_capital"] = capital
    wallet = Wallets.__new__(Wallets)
    wallet._config = config
    wallet._stake_currency = "USDT"
    wallet.get_free = lambda currency, free=free: free
    with (
        patch.object(Trade, "total_open_trades_stakes", return_value=tied),
        patch.object(Trade, "get_total_closed_profit", return_value=profit),
    ):
        available = wallet.get_available_stake_amount()
        try:
            stake = wallet.get_trade_stake_amount("BTC/USDT", 2, update=False)
        except DependencyException:
            stake = None
    cases.append(
        {
            "amend": amend,
            "ratio": ratio,
            "free": free,
            "tied": tied,
            "capital": capital,
            "closed_profit": profit,
            "unlimited": unlimited,
            "available": available,
            "stake": stake,
        }
    )
print(
    json.dumps(
        {
            "freqtrade_version": freqtrade.__version__,
            "closed_profit_source": inspect.getsource(Trade.get_total_closed_profit),
            "cases": cases,
        },
        indent=2,
    )
)
