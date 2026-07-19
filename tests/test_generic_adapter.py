from __future__ import annotations

from pathlib import Path

import pandas as pd
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.engine_runtime import run_engine
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.generic_adapter import (
    build_generic_simulation_input,
    build_generic_vector_manifest,
    generic_adapter_blockers,
)
from nfi_backtest_engine.parity import first_difference
from nfi_backtest_engine.research_runner import run_research_backtest
from nfi_backtest_engine.strategy_ir import analyze_strategy

ROOT = Path(__file__).parents[1]
STOPS_FIXTURE = ROOT / "benchmarks" / "fixtures" / "captured" / "stops-only-spot-2025-01-01_04"


def _analysis(tmp_path: Path) -> dict:
    source = tmp_path / "Simple.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Simple(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    stoploss = -0.1\n"
        "    minimal_roi = {}\n"
        "    can_short = False\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    return analyze_strategy(source, class_name="Simple")


def _config() -> dict:
    return {
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "trading_mode": "spot",
        "dry_run_wallet": 1000,
        "stake_amount": 100,
        "max_open_trades": 1,
    }


def test_generic_adapter_requires_frozen_market_metadata(tmp_path: Path) -> None:
    blockers = generic_adapter_blockers(
        _analysis(tmp_path),
        _config(),
        market_metadata_path=None,
    )

    assert [item["code"] for item in blockers] == ["MARKET_METADATA_REQUIRED"]


def test_generic_adapter_fails_closed_for_uncertified_spot_semantics(
    tmp_path: Path,
) -> None:
    analysis = _analysis(tmp_path)
    analysis["strategies"][0]["constants"]["can_short"] = True
    config = {
        **_config(),
        "exit_profit_only": True,
        "enable_protections": True,
        "tradable_balance_ratio": 0.05,
    }

    blockers = generic_adapter_blockers(
        analysis,
        config,
        market_metadata_path=tmp_path / "missing-markets.json",
    )

    codes = {item["code"] for item in blockers}
    assert "GENERIC_SHORT_ADAPTER_UNSUPPORTED" in codes
    assert "PROTECTIONS_UNSUPPORTED" in codes
    assert "EXIT_PROFIT_ONLY_UNSUPPORTED" in codes
    assert "STAKE_CAPACITY_UNPROVEN" in codes


def test_generic_adapter_compiles_next_open_signal_arrays(tmp_path: Path) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.0, 101.0],
            "volume": [1.0, 1.0],
            "nfi_exec_enter_long": [0, 1],
            "nfi_exec_exit_long": [0, 0],
            "nfi_exec_enter_short": [0, 0],
            "nfi_exec_exit_short": [0, 0],
            "nfi_exec_enter_tag": [None, "entry"],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    write_json(
        markets,
        {
            "markets": {
                "BTC/USDT": {
                    "precision": {"amount": 0.00001, "price": 0.01},
                    "taker": 0.001,
                }
            }
        },
    )

    document = build_generic_simulation_input(
        analysis=_analysis(tmp_path),
        config=_config(),
        vector_report={
            "outputs": [{"pair": "BTC/USDT", "path": str(vector)}],
        },
        market_metadata_path=markets,
        destination=tmp_path / "simulation.json",
    )

    candles = document["pairs"][0]["candles"]
    assert candles[0]["enter_long"] is None
    assert candles[1]["enter_long"]["tag"] == "entry"
    assert document["config"]["fee_rate"] == 0.001


def test_feather_manifest_matches_the_legacy_json_transport(tmp_path: Path) -> None:
    vector = tmp_path / "BTC_USDT.feather"
    pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=3, freq="5min", tz="UTC"),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [1.0, 1.0, 1.0],
            "nfi_exec_enter_long": [0, 1, 0],
            "nfi_exec_exit_long": [0, 0, 0],
            "nfi_exec_enter_short": [0, 0, 0],
            "nfi_exec_exit_short": [0, 0, 0],
            "nfi_exec_enter_tag": [None, "entry", None],
        }
    ).to_feather(vector)
    markets = tmp_path / "markets.json"
    write_json(
        markets,
        {
            "markets": {
                "BTC/USDT": {
                    "precision": {"amount": 0.00001, "price": 0.01},
                    "taker": 0.001,
                }
            }
        },
    )
    vector_report = {
        "outputs": [
            {
                "pair": "BTC/USDT",
                "path": str(vector),
                "sha256": sha256_file(vector),
                "execution_start_index": 1,
            }
        ]
    }
    legacy_input = tmp_path / "legacy-input.json"
    manifest_input = tmp_path / "vector-input.manifest.json"
    build_generic_simulation_input(
        analysis=_analysis(tmp_path),
        config=_config(),
        vector_report=vector_report,
        market_metadata_path=markets,
        destination=legacy_input,
    )
    manifest = build_generic_vector_manifest(
        analysis=_analysis(tmp_path),
        config=_config(),
        vector_report=vector_report,
        market_metadata_path=markets,
        destination=manifest_input,
    )

    run_engine(legacy_input, tmp_path / "legacy-result.json")
    run_engine(
        manifest_input,
        tmp_path / "manifest-result.json",
        vector_manifest=True,
    )

    assert manifest["pairs"][0]["vector"]["path"] == "BTC_USDT.feather"
    assert manifest["pairs"][0]["execution_start_index"] == 1
    assert read_json(tmp_path / "manifest-result.json") == read_json(
        tmp_path / "legacy-result.json"
    )


def test_public_generic_runner_matches_captured_freqtrade_surface(tmp_path: Path) -> None:
    report = run_research_backtest(
        strategy_path=STOPS_FIXTURE / "inputs" / "strategy.py",
        class_name="ContractStopsOnly",
        config_path=STOPS_FIXTURE / "inputs" / "config.json",
        data_directory=STOPS_FIXTURE / "inputs" / "candles",
        timerange="20250101-20250104",
        output_directory=tmp_path / "run",
        cache_directory=tmp_path / "cache",
        profile_path=tmp_path / "profile.json",
        market_metadata_path=(STOPS_FIXTURE / "inputs" / "market_metadata" / "markets.json"),
        download_missing=False,
    )

    expected = read_json(STOPS_FIXTURE / "artifacts" / "trade-surface.json")
    actual = read_json(tmp_path / "run" / "trade-surface.json")
    assert report["status"] == "complete"
    assert first_difference(expected, actual) is None
