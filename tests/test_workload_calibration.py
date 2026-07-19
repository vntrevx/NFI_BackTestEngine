from __future__ import annotations

import pytest
from nfi_backtest_engine import workload_calibration
from nfi_backtest_engine.errors import SpecValidationError


def test_calibration_uses_measured_peak_for_admission(monkeypatch, tmp_path) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        workload_calibration.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"available": 20 * gib})(),
    )
    path = tmp_path / "calibration.json"
    identity = {"strategy": "hash", "timerange": "five-years"}
    key = workload_calibration.calibration_key(identity)

    report = workload_calibration.create_workload_calibration(
        path,
        key=key,
        identity=identity,
        hardware_fingerprint="hardware",
        probe_pair="WORST/USDT",
        probe_peak_rss_bytes=3 * gib,
        probe_wall_time_seconds=10.0,
        requested_cpu_processes=12,
        memory_cap_bytes=18 * gib,
        coordinator_rss_bytes=1 * gib,
    )

    # 17 GiB remains after the coordinator.  One measured 3 GiB worker is
    # retained as the reserve, so four workers are admitted.
    assert report["decision"]["safe_processes"] == 4
    assert report["decision"]["measured_reserve_bytes"] == 3 * gib
    assert workload_calibration.load_workload_calibration(
        path,
        expected_key=key,
        hardware_fingerprint="hardware",
    ) == report


def test_cached_peak_is_readmitted_against_current_free_memory(monkeypatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        workload_calibration.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"available": 11 * gib})(),
    )

    admission = workload_calibration.calibrated_admission(
        probe_peak_rss_bytes=3 * gib,
        requested_cpu_processes=8,
        memory_cap_bytes=20 * gib,
        coordinator_rss_bytes=1 * gib,
    )

    # A calibration created when the machine was idle must not reuse that old
    # free-memory snapshot. Ten GiB is currently available to children; after
    # the measured reserve, only two measured workers are admitted.
    assert admission["admitted_memory_bytes"] == 11 * gib
    assert admission["safe_processes"] == 2


def test_explicit_cap_cannot_be_silently_exceeded(monkeypatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        workload_calibration.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"available": 20 * gib})(),
    )

    with pytest.raises(SpecValidationError, match="cannot admit one measured"):
        workload_calibration.calibrated_admission(
            probe_peak_rss_bytes=3 * gib,
            requested_cpu_processes=8,
            memory_cap_bytes=2 * gib,
            coordinator_rss_bytes=1 * gib,
        )
