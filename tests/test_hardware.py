from __future__ import annotations

from copy import deepcopy

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.hardware import GIB, derive_tuning, hardware_fingerprint


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


def test_tuning_preserves_global_loop_and_host_headroom() -> None:
    tuning = derive_tuning(_hardware())

    assert tuning["portfolio_simulator_threads"] == 1
    assert tuning["reserved_physical_cores"] == 1
    assert tuning["working_memory_bytes"] == 8 * GIB
    assert tuning["indicator_threads"] == 10
    assert tuning["independent_engine_jobs"] == 5
    assert tuning["independent_reference_jobs"] == 1


def test_hardware_fingerprint_ignores_available_memory_drift() -> None:
    first = _hardware()
    second = deepcopy(first)
    second["memory"]["available_bytes"] = 4 * GIB

    assert hardware_fingerprint(first) == hardware_fingerprint(second)


def test_tuning_rejects_too_small_memory_cap() -> None:
    with pytest.raises(SpecValidationError, match="at least 1 GiB"):
        derive_tuning(_hardware(), memory_cap_bytes=512 * 1024**2)
