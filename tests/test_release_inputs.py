from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.data_seal import inspect_candle_quality
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.release_contract import SPOT_RELEASE_CONTRACT
from nfi_backtest_engine.release_inputs import (
    discover_release_universe,
    select_release_universe,
    validate_release_data_roles,
    validate_release_input_lock,
)

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
)
FUTURES_FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-variable-leverage-futures-v17.4.435-2021-02-04_09"
)


def test_release_selector_seals_strict_complete_pairs_in_source_order(
    tmp_path: Path,
) -> None:
    candidates = tmp_path / "candidates.json"
    write_json(candidates, {"pairs": ["BTC/USDT"]})

    lock = select_release_universe(
        candidates_path=candidates,
        strategy_path=FIXTURE / "inputs" / "strategy.py",
        class_name="ContractNormalRouting",
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="1735690800-1735948800",
        output_directory=tmp_path / "release-inputs",
        pair_count=1,
        upstream_repository="https://github.com/iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
    )

    assert lock["pairlist"]["pairs"] == ["BTC/USDT"]
    assert lock["data"]["coverage_shortfall_count"] == 0
    assert lock["data"]["startup_shortfall_count"] == 0
    assert lock["data"]["startup_coverage_policy"] == "record"
    assert lock["scope"]["mode_contract"] == "binance-spot"
    assert lock["data"]["role_counts"] == {"candles": 1}
    validate_release_input_lock(lock, required_pair_count=1)

    changed = read_json(tmp_path / "release-inputs" / "release-input-lock.json")
    changed["pairlist"]["pairs"] = ["ETH/USDT"]
    with pytest.raises(SpecValidationError, match="identity is corrupt"):
        validate_release_input_lock(changed, required_pair_count=1)


def test_release_selector_records_pre_listing_startup_without_relaxing_timerange(
    tmp_path: Path,
) -> None:
    candidates = tmp_path / "candidates.json"
    write_json(candidates, {"pairs": ["BTC/USDT"]})

    lock = select_release_universe(
        candidates_path=candidates,
        strategy_path=FIXTURE / "inputs" / "strategy.py",
        class_name="ContractNormalRouting",
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="1735689900-1735948800",
        output_directory=tmp_path / "release-inputs",
        pair_count=1,
        upstream_repository="https://github.com/iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
    )

    assert lock["data"]["coverage_shortfall_count"] == 0
    assert lock["data"]["startup_shortfall_count"] == 1
    validate_release_input_lock(lock, required_pair_count=1)


def test_release_selector_seals_futures_side_channels_and_mode_contract(
    tmp_path: Path,
) -> None:
    candidates = tmp_path / "candidates.json"
    write_json(candidates, {"pairs": ["ALGO/USDT:USDT"]})

    lock = select_release_universe(
        candidates_path=candidates,
        strategy_path=FUTURES_FIXTURE / "inputs" / "strategy.py",
        class_name="NostalgiaForInfinityX7",
        config_path=FUTURES_FIXTURE / "inputs" / "config.json",
        data_directory=FUTURES_FIXTURE / "inputs" / "data",
        timerange="20210204-20210209",
        output_directory=tmp_path / "release-inputs",
        pair_count=1,
        upstream_repository="https://github.com/iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
    )

    assert lock["scope"]["mode_contract"] == "binance-usdtm-isolated"
    assert lock["scope"]["margin_mode"] == "isolated"
    assert lock["data"]["role_counts"] == {
        "candles": 5,
        "funding_rate": 1,
        "mark": 1,
    }
    validate_release_input_lock(lock, required_pair_count=1)


def test_release_discovery_filters_frozen_futures_markets_by_onboard_date(
    tmp_path: Path,
) -> None:
    report = discover_release_universe(
        config_path=FUTURES_FIXTURE / "inputs" / "config.json",
        market_snapshot_path=(
            FUTURES_FIXTURE
            / "inputs"
            / "reference_market_metadata"
            / "reference-markets.json"
        ),
        timerange="20210204-20260204",
        destination=tmp_path / "candidates.json",
    )

    assert report["mode_contract"] == "binance-usdtm-isolated"
    assert "ALGO/USDT:USDT" in report["pairs"]
    assert report["market_snapshot"]["sha256"]


def test_release_discovery_rejects_late_listings_without_fallback(
    tmp_path: Path,
) -> None:
    config = read_json(FUTURES_FIXTURE / "inputs" / "config.json")
    config_path = tmp_path / "config.json"
    write_json(config_path, config)
    market = {
        "active": True,
        "created": 1_700_000_000_000,
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "contract": True,
        "linear": True,
        "inverse": False,
        "marginModes": {"isolated": True},
    }
    markets_path = tmp_path / "markets.json"
    write_json(
        markets_path,
        {
            "exchange": "binance",
            "trading_mode": "futures",
            "pairs": ["ALGO/USDT:USDT"],
            "markets": {"ALGO/USDT:USDT": market},
        },
    )

    report = discover_release_universe(
        config_path=config_path,
        market_snapshot_path=markets_path,
        timerange="20210101-20260101",
        destination=tmp_path / "candidates.json",
    )

    assert report["pairs"] == []
    assert report["rejected"] == [
        {
            "pair": "ALGO/USDT:USDT",
            "reason": "LISTED_AFTER_TIMERANGE_START",
        }
    ]


def test_release_selector_rejects_duplicate_candle_timestamps(tmp_path: Path) -> None:
    source = FIXTURE / "inputs" / "candles" / "BTC_USDT-5m.feather"
    frame = pd.read_feather(source)
    frame = pd.concat([frame.iloc[:1], frame], ignore_index=True)
    data = tmp_path / "data"
    data.mkdir()
    frame.to_feather(data / source.name)
    candidates = tmp_path / "candidates.json"
    write_json(candidates, ["BTC/USDT"])

    quality = inspect_candle_quality(data / source.name, timeframe="5m")
    assert quality["duplicate_timestamp_count"] == 1

    with pytest.raises(BenchmarkError, match="only 0 candidates"):
        select_release_universe(
            candidates_path=candidates,
            strategy_path=FIXTURE / "inputs" / "strategy.py",
            class_name="ContractNormalRouting",
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=data,
            timerange="1735690800-1735948800",
            output_directory=tmp_path / "release-inputs",
            pair_count=1,
            upstream_repository=(
                "https://github.com/iterativv/NostalgiaForInfinity"
            ),
            upstream_commit="a" * 40,
        )
    report = read_json(tmp_path / "release-inputs" / "selection-report.json")
    assert report["rejected_candidates"][0]["reasons"][-1]["code"] == (
        "DUPLICATE_TIMESTAMPS"
    )


def test_release_data_roles_reject_duplicate_timeframe_files() -> None:
    seal = {
        "request": {
            "pairs": ["BTC/USDT"],
            "timeframes": ["5m", "1h"],
            "start_timestamp_ms": 1_000,
            "end_timestamp_ms": 2_000,
            "trading_mode": "spot",
            "exchange": "binance",
        },
        "files": [
            {"path": "BTC_USDT-5m.feather"},
            {"path": "nested/BTC_USDT-5m.feather"},
        ],
    }

    with pytest.raises(SpecValidationError, match="exactly one 5m candle"):
        validate_release_data_roles(seal, contract=SPOT_RELEASE_CONTRACT)
