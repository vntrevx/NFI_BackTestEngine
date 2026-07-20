from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.platform_benchmark import (
    _portable_timerange,
    seal_platform_evidence,
)


def _report(system: str, machine: str, *, result: str = "a" * 64) -> dict:
    return {
        "schema_version": "1.0.0",
        "complete": True,
        "platform": {
            "system": system,
            "machine": machine,
            "wsl": system == "linux",
        },
        "package": {
            "version": "1.0.0",
            "wheel_sha256": system[0] * 64,
            "installed_extension_equal": True,
        },
        "workload": {
            "identity_sha256": "d" * 64,
            "pairs": [f"PAIR-{index}" for index in range(20)],
        },
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
