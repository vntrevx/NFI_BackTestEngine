from __future__ import annotations

import os
from copy import deepcopy

import pytest
from nfi_backtest_engine import hardware
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.hardware import (
    GIB,
    current_resource_limits,
    derive_tuning,
    execution_environment,
    hardware_fingerprint,
    inspect_hardware,
    tuning_environment,
)


def _hardware() -> dict:
    return {
        "platform": "Windows-test",
        "machine": "AMD64",
        "cpu_name": "Test CPU",
        "physical_cpu_count": 6,
        "logical_cpu_count": 12,
        "affinity_cpu_count": 12,
        "affinity_cpu_ids": list(range(12)),
        "memory": {
            "total_bytes": 64 * GIB,
            "available_bytes": 48 * GIB,
        },
    }


def test_host_limits_contain_no_guessed_workload_peaks() -> None:
    limits = derive_tuning(_hardware())

    assert limits == {
        "memory_cap_bytes": None,
        "cpu_process_limit": 6,
    }


def test_hardware_fingerprint_ignores_available_memory_drift() -> None:
    first = _hardware()
    second = deepcopy(first)
    second["memory"]["available_bytes"] = 4 * GIB

    assert hardware_fingerprint(first) == hardware_fingerprint(second)


def test_tuning_accepts_an_explicit_positive_memory_cap() -> None:
    limits = derive_tuning(_hardware(), memory_cap_bytes=512 * 1024**2)

    assert limits["memory_cap_bytes"] == 512 * 1024**2


def test_fixed_peak_inputs_are_rejected() -> None:
    with pytest.raises(SpecValidationError, match="workload calibration"):
        derive_tuning(_hardware(), observed_indicator_worker_peak_bytes=4 * GIB)


def test_numeric_libraries_are_single_threaded_inside_pair_processes() -> None:
    environment = tuning_environment({"nested_numeric_threads": 1})

    assert environment["POLARS_MAX_THREADS"] == "1"
    assert environment["RAYON_NUM_THREADS"] == "1"
    assert environment["OMP_NUM_THREADS"] == "1"


def test_profile_records_an_explicit_disk_spool_without_defaulting_one(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(hardware, "inspect_hardware", lambda _workspace=None: _hardware())
    default_profile = hardware.create_execution_profile(tmp_path / "default.json")
    configured_profile = hardware.create_execution_profile(
        tmp_path / "configured.json",
        spool_directory=tmp_path,
    )

    assert hardware.SPOOL_DIRECTORY_ENVIRONMENT not in default_profile["environment"]
    assert configured_profile["environment"][hardware.SPOOL_DIRECTORY_ENVIRONMENT] == str(
        tmp_path.resolve()
    )


def test_current_limits_use_live_available_memory() -> None:
    host = _hardware()
    profile = {
        "schema_version": hardware.EXECUTION_PROFILE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "hardware_fingerprint": hardware_fingerprint(host),
        "hardware": host,
        "limits": {
            "memory_cap_bytes": 32 * GIB,
            "cpu_process_limit": 6,
        },
        "runtime": {
            "portfolio_simulator_threads": 1,
            "nested_numeric_threads": 1,
        },
        "environment": tuning_environment({"nested_numeric_threads": 1}),
    }

    limits = current_resource_limits(profile, hardware=host)

    assert limits["working_memory_bytes"] == 32 * GIB
    assert limits["cpu_process_limit"] == 6


def test_execution_environment_is_scoped(monkeypatch) -> None:
    monkeypatch.setenv("OMP_NUM_THREADS", "outside")

    with execution_environment({"OMP_NUM_THREADS": "1", "NFI_TEST_LIMIT": "4"}):
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["NFI_TEST_LIMIT"] == "4"

    assert os.environ["OMP_NUM_THREADS"] == "outside"
    assert "NFI_TEST_LIMIT" not in os.environ


def test_hardware_inspection_allows_platforms_without_cpu_frequency(
    monkeypatch,
    tmp_path,
) -> None:
    """macOS psutil builds can omit cpu_freq entirely."""
    monkeypatch.setattr(hardware.psutil, "cpu_freq", None, raising=False)

    inspected = inspect_hardware(tmp_path)

    assert inspected["cpu_frequency_mhz"] == {
        "current": None,
        "minimum": None,
        "maximum": None,
    }
