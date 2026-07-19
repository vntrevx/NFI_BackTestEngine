"""Hardware inspection and measured execution-capacity profiles.

The host profile records only facts and explicit user caps.  Strategy-specific
memory peaks and process counts belong to ``workload-calibration`` records
created from real full-range work, never to machine-independent GiB guesses.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import psutil

from .canonical import read_json, write_json
from .errors import SpecValidationError

GIB = 1024**3
MIB = 1024**2
EXECUTION_PROFILE_VERSION = "2.0.0"
LEGACY_EXECUTION_PROFILE_VERSION = "1.2.0"
SPOOL_DIRECTORY_ENVIRONMENT = "NFI_BTE_SPOOL_DIRECTORY"


class ResolvedResourceLimits(TypedDict):
    memory_cap_bytes: int | None
    working_memory_bytes: int
    cpu_process_limit: int


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
    frequency = _cpu_frequency()
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
    """Return factual host limits without inventing per-workload memory peaks.

    The ``observed_*`` arguments remain in the Python signature so v0.4 callers
    receive an actionable migration error instead of a ``TypeError``.
    """
    if any(
        value is not None
        for value in (
            observed_indicator_worker_peak_bytes,
            observed_engine_peak_bytes,
            observed_reference_peak_bytes,
        )
    ):
        raise SpecValidationError(
            "fixed peak-memory inputs were removed; run a workload calibration instead"
        )
    logical = _positive_int(hardware, "logical_cpu_count")
    physical = _positive_int(hardware, "physical_cpu_count")
    affinity = _positive_int(hardware, "affinity_cpu_count")
    if memory_cap_bytes is not None and memory_cap_bytes <= 0:
        raise SpecValidationError("execution memory cap must be positive")
    return {
        "memory_cap_bytes": memory_cap_bytes,
        "cpu_process_limit": min(logical, physical, affinity),
    }


def create_execution_profile(
    destination: str | Path,
    *,
    workspace: str | Path | None = None,
    memory_cap_bytes: int | None = None,
    observed_indicator_worker_peak_bytes: int | None = None,
    observed_engine_peak_bytes: int | None = None,
    observed_reference_peak_bytes: int | None = None,
    spool_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect, tune, and persist a hardware-bound execution profile."""
    hardware = inspect_hardware(workspace)
    limits = derive_tuning(
        hardware,
        memory_cap_bytes=memory_cap_bytes,
        observed_indicator_worker_peak_bytes=observed_indicator_worker_peak_bytes,
        observed_engine_peak_bytes=observed_engine_peak_bytes,
        observed_reference_peak_bytes=observed_reference_peak_bytes,
    )
    environment = tuning_environment({"nested_numeric_threads": 1})
    if spool_directory is not None:
        spool = Path(spool_directory).resolve()
        if not spool.is_dir():
            raise SpecValidationError(f"engine spool directory does not exist: {spool}")
        environment[SPOOL_DIRECTORY_ENVIRONMENT] = str(spool)
    profile = {
        "schema_version": EXECUTION_PROFILE_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "hardware_fingerprint": hardware_fingerprint(hardware),
        "hardware": hardware,
        "limits": limits,
        "runtime": {
            "portfolio_simulator_threads": 1,
            "nested_numeric_threads": 1,
        },
        "environment": environment,
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
    spool_directory: str | Path | None = None,
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
        spool_directory=spool_directory,
    )


def load_execution_profile(
    source: str | Path,
    *,
    require_current_hardware: bool = True,
) -> dict[str, Any]:
    profile = read_json(source)
    current = inspect_hardware(Path(source).resolve().parent) if require_current_hardware else None
    if (
        isinstance(profile, dict)
        and profile.get("schema_version") == LEGACY_EXECUTION_PROFILE_VERSION
    ):
        _validate_legacy_execution_profile(profile, current_hardware=current)
        return _migrate_legacy_execution_profile(profile)
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
        "limits",
        "runtime",
        "environment",
    }
    if set(profile) != required:
        raise SpecValidationError("execution profile fields differ from the v2 contract")
    if profile["schema_version"] != EXECUTION_PROFILE_VERSION:
        raise SpecValidationError(
            f"unsupported execution profile version: {profile['schema_version']!r}"
        )
    if hardware_fingerprint(profile["hardware"]) != profile["hardware_fingerprint"]:
        raise SpecValidationError("execution profile hardware fingerprint is corrupt")
    limits = profile["limits"]
    if not isinstance(limits, dict) or set(limits) != {
        "memory_cap_bytes",
        "cpu_process_limit",
    }:
        raise SpecValidationError("execution profile limits differ from v2")
    memory_cap = limits["memory_cap_bytes"]
    if memory_cap is not None and (
        not isinstance(memory_cap, int) or isinstance(memory_cap, bool) or memory_cap <= 0
    ):
        raise SpecValidationError("execution profile memory cap must be null or positive")
    cpu_limit = limits["cpu_process_limit"]
    if not isinstance(cpu_limit, int) or isinstance(cpu_limit, bool) or cpu_limit <= 0:
        raise SpecValidationError("execution profile CPU process limit must be positive")
    runtime = profile["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {
        "portfolio_simulator_threads",
        "nested_numeric_threads",
    }:
        raise SpecValidationError("execution profile runtime fields differ from v2")
    if runtime["portfolio_simulator_threads"] != 1:
        raise SpecValidationError("global chronological simulator must use exactly one thread")
    if runtime["nested_numeric_threads"] != 1:
        raise SpecValidationError("pair workers must use one nested numeric thread")
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


def current_resource_limits(
    profile: dict[str, Any],
    *,
    hardware: dict[str, Any] | None = None,
) -> ResolvedResourceLimits:
    """Resolve a profile against current free memory immediately before work."""
    current = hardware if hardware is not None else inspect_hardware()
    validate_execution_profile(profile, current_hardware=current)
    available = _positive_int(current["memory"], "available_bytes")
    cap = profile["limits"]["memory_cap_bytes"]
    working = min(available, cap) if cap is not None else available
    return {
        "memory_cap_bytes": cap,
        "working_memory_bytes": working,
        "cpu_process_limit": min(
            int(profile["limits"]["cpu_process_limit"]),
            _positive_int(current, "affinity_cpu_count"),
        ),
    }


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
    nested = str(tuning["nested_numeric_threads"])
    return {
        "POLARS_MAX_THREADS": nested,
        "RAYON_NUM_THREADS": nested,
        "OMP_NUM_THREADS": nested,
        "OPENBLAS_NUM_THREADS": nested,
        "MKL_NUM_THREADS": nested,
    }


def _validate_legacy_execution_profile(
    profile: dict[str, Any],
    *,
    current_hardware: dict[str, Any] | None,
) -> None:
    required = {
        "schema_version",
        "created_at",
        "hardware_fingerprint",
        "hardware",
        "tuning",
        "environment",
    }
    if set(profile) != required:
        raise SpecValidationError("legacy execution profile fields differ from v1.2")
    if hardware_fingerprint(profile["hardware"]) != profile["hardware_fingerprint"]:
        raise SpecValidationError("legacy execution profile hardware fingerprint is corrupt")
    if current_hardware is not None and (
        hardware_fingerprint(current_hardware) != profile["hardware_fingerprint"]
    ):
        raise SpecValidationError(
            "execution profile belongs to different hardware; run `nfi-bte system tune` again"
        )
    tuning = profile.get("tuning")
    if not isinstance(tuning, dict):
        raise SpecValidationError("legacy execution profile tuning must be an object")


def _migrate_legacy_execution_profile(profile: dict[str, Any]) -> dict[str, Any]:
    hardware = profile["hardware"]
    legacy_tuning = profile["tuning"]
    legacy_cap = legacy_tuning.get("memory_cap_bytes")
    cap = (
        legacy_cap
        if isinstance(legacy_cap, int) and not isinstance(legacy_cap, bool) and legacy_cap > 0
        else None
    )
    limits = derive_tuning(hardware, memory_cap_bytes=cap)
    return {
        "schema_version": EXECUTION_PROFILE_VERSION,
        "created_at": profile["created_at"],
        "hardware_fingerprint": profile["hardware_fingerprint"],
        "hardware": hardware,
        "limits": limits,
        "runtime": {
            "portfolio_simulator_threads": 1,
            "nested_numeric_threads": 1,
        },
        "environment": tuning_environment({"nested_numeric_threads": 1}),
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


def _cpu_frequency() -> Any | None:
    """Return frequency information only on platforms where psutil exposes it."""
    reader = getattr(psutil, "cpu_freq", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except (NotImplementedError, psutil.Error):
        return None
