"""Measured, content-bound resource calibration for vector workloads."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import psutil

from .canonical import read_json, write_json
from .errors import SpecValidationError

WORKLOAD_CALIBRATION_VERSION = "1.0.0"


class WorkloadAdmission(TypedDict):
    requested_cpu_processes: int
    memory_cap_bytes: int | None
    admitted_memory_bytes: int
    available_memory_bytes: int
    coordinator_rss_bytes: int
    measured_reserve_bytes: int
    safe_processes: int


def calibration_key(identity: dict[str, Any]) -> str:
    """Hash every input that can change preparation memory or runtime shape."""
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def calibration_path(directory: str | Path, key: str) -> Path:
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise SpecValidationError("workload calibration key must be a SHA-256 digest")
    return Path(directory).resolve() / f"{key}.json"


def load_workload_calibration(
    path: str | Path,
    *,
    expected_key: str,
    hardware_fingerprint: str,
) -> dict[str, Any]:
    document = read_json(path)
    validate_workload_calibration(
        document,
        expected_key=expected_key,
        hardware_fingerprint=hardware_fingerprint,
    )
    return document


def create_workload_calibration(
    path: str | Path,
    *,
    key: str,
    identity: dict[str, Any],
    hardware_fingerprint: str,
    probe_pair: str,
    probe_peak_rss_bytes: int,
    probe_wall_time_seconds: float,
    requested_cpu_processes: int,
    memory_cap_bytes: int | None,
    coordinator_rss_bytes: int | None = None,
) -> dict[str, Any]:
    """Derive concurrency from a full-range isolated probe without byte guesses.

    One measured worker peak is retained as the admission reserve.  That reserve
    grows and shrinks with the actual strategy instead of encoding a machine-
    independent GiB constant.
    """
    if probe_peak_rss_bytes <= 0:
        raise SpecValidationError("workload probe peak memory must be positive")
    if requested_cpu_processes <= 0:
        raise SpecValidationError("workload CPU process limit must be positive")
    admission = calibrated_admission(
        probe_peak_rss_bytes=probe_peak_rss_bytes,
        requested_cpu_processes=requested_cpu_processes,
        memory_cap_bytes=memory_cap_bytes,
        coordinator_rss_bytes=coordinator_rss_bytes,
    )
    document = {
        "schema_version": WORKLOAD_CALIBRATION_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "key": key,
        "identity": identity,
        "hardware_fingerprint": hardware_fingerprint,
        "probe": {
            "pair": probe_pair,
            "peak_rss_bytes": probe_peak_rss_bytes,
            "wall_time_seconds": probe_wall_time_seconds,
            "coordinator_rss_bytes": admission["coordinator_rss_bytes"],
            "available_memory_bytes": admission["available_memory_bytes"],
        },
        "decision": {
            **admission,
            "reason": "full-range worst-footprint pair peak and current host capacity",
        },
    }
    validate_workload_calibration(
        document,
        expected_key=key,
        hardware_fingerprint=hardware_fingerprint,
    )
    write_json(path, document)
    return document


def calibrated_admission(
    *,
    probe_peak_rss_bytes: int,
    requested_cpu_processes: int,
    memory_cap_bytes: int | None,
    coordinator_rss_bytes: int | None = None,
) -> WorkloadAdmission:
    """Re-evaluate a measured peak against memory available right now.

    The expensive full-range probe is reusable while its content and hardware
    identity remain valid. Free memory is not reusable evidence, so worker
    admission is recalculated on every invocation.
    """
    if probe_peak_rss_bytes <= 0:
        raise SpecValidationError("workload probe peak memory must be positive")
    if requested_cpu_processes <= 0:
        raise SpecValidationError("workload CPU process limit must be positive")
    coordinator_rss = (
        psutil.Process().memory_info().rss
        if coordinator_rss_bytes is None
        else coordinator_rss_bytes
    )
    if coordinator_rss < 0:
        raise SpecValidationError("coordinator resident memory cannot be negative")
    available = int(psutil.virtual_memory().available)
    admitted_memory = (
        min(available, memory_cap_bytes) if memory_cap_bytes is not None else available
    )
    child_budget = max(0, admitted_memory - coordinator_rss)
    if child_budget < probe_peak_rss_bytes:
        raise SpecValidationError(
            "current memory budget cannot admit one measured vector worker; "
            "free memory or raise the explicit cap"
        )
    # Keep one observed worker peak unallocated. This envelope follows the
    # actual strategy and timerange instead of a fixed percentage or GiB.
    slots_by_memory = max(
        1,
        max(0, child_budget - probe_peak_rss_bytes) // probe_peak_rss_bytes,
    )
    return {
        "requested_cpu_processes": requested_cpu_processes,
        "memory_cap_bytes": memory_cap_bytes,
        "admitted_memory_bytes": admitted_memory,
        "available_memory_bytes": available,
        "coordinator_rss_bytes": coordinator_rss,
        "measured_reserve_bytes": probe_peak_rss_bytes,
        "safe_processes": max(1, min(requested_cpu_processes, slots_by_memory)),
    }


def validate_workload_calibration(
    document: Any,
    *,
    expected_key: str,
    hardware_fingerprint: str,
) -> None:
    if not isinstance(document, dict):
        raise SpecValidationError("workload calibration must be an object")
    if document.get("schema_version") != WORKLOAD_CALIBRATION_VERSION:
        raise SpecValidationError("unsupported workload calibration schema")
    if document.get("key") != expected_key:
        raise SpecValidationError("workload calibration identity changed")
    if document.get("hardware_fingerprint") != hardware_fingerprint:
        raise SpecValidationError("workload calibration belongs to different hardware")
    probe = document.get("probe")
    decision = document.get("decision")
    if not isinstance(probe, dict) or not isinstance(decision, dict):
        raise SpecValidationError("workload calibration probe and decision are required")
    for owner, key in (
        (probe, "peak_rss_bytes"),
        (probe, "available_memory_bytes"),
        (decision, "admitted_memory_bytes"),
        (decision, "measured_reserve_bytes"),
        (decision, "safe_processes"),
    ):
        value = owner.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SpecValidationError(f"workload calibration {key} must be positive")
