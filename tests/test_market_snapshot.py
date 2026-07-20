from __future__ import annotations

from pathlib import Path

import ccxt
from nfi_backtest_engine import market_snapshot
from nfi_backtest_engine.market_snapshot import capture_market_snapshot


def test_market_capture_converts_tick_size_precision(monkeypatch, tmp_path: Path) -> None:
    class FakeExchange:
        precisionMode = ccxt.TICK_SIZE
        fees = {"trading": {"taker": 0.002}}

        def __init__(self, config):
            self.config = config

        def load_markets(self):
            return {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "base": "BTC",
                    "quote": "USDT",
                    "spot": True,
                    "precision": {"amount": 0.00001, "price": 0.01},
                    "limits": {
                        "amount": {"min": 0.0001, "max": 1000},
                        "cost": {"min": 5, "max": None},
                        "leverage": {"min": 1, "max": 20},
                    },
                    "taker": 0.001,
                }
            }

    monkeypatch.setattr(market_snapshot.ccxt, "fakeexchange", FakeExchange, raising=False)

    result = capture_market_snapshot(
        {
            "exchange": {
                "name": "fakeexchange",
                "ccxt_config": {},
            }
        },
        ["BTC/USDT"],
        tmp_path / "markets.json",
        leverage_tiers={
            "BTC/USDT": [
                {
                    "minNotional": 10_000,
                    "maxNotional": 50_000,
                    "maxLeverage": 10,
                    "maintenanceMarginRate": 0.01,
                    "maintAmt": 50,
                },
                {
                    "minNotional": 0,
                    "maxNotional": 10_000,
                    "maxLeverage": 20,
                    "maintenanceMarginRate": 0.005,
                    "info": {"cum": 0},
                },
            ]
        },
        leverage_tier_source={"kind": "test-fixture"},
    )

    assert result["markets"]["BTC/USDT"]["precision"]["amount"] == 0.00001
    assert result["markets"]["BTC/USDT"]["precision"]["price"] == 0.01
    assert result["markets"]["BTC/USDT"]["limits"]["amount"]["min"] == 0.0001
    assert result["markets"]["BTC/USDT"]["limits"]["cost"]["min"] == 5.0
    assert result["markets"]["BTC/USDT"]["limits"]["leverage"]["max"] == 20.0
    assert result["markets"]["BTC/USDT"]["leverage_tiers"] == [
        {
            "min_notional": 0.0,
            "max_notional": 10_000.0,
            "maximum_leverage": 20.0,
            "maintenance_margin_rate": 0.005,
            "maintenance_amount": 0.0,
        },
        {
            "min_notional": 10_000.0,
            "max_notional": 50_000.0,
            "maximum_leverage": 10.0,
            "maintenance_margin_rate": 0.01,
            "maintenance_amount": 50.0,
        },
    ]
    assert result["markets"]["BTC/USDT"]["taker"] == 0.001
    assert result["leverage_tier_source"] == {"kind": "test-fixture"}
    assert len(result["sha256"]) == 64
