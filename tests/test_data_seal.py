from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine import data_seal
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.data_seal import (
    _append_until_end_covered,
    _download_data,
    _needs_prepend,
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


def test_new_files_do_not_schedule_a_redundant_prepend_download() -> None:
    missing_file = {
        "start_missing": True,
        "end_missing": True,
        "available_start_timestamp_ms": None,
    }
    existing_partial_file = {
        "start_missing": True,
        "end_missing": True,
        "available_start_timestamp_ms": 1_700_000_000_000,
    }

    assert not _needs_prepend(
        [missing_file],
        [],
        require_startup_coverage=False,
    )
    assert _needs_prepend(
        [existing_partial_file],
        [],
        require_startup_coverage=False,
    )


def test_append_download_converges_without_a_fixed_retry_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    initial = [
        {
            "pair": "SOL/USDT",
            "timeframe": "15m",
            "end_missing": True,
            "available_end_timestamp_ms": 100,
        }
    ]
    remaining = [
        [
            {
                "pair": "SOL/USDT",
                "timeframe": "15m",
                "end_missing": True,
                "available_end_timestamp_ms": 200,
            }
        ],
        [],
    ]
    monkeypatch.setattr(
        data_seal,
        "_download_data",
        lambda **_kwargs: {"exit_code": 0},
    )
    monkeypatch.setattr(
        data_seal,
        "find_coverage_gaps",
        lambda *_args: remaining.pop(0),
    )

    downloads, gaps = _append_until_end_covered(
        config_file=tmp_path / "config.json",
        data_root=tmp_path,
        request={},
        gaps=initial,
    )

    assert len(downloads) == 2
    assert gaps == []


def test_append_download_fails_when_coverage_frontier_stalls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stalled = [
        {
            "pair": "SOL/USDT",
            "timeframe": "15m",
            "end_missing": True,
            "available_end_timestamp_ms": 100,
        }
    ]
    monkeypatch.setattr(
        data_seal,
        "_download_data",
        lambda **_kwargs: {"exit_code": 0},
    )
    monkeypatch.setattr(
        data_seal,
        "find_coverage_gaps",
        lambda *_args: stalled,
    )

    with pytest.raises(BenchmarkError, match="did not advance"):
        _append_until_end_covered(
            config_file=tmp_path / "config.json",
            data_root=tmp_path,
            request={},
            gaps=stalled,
        )


def test_data_download_flattens_host_relative_config_includes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.json"
    child.write_text(
        '{"exchange":{"name":"binance","pair_whitelist":["BTC/USDT"]},'
        '"pairlists":[{"method":"StaticPairList"}],'
        '"api_server":{"enabled":true,"jwt_secret_key":"local-secret"}}',
        encoding="utf-8",
    )
    root = tmp_path / "root.json"
    root.write_text('{"add_config_files":["child.json"]}', encoding="utf-8")
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    observed: dict[str, object] = {"write_file": True}

    monkeypatch.setattr(data_seal, "ensure_docker_config", lambda: tmp_path)
    monkeypatch.setattr(data_seal, "ensure_reference_image", lambda **_kwargs: None)

    @contextmanager
    def fake_managed_run(**_kwargs):
        yield {"command_prefix": ["docker", "run"]}

    monkeypatch.setattr(data_seal, "managed_docker_run", fake_managed_run)

    def fake_subprocess_run(command, **_kwargs):
        mount = command[command.index("--volume") + 1]
        mounted_path = Path(mount.removesuffix(":/input/config.json:ro"))
        mounted = read_json(mounted_path)
        observed["mounted"] = mounted
        if observed["write_file"]:
            (data_directory / "BTC_USDT-5m.feather").write_bytes(b"captured")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(data_seal.subprocess, "run", fake_subprocess_run)
    report = _download_data(
        config_file=root,
        data_root=data_directory,
        request={
            "download_timerange": "20210101-20260101",
            "timeframes": ["5m"],
            "pairs": ["BTC/USDT"],
            "trading_mode": "spot",
        },
        prepend=False,
    )

    assert report["exit_code"] == 0
    assert observed["mounted"] == {
        "exchange": {
            "name": "binance",
            "pair_whitelist": ["BTC/USDT"],
        },
        "pairlists": [{"method": "StaticPairList"}],
    }
    observed["write_file"] = False
    with pytest.raises(BenchmarkError, match="no candle file was created or changed"):
        _download_data(
            config_file=root,
            data_root=data_directory,
            request={
                "download_timerange": "20210101-20260101",
                "timeframes": ["5m"],
                "pairs": ["BTC/USDT"],
                "trading_mode": "spot",
            },
            prepend=False,
        )


def test_available_history_records_a_later_listing_without_hiding_stale_data(
    tmp_path: Path,
) -> None:
    source = FIXTURE / "inputs" / "candles" / "BTC_USDT-5m.feather"
    candles = pd.read_feather(source).iloc[12:].copy()
    candles.to_feather(tmp_path / "BTC_USDT-5m.feather")

    seal = prepare_data(
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=tmp_path,
        timerange="20250101-20250104",
        timeframes=["5m"],
        destination=tmp_path / "data-seal.json",
        download_missing=False,
        history_coverage_policy="available",
    )

    assert seal["request"]["history_coverage_policy"] == "available"
    assert seal["coverage_shortfalls"] == [
        {
            "pair": "BTC/USDT",
            "timeframe": "5m",
            "start_missing": True,
            "end_missing": False,
            "available_start_timestamp_ms": 1_735_693_200_000,
            "available_end_timestamp_ms": 1_735_948_500_000,
        }
    ]


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
