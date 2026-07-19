from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.data_seal import (
    find_coverage_gaps,
    prepare_data,
    validate_data_seal,
)
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "benchmarks" / "fixtures" / "captured" / "stops-only-spot-2025-01-01_04"


def test_existing_fixture_data_is_coverage_checked_and_sealed(tmp_path: Path) -> None:
    output = tmp_path / "data-seal.json"
    seal = prepare_data(
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="20250101-20250104",
        timeframes=["5m"],
        destination=output,
        download_missing=False,
    )

    assert seal["downloads"] == []
    assert seal["files"][0]["coverage"]["rows"] == 864
    assert validate_data_seal(output)["aggregate_sha256"] == seal["aggregate_sha256"]


def test_missing_coverage_fails_before_network_when_download_is_disabled(
    tmp_path: Path,
) -> None:
    config = read_json(FIXTURE / "inputs" / "config.json")
    request = {
        "pairs": config["exchange"]["pair_whitelist"],
        "timeframes": ["5m"],
        "trading_mode": "spot",
        "start_timestamp_ms": 1_735_689_600_000,
        "end_timestamp_ms": 1_735_948_800_000,
    }
    assert find_coverage_gaps(tmp_path, request)

    with pytest.raises(BenchmarkError, match="coverage is incomplete"):
        prepare_data(
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=tmp_path,
            timerange="20250101-20250104",
            timeframes=["5m"],
            destination=tmp_path / "seal.json",
            download_missing=False,
        )


def test_invalid_timerange_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SpecValidationError, match="YYYYMMDD"):
        prepare_data(
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=FIXTURE / "inputs" / "candles",
            timerange="2025-01-01",
            timeframes=["5m"],
            destination=tmp_path / "seal.json",
            download_missing=False,
        )


def test_unix_second_timerange_uses_the_same_data_seal_boundaries(
    tmp_path: Path,
) -> None:
    seal = prepare_data(
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="1735689600-1735948800",
        timeframes=["5m"],
        destination=tmp_path / "data-seal.json",
        download_missing=False,
    )

    assert seal["request"]["start_timestamp_ms"] == 1_735_689_600_000
    assert seal["request"]["end_timestamp_ms"] == 1_735_948_800_000


def test_startup_shortfall_is_sealed_like_freqtrade_allows_it(tmp_path: Path) -> None:
    seal = prepare_data(
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="20250101-20250104",
        timeframes=["5m"],
        destination=tmp_path / "data-seal.json",
        download_missing=False,
        startup_candles=1,
    )

    assert seal["request"]["startup_coverage_policy"] == "record"
    assert seal["startup_shortfalls"] == [
        {
            "pair": "BTC/USDT",
            "timeframe": "5m",
            "required_start_timestamp_ms": 1_735_689_300_000,
            "available_start_timestamp_ms": 1_735_689_600_000,
            "missing_candles": 1,
        }
    ]


def test_strict_startup_policy_fails_before_network(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="startup candle coverage is incomplete"):
        prepare_data(
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=FIXTURE / "inputs" / "candles",
            timerange="20250101-20250104",
            timeframes=["5m"],
            destination=tmp_path / "data-seal.json",
            download_missing=False,
            startup_candles=1,
            require_startup_coverage=True,
        )


def test_startup_coverage_boundaries_are_sealed_per_timeframe(tmp_path: Path) -> None:
    seal = prepare_data(
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="1735690200-1735948800",
        timeframes=["5m"],
        destination=tmp_path / "data-seal.json",
        download_missing=False,
        startup_candles=2,
    )

    request = seal["request"]
    assert request["start_timestamp_ms"] == 1_735_690_200_000
    assert request["coverage_start_timestamp_ms_by_timeframe"] == {
        "5m": 1_735_689_600_000
    }
    assert request["download_timerange"] == "1735689600000-1735948800000"


def test_negative_startup_count_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SpecValidationError, match="non-negative integer"):
        prepare_data(
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=FIXTURE / "inputs" / "candles",
            timerange="20250101-20250104",
            timeframes=["5m"],
            destination=tmp_path / "data-seal.json",
            download_missing=False,
            startup_candles=-1,
        )
