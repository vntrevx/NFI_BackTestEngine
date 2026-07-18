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
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
)


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
