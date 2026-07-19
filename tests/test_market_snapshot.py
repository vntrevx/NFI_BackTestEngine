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
    )

    assert result["markets"]["BTC/USDT"]["precision"]["amount"] == 0.00001
    assert result["markets"]["BTC/USDT"]["precision"]["price"] == 0.01
    assert result["markets"]["BTC/USDT"]["limits"]["amount"]["min"] == 0.0001
    assert result["markets"]["BTC/USDT"]["limits"]["cost"]["min"] == 5.0
    assert result["markets"]["BTC/USDT"]["taker"] == 0.001
    assert len(result["sha256"]) == 64
