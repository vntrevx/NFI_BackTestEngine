"""Hardware inspection and conservative one-time execution-profile tuning."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .canonical import read_json, write_json
from .errors import SpecValidationError

GIB = 1024**3
MIB = 1024**2
EXECUTION_PROFILE_VERSION = "1.2.0"
DEFAULT_INDICATOR_WORKER_PEAK_BYTES = 3 * GIB


def inspect_hardware(workspace: str | Path | None = None) -> dict[str, Any]:
    """Read the resources visible to this process, including affinity constraints."""
    memory = psutil.virtual_memory()
    process = psutil.Process()
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    try:
        affinity = process.cpu_affinity()
    except (AttributeError, psutil.Error):
        affinity = list(range(logical))
    frequency = psutil.cpu_freq()
    target = Path(workspace or Path.cwd()).resolve()
    disk = psutil.disk_usage(target.anchor or str(target))
    cpu_name = platform.processor().strip() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown")
    return {
        "schema_version": "1.0.0",
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu_name": cpu_name,
        "physical_cpu_count": physical,
        "logical_cpu_count": logical,
        "affinity_cpu_count": len(affinity),
        "affinity_cpu_ids": affinity,
        "cpu_frequency_mhz": {
            "current": frequency.current if frequency else None,
            "minimum": frequency.min if frequency else None,
            "maximum": frequency.max if frequency else None,
        },
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "available_ratio": memory.available / memory.total,
        },
        "workspace_disk": {
            "path": str(target),
            "total_bytes": disk.total,
            "free_bytes": disk.free,
        },
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }


def derive_tuning(
    hardware: dict[str, Any],
    *,
    memory_cap_bytes: int | None = None,
    observed_indicator_worker_peak_bytes: int | None = None,
    observed_engine_peak_bytes: int | None = None,
    observed_reference_peak_bytes: int | None = None,
) -> dict[str, Any]:
    """Derive explicit pools while preserving one physical core and host memory."""
    logical = _positive_int(hardware, "logical_cpu_count")
    physical = _positive_int(hardware, "physical_cpu_count")
    affinity = _positive_int(hardware, "affinity_cpu_count")
    available = _positive_int(hardware["memory"], "available_bytes")
    total = _positive_int(hardware["memory"], "total_bytes")

    host_reserve = max(2 * GIB, min(8 * GIB, total * 15 // 100))
    if memory_cap_bytes is not None and memory_cap_bytes < GIB:
        raise SpecValidationError("execution memory cap must be at least 1 GiB")
    selected_memory_cap = memory_cap_bytes or max(GIB, total - host_reserve)
    currently_usable = max(512 * MIB, available - host_reserve)
    working_memory = min(selected_memory_cap, currently_usable)
    physical_budget = max(1, min(physical - 1 if physical > 2 else physical, affinity))
    indicator_peak = observed_indicator_worker_peak_bytes or DEFAULT_INDICATOR_WORKER_PEAK_BYTES
    if indicator_peak <= 0:
        raise SpecValidationError("observed indicator worker peak memory must be positive")
    indicator_processes = max(1, min(physical_budget, working_memory // indicator_peak))
    indicator_processes = min(indicator_processes, logical)

    engine_peak = observed_engine_peak_bytes or GIB
    reference_peak = observed_reference_peak_bytes or 24 * GIB
    if engine_peak <= 0 or reference_peak <= 0:
        raise SpecValidationError("observed peak memory values must be positive")
    engine_jobs = max(1, min(physical_budget, working_memory // engine_peak))
    reference_jobs = max(1, min(physical_budget, working_memory // reference_peak))
    research_jobs = max(1, min(physical_budget, working_memory // indicator_peak))

    return {
        "memory_cap_bytes": selected_memory_cap,
        "host_reserve_bytes": host_reserve,
        "working_memory_bytes": working_memory,
        "reserved_physical_cores": max(0, physical - physical_budget),
        "indicator_processes": int(indicator_processes),
        "portfolio_simulator_threads": 1,
        "independent_research_jobs": int(research_jobs),
        "independent_engine_jobs": int(engine_jobs),
        "independent_reference_jobs": int(reference_jobs),
        "assumed_indicator_worker_peak_bytes": indicator_peak,
        "assumed_engine_peak_bytes": engine_peak,
        "assumed_reference_peak_bytes": reference_peak,
        "nested_numeric_threads": 1,
    }


def create_execution_profile(
    destination: str | Path,
    *,
    workspace: str | Path | None = None,
    memory_cap_bytes: int | None = None,
    observed_indicator_worker_peak_bytes: int | None = None,
    observed_engine_peak_bytes: int | None = None,
    observed_reference_peak_bytes: int | None = None,
) -> dict[str, Any]:
    """Inspect, tune, and persist a hardware-bound execution profile."""
    hardware = inspect_hardware(workspace)
    tuning = derive_tuning(
        hardware,
        memory_cap_bytes=memory_cap_bytes,
        observed_indicator_worker_peak_bytes=observed_indicator_worker_peak_bytes,
        observed_engine_peak_bytes=observed_engine_peak_bytes,
        observed_reference_peak_bytes=observed_reference_peak_bytes,
    )
    profile = {
        "schema_version": EXECUTION_PROFILE_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "hardware_fingerprint": hardware_fingerprint(hardware),
        "hardware": hardware,
        "tuning": tuning,
        "environment": tuning_environment(tuning),
    }
    validate_execution_profile(profile, current_hardware=hardware)
    write_json(destination, profile)
    return profile


def ensure_execution_profile(
    destination: str | Path,
    *,
    workspace: str | Path | None = None,
    memory_cap_bytes: int | None = None,
    observed_indicator_worker_peak_bytes: int | None = None,
    observed_engine_peak_bytes: int | None = None,
    observed_reference_peak_bytes: int | None = None,
) -> dict[str, Any]:
    """Reuse a hardware-bound profile or safely recalibrate it when the host changed."""
    path = Path(destination).resolve()
    if path.is_file():
        try:
            return load_execution_profile(path)
        except SpecValidationError:
            pass
    return create_execution_profile(
        path,
        workspace=workspace,
        memory_cap_bytes=memory_cap_bytes,
        observed_indicator_worker_peak_bytes=observed_indicator_worker_peak_bytes,
        observed_engine_peak_bytes=observed_engine_peak_bytes,
        observed_reference_peak_bytes=observed_reference_peak_bytes,
    )


def load_execution_profile(
    source: str | Path,
    *,
    require_current_hardware: bool = True,
) -> dict[str, Any]:
    profile = read_json(source)
    current = inspect_hardware(Path(source).resolve().parent) if require_current_hardware else None
    validate_execution_profile(profile, current_hardware=current)
    return profile


def validate_execution_profile(
    profile: Any,
    *,
    current_hardware: dict[str, Any] | None = None,
) -> None:
    if not isinstance(profile, dict):
        raise SpecValidationError("execution profile must be an object")
    required = {
        "schema_version",
        "created_at",
        "hardware_fingerprint",
        "hardware",
        "tuning",
        "environment",
    }
    if set(profile) != required:
        raise SpecValidationError("execution profile fields differ from the v1 contract")
    if profile["schema_version"] != EXECUTION_PROFILE_VERSION:
        raise SpecValidationError(
            f"unsupported execution profile version: {profile['schema_version']!r}"
        )
    if hardware_fingerprint(profile["hardware"]) != profile["hardware_fingerprint"]:
        raise SpecValidationError("execution profile hardware fingerprint is corrupt")
    tuning = profile["tuning"]
    tuning_fields = {
        "memory_cap_bytes",
        "host_reserve_bytes",
        "working_memory_bytes",
        "reserved_physical_cores",
        "indicator_processes",
        "portfolio_simulator_threads",
        "independent_research_jobs",
        "independent_engine_jobs",
        "independent_reference_jobs",
        "assumed_indicator_worker_peak_bytes",
        "assumed_engine_peak_bytes",
        "assumed_reference_peak_bytes",
        "nested_numeric_threads",
    }
    if not isinstance(tuning, dict) or set(tuning) != tuning_fields:
        raise SpecValidationError("execution profile tuning fields differ from v1.2")
    for key in tuning_fields:
        if not isinstance(tuning.get(key), int) or isinstance(tuning.get(key), bool):
            raise SpecValidationError(f"execution profile tuning.{key} must be an integer")
        if tuning[key] < 0:
            raise SpecValidationError(f"execution profile tuning.{key} cannot be negative")
    for key in tuning_fields - {"reserved_physical_cores"}:
        if tuning[key] == 0:
            raise SpecValidationError(f"execution profile tuning.{key} must be positive")
    if tuning["portfolio_simulator_threads"] != 1:
        raise SpecValidationError("global chronological simulator must use exactly one thread")
    if not isinstance(profile["environment"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in profile["environment"].items()
    ):
        raise SpecValidationError("execution profile environment must map strings to strings")
    if current_hardware is not None:
        current_fingerprint = hardware_fingerprint(current_hardware)
        if current_fingerprint != profile["hardware_fingerprint"]:
            raise SpecValidationError(
                "execution profile belongs to different hardware; run `nfi-bte system tune` again"
            )
        current_available = _positive_int(current_hardware["memory"], "available_bytes")
        current_usable = max(512 * MIB, current_available - tuning["host_reserve_bytes"])
        revalidation_margin = max(256 * MIB, tuning["working_memory_bytes"] // 20)
        if tuning["working_memory_bytes"] > current_usable + revalidation_margin:
            raise SpecValidationError(
                "execution profile exceeds current free memory; recalibrate before running"
            )


def hardware_fingerprint(hardware: dict[str, Any]) -> str:
    """Hash stable resource identity, excluding free/used memory and frequency."""
    stable = {
        "platform": hardware["platform"],
        "machine": hardware["machine"],
        "cpu_name": hardware["cpu_name"],
        "physical_cpu_count": hardware["physical_cpu_count"],
        "logical_cpu_count": hardware["logical_cpu_count"],
        "affinity_cpu_ids": hardware["affinity_cpu_ids"],
        "total_memory_bytes": hardware["memory"]["total_bytes"],
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def tuning_environment(tuning: dict[str, Any]) -> dict[str, str]:
    indicators = str(tuning["indicator_processes"])
    nested = str(tuning["nested_numeric_threads"])
    return {
        "NFI_INDICATOR_PROCESSES": indicators,
        "NFI_RESEARCH_JOBS": str(tuning["independent_research_jobs"]),
        "NFI_ENGINE_JOBS": str(tuning["independent_engine_jobs"]),
        "NFI_REFERENCE_JOBS": str(tuning["independent_reference_jobs"]),
        "NFI_MEMORY_BUDGET_BYTES": str(tuning["working_memory_bytes"]),
        "POLARS_MAX_THREADS": nested,
        "RAYON_NUM_THREADS": nested,
        "OMP_NUM_THREADS": nested,
        "OPENBLAS_NUM_THREADS": nested,
        "MKL_NUM_THREADS": nested,
        "MALLOC_ARENA_MAX": "2",
    }


@contextmanager
def execution_environment(environment: Mapping[str, str]) -> Iterator[None]:
    """Apply numeric-thread limits while child processes are spawned.

    Spawned vector workers inherit this environment before importing NumPy,
    Polars, or TA-Lib. Restoring it afterwards keeps unrelated commands in the
    long-lived parent process untouched.
    """
    previous = {key: os.environ.get(key) for key in environment}
    try:
        os.environ.update(environment)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _positive_int(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecValidationError(f"hardware {key} must be a positive integer")
    return value
