from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.platform_benchmark import (
    EXACT_FIXTURE_LANE,
    RAW_INPUT_LANE,
    _fixture_result_sha256,
    _portable_timerange,
    seal_platform_evidence,
)


def _report(
    system: str,
    machine: str,
    *,
    result: str = "a" * 64,
    mode_contract: str = "binance-usdtm-isolated",
    lane: str = RAW_INPUT_LANE,
) -> dict:
    workload = {
        "mode_contract": mode_contract,
        "identity_sha256": "d" * 64,
    }
    package = {
        "version": "1.0.0",
        "wheel_sha256": system[0] * 64,
        "installed_extension_equal": True,
    }
    if lane == RAW_INPUT_LANE:
        workload["pairs"] = [f"PAIR-{index}" for index in range(20)]
    else:
        workload.update(
            {
                "lane": EXACT_FIXTURE_LANE,
                "fixture_id": "x7-futures-short",
                "manifest_sha256": "e" * 64,
                "strategy_sha256": "f" * 64,
                "verification_level": "full",
            }
        )
        package["portable_package_sha256"] = "c" * 64
    return {
        "schema_version": "1.2.0",
        "complete": True,
        "lane": lane,
        "platform": {
            "system": system,
            "machine": machine,
            "wsl": system == "linux",
        },
        "package": package,
        "workload": workload,
        "measurement": {
            "result_sha256": [result],
            "wall_time_seconds": {"median": 10.0},
            "peak_rss_bytes": {"maximum": 1000},
            "measured_repetitions": 3,
        },
    }


def test_portable_workload_uses_last_complete_year_of_release_timerange() -> None:
    assert _portable_timerange("20210101-20260101") == "20250101-20260101"


def test_platform_seal_requires_three_systems_and_one_result(tmp_path: Path) -> None:
    paths = []
    for system, machine in (
        ("windows", "amd64"),
        ("linux", "x86_64"),
        ("darwin", "arm64"),
    ):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine))
        paths.append(path)

    evidence = seal_platform_evidence(paths, tmp_path / "sealed")

    assert evidence["release_certified"] is True
    assert evidence["mode_contract"] == "binance-usdtm-isolated"
    assert evidence["workload"]["identity_sha256"] == "d" * 64
    assert evidence["result_sha256"] == "a" * 64
    assert [item["system"] for item in evidence["platforms"]] == [
        "darwin",
        "linux",
        "windows",
    ]


def test_platform_seal_rejects_cross_os_result_drift(tmp_path: Path) -> None:
    paths = []
    for system, machine, result in (
        ("windows", "amd64", "a" * 64),
        ("linux", "x86_64", "a" * 64),
        ("darwin", "arm64", "b" * 64),
    ):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine, result=result))
        paths.append(path)

    with pytest.raises(SpecValidationError, match="differs"):
        seal_platform_evidence(paths, tmp_path / "sealed")


def test_platform_seal_accepts_exact_fixture_lane(tmp_path: Path) -> None:
    paths = []
    for system, machine in (
        ("windows", "amd64"),
        ("linux", "x86_64"),
        ("darwin", "arm64"),
    ):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine, lane=EXACT_FIXTURE_LANE))
        paths.append(path)

    evidence = seal_platform_evidence(paths, tmp_path / "sealed")

    assert evidence["release_certified"] is True
    assert evidence["lane"] == EXACT_FIXTURE_LANE
    assert all(
        item["native_extension_sha256"] is None
        for item in evidence["platforms"]
    )


def test_fixture_result_identity_ignores_host_paths() -> None:
    def report(root: str) -> dict:
        return {
            "fixture_id": "x7-futures-short",
            "artifacts": {
                "trade_surface": {
                    "path": f"{root}/trade-surface.json",
                    "sha256": "a" * 64,
                }
            },
            "parity": {
                "state_trace": {
                    "actual": {
                        "path": f"{root}/state.trace",
                        "input_sha256": "b" * 64,
                        "profile_sha256": "c" * 64,
                        "stream_hash": "d" * 64,
                    }
                }
            },
        }

    trade_surface = {
        "schema_version": "1.0.0",
        "strategy": "NostalgiaForInfinityX7",
        "trades": [],
    }
    assert _fixture_result_sha256(
        report("C:/runner"),
        trade_surface,
    ) == _fixture_result_sha256(
        report("/home/runner"),
        trade_surface,
    )
