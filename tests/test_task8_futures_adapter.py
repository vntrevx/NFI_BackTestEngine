from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd  # noqa: PANDAS_OK
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.generic_adapter import (
    _signal_candles,
    build_generic_vector_manifest,
    generic_adapter_blockers,
)
from nfi_backtest_engine.strategy_ir import analyze_strategy

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "benchmarks/reference/strategies/CurrentChangedPredicateContract.py"


def _config() -> dict[str, Any]:
    return {
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT:USDT"]},
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "dry_run_wallet": 1000,
        "stake_amount": 100,
        "max_open_trades": 1,
    }


def test_futures_signal_adapter_preserves_funding_and_mark_inputs() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=1, freq="5min", tz="UTC"),
            "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0],
            "volume": [1.0], "nfi_exec_enter_long": [0], "nfi_exec_enter_short": [1],
            "nfi_exec_exit_long": [0], "nfi_exec_exit_short": [0],
            "nfi_exec_funding_rate": [0.0001],
            "nfi_exec_funding_mark_price": [100.5],
        }
    )

    candle = _signal_candles(frame, can_short=True, use_exit_signal=True)[0]

    assert candle["funding_rate"] == 0.0001
    assert candle["funding_mark_price"] == 100.5


def test_callback_free_futures_signal_adapter_accepts_typed_short_lanes() -> None:
    analysis = analyze_strategy(CONTRACT, class_name="CurrentChangedPredicateContract")
    analysis["strategies"][0]["constants"]["can_short"] = True

    blockers = generic_adapter_blockers(
        analysis,
        _config(),
        market_metadata_path=ROOT / "planning/freqtrade-futures-contract.json",
    )

    assert "GENERIC_FUTURES_ADAPTER_UNSUPPORTED" not in {item["code"] for item in blockers}
    assert "GENERIC_SHORT_ADAPTER_UNSUPPORTED" not in {item["code"] for item in blockers}


def test_generic_futures_manifest_includes_funding_transport(tmp_path: Path) -> None:
    vector = tmp_path / "BTC_USDT_USDT.feather"
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0],
            "close": [100.0, 100.0], "volume": [1.0, 1.0],
            "nfi_exec_enter_long": [0, 1], "nfi_exec_enter_short": [0, 0],
            "nfi_exec_exit_long": [0, 0], "nfi_exec_exit_short": [0, 0],
            "nfi_exec_funding_rate": [0.0001, float("nan")],
            "nfi_exec_funding_mark_price": [100.0, float("nan")],
        }
    )
    frame.to_feather(vector)
    markets = tmp_path / "markets.json"
    write_json(
        markets,
        {
            "markets": {
                "BTC/USDT:USDT": {
                    "precision": {"amount": 0.001, "price": 0.1},
                    "taker": 0.0005,
                }
            }
        },
    )
    analysis = analyze_strategy(CONTRACT, class_name="CurrentChangedPredicateContract")
    analysis["strategies"][0]["constants"]["can_short"] = True

    manifest = build_generic_vector_manifest(
        analysis=analysis,
        config=_config(),
        vector_report={
            "outputs": [
                {
                    "pair": "BTC/USDT:USDT",
                    "path": str(vector),
                    "sha256": sha256_file(vector),
                    "execution_start_index": 0,
                }
            ]
        },
        market_metadata_path=markets,
        destination=tmp_path / "manifest.json",
    )

    assert manifest["pairs"][0]["include_funding"] is True
    assert manifest["config"]["funding_fee_interval_ms"] == 8 * 60 * 60 * 1000
