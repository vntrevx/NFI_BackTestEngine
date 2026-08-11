from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine import _rust, full_vector_runtime, research_runner
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.full_vector_runtime import (
    _config_identity_sha256,
    _retained_trade_features,
    build_full_native_vector_manifest,
)
from nfi_backtest_engine.hot_ir import build_hot_callback_ir
from nfi_backtest_engine.strategy_ir import analyze_strategy

START_MS = 1_735_689_600_000


def test_config_identity_is_cross_language_and_float_formatter_independent() -> None:
    left = {"z": [None, True, False, 7, -3, 1e-5, -0.0], "a": {"한글": "값"}}
    right = {"a": {"한글": "값"}, "z": [None, True, False, 7, -3, 0.00001, -0.0]}

    expected = "df8efe5440e003a372b0ae0d57c7dcd360517af8ace16e68601e862f48a79525"
    assert _config_identity_sha256(left) == expected
    assert _config_identity_sha256(right) == expected


def test_retained_features_exclude_candle_fields_but_keep_raw_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        full_vector_runtime,
        "_required_trade_features",
        lambda _hot_ir: ["close", "enter_long", "open", "RSI_14"],
    )

    assert _retained_trade_features({}) == ["enter_long", "RSI_14"]


def test_builder_hardlinks_raw_frames_and_runs_the_sealed_manifest(tmp_path: Path) -> None:
    strategy = tmp_path / "strategy.py"
    _write_strategy(strategy)
    data = tmp_path / "data"
    raw = data / "BTC_USDT-5m.feather"
    _write_frame(raw)
    market = tmp_path / "market.json"
    write_json(market, _market_snapshot())
    config = _config()
    analysis = analyze_strategy(strategy, class_name="ManifestStrategy")
    hot_ir = build_hot_callback_ir(
        analysis,
        trading_mode="spot",
        run_mode="backtest",
        config=config,
    )
    manifest = tmp_path / "run" / "simulation-input.manifest.json"

    document = build_full_native_vector_manifest(
        strategy_path=strategy,
        class_name="ManifestStrategy",
        analysis=analysis,
        hot_ir=hot_ir,
        config=config,
        pairs=["BTC/USDT"],
        data_directory=data,
        timerange=f"{START_MS}-{START_MS + 900_000}",
        market_metadata_path=market,
        destination=manifest,
    )

    linked = manifest.parent / document["frames"][0]["artifact"]["path"]
    assert os.path.samefile(raw, linked)
    assert raw.stat().st_size == linked.stat().st_size
    assert document["run"]["source_row_shift"] == 1
    assert document["retained_features"]["columns"] == []
    assert read_json(manifest) == document

    result = tmp_path / "run" / "result.json"
    profile = tmp_path / "run" / "profile.json"
    _rust.simulate_full_vector_file_profiled(manifest, result, profile)
    trades = read_json(result)["trades"]
    assert len(trades) == 1
    assert trades[0]["entry_tag"] == "test  "
    assert trades[0]["exit_reason"] == "force_exit"
    input_profile = read_json(profile)["input"]
    assert input_profile["manifest_sha256"] is not None
    assert input_profile["raw_frame_count"] == 1
    assert input_profile["transport"]["pair_count"] == 1

    research_runner._validate_full_native_manifest_artifacts(manifest)
    linked.write_bytes(linked.read_bytes() + b"tampered")
    with pytest.raises(BenchmarkError, match="artifact SHA-256 differs"):
        research_runner._validate_full_native_manifest_artifacts(manifest)


def _write_strategy(path: Path) -> None:
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class ManifestStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    startup_candle_count = 0\n"
        "    stoploss = -0.2\n"
        "    minimal_roi = {}\n"
        "    can_short = False\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['score'] = dataframe['close'] - dataframe['open']\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['enter_long', 'enter_short']] = 0\n"
        "        dataframe.loc[dataframe['score'] > 0, 'enter_long'] = 1\n"
        "        dataframe.loc[dataframe['score'] > 0, 'enter_tag'] = 'test  '\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['exit_long', 'exit_short']] = 0\n"
        "        return dataframe\n",
        encoding="utf-8",
    )


def _write_frame(path: Path) -> None:
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": pd.to_datetime(
                [START_MS + offset * 300_000 for offset in range(4)],
                unit="ms",
                utc=True,
            ),
            "open": [10.0, 10.0, 10.0, 10.0],
            "high": [11.0, 12.0, 11.0, 10.0],
            "low": [9.0, 9.0, 8.0, 9.0],
            "close": [10.0, 11.0, 9.0, 10.0],
            "volume": [1.0, 1.0, 1.0, 1.0],
        }
    ).to_feather(path)


def _config() -> dict:
    return {
        "trading_mode": "spot",
        "stake_currency": "USDT",
        "dry_run_wallet": 1_000.0,
        "max_open_trades": 1,
        "stake_amount": 100.0,
        "fee": 0.001,
        "enable_protections": False,
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
    }


def _market_snapshot() -> dict:
    return {
        "exchange": "binance",
        "markets": {
            "BTC/USDT": {
                "taker": 0.001,
                "precision": {"amount": 0.001, "price": 0.01},
                "limits": {
                    "amount": {"min": 0.001},
                    "cost": {"min": 5.0},
                    "leverage": {},
                },
            }
        },
    }
