from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.normalize import normalize_freqtrade_result
from nfi_backtest_engine.specs import validate_trade_surface

ROOT = Path(__file__).parents[2]
RAW_RESULT = (
    ROOT / "benchmarks" / "fixtures" / "contract" / "normal-routing" / "freqtrade-result.json"
)


def _v2_source() -> dict:
    raw = read_json(RAW_RESULT, decimals=True)
    result = raw["strategy"]["ContractNormalRouting"]
    result.update(
        {
            "locks": [],
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "timeframe": "5m",
            "timeframe_detail": None,
            "timerange": "20220101-20220103",
            "total_trades": len(result["trades"]),
            "starting_balance": 1000,
            "final_balance": 1005,
            "profit_total_abs": 5,
            "total_volume": 500,
            "rejected_signals": 0,
            "timedout_entry_orders": 0,
            "timedout_exit_orders": 0,
            "canceled_trade_entries": 0,
            "canceled_entry_orders": 0,
            "replaced_entry_orders": 0,
            "max_open_trades": 2,
        }
    )
    for trade in result["trades"]:
        trade.update(
            {
                "trade_duration": 60,
                "is_open": False,
                "min_rate": trade["open_rate"],
                "max_rate": trade["close_rate"],
                "initial_stop_loss_ratio": -0.1,
                "stop_loss_ratio": -0.05,
                "weekday": 1,
            }
        )
    return raw


def test_v2_normalizes_context_summary_and_extended_trade_state() -> None:
    surface = normalize_freqtrade_result(_v2_source(), surface_version="2")

    assert surface["schema_version"] == "2.0.0"
    assert surface["strategy"] == "ContractNormalRouting"
    assert surface["context"]["margin_mode"] == "isolated"
    assert surface["summary"]["starting_balance"] == "1000"
    assert surface["trades"][0]["duration_minutes"] == 60
    assert surface["trades"][0]["initial_stop_loss_ratio"] == "-0.1"


def test_v2_rejects_noncanonical_summary_decimal() -> None:
    surface = normalize_freqtrade_result(_v2_source(), surface_version="2")
    surface["summary"]["starting_balance"] = "1000.0"

    with pytest.raises(SpecValidationError, match="decimal is not canonical"):
        validate_trade_surface(surface)
