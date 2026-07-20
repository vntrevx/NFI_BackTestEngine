"""Resource discovery and budgeting for Docker Desktop and native Docker daemons."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import BenchmarkError, SpecValidationError

GIB = 1024**3
DOCKER_RESOURCE_VERSION = "1.0.0"
_MEMORY_VALUE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[kKMGT]?i?B)$")
_MEMORY_MULTIPLIERS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "KiB": 1024,
    "MB": 1000**2,
    "MiB": 1024**2,
    "GB": 1000**3,
    "GiB": 1024**3,
    "TB": 1000**4,
    "TiB": 1024**4,
}


def docker_executable() -> str:
    """Return a testable command name while preserving clear execution errors."""
    return shutil.which("docker") or "docker"


def inspect_docker_daemon(
    *,
    docker_config: str | Path,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Read resources exposed by the daemon, not the host running Docker Desktop."""
    docker = docker_executable()
    try:
        completed = subprocess.run(
            [
                docker,
                "--config",
                str(Path(docker_config)),
                "info",
                "--format",
                "{{json .}}",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"cannot inspect Docker daemon resources: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker info failed"
        raise BenchmarkError(f"cannot inspect Docker daemon resources: {detail}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("Docker daemon returned invalid resource metadata") from exc
    if not isinstance(document, dict):
        raise BenchmarkError("Docker daemon resource metadata must be an object")
    total_memory = _positive_int(document.get("MemTotal"), "Docker MemTotal")
    cpus = _positive_int(document.get("NCPU"), "Docker NCPU")
    active_count, active_memory = _inspect_active_container_usage(
        docker=docker,
        docker_config=Path(docker_config),
        timeout_seconds=timeout_seconds,
    )
    return {
        "schema_version": DOCKER_RESOURCE_VERSION,
        "server_version": _optional_string(document.get("ServerVersion")),
        "operating_system": _optional_string(document.get("OperatingSystem")),
        "os_type": _optional_string(document.get("OSType")),
        "architecture": _optional_string(document.get("Architecture")),
        "cpu_count": cpus,
        "total_memory_bytes": total_memory,
        "active_container_count": active_count,
        "active_container_memory_bytes": active_memory,
        "memory_limit_supported": document.get("MemoryLimit") is True,
        "swap_limit_supported": document.get("SwapLimit") is True,
    }


def inspect_docker_swap_capacity(
    *,
    docker_config: str | Path,
    image: str,
    timeout_seconds: int = 30,
) -> int:
    """Return swap visible to a container in the selected Docker daemon.

    Docker Desktop runs Linux containers inside a VM.  Host pagefile or swap
    information is therefore not a safe proxy for what a reference backtest can
    use.  This short, network-disabled probe reads ``/proc/meminfo`` through the
    same daemon and image platform as the real workload.
    """
    if not image:
        raise SpecValidationError("Docker swap probe image must be non-empty")
    command = [
        docker_executable(),
        "--config",
        str(Path(docker_config)),
        "run",
        "--rm",
        "--network",
        "none",
        "--label",
        "io.nfi-backtest-engine.managed=true",
        "--label",
        "io.nfi-backtest-engine.role=resource-probe",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "awk '/^SwapTotal:/ { print $2 * 1024; found=1 } END { if (!found) exit 1 }' "
        "/proc/meminfo",
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"cannot inspect Docker daemon swap: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "swap probe failed"
        raise BenchmarkError(f"cannot inspect Docker daemon swap: {detail}")
    value = completed.stdout.strip()
    try:
        swap_bytes = int(float(value))
    except ValueError as exc:
        raise BenchmarkError(f"Docker swap probe returned an invalid value: {value!r}") from exc
    if swap_bytes < 0:
        raise BenchmarkError("Docker swap probe returned a negative capacity")
    return swap_bytes


def derive_docker_policy(
    daemon: dict[str, Any],
    *,
    memory_cap_bytes: int | None = None,
    swap_mode: str = "disabled",
    daemon_swap_bytes: int | None = None,
    swap_cap_bytes: int | None = None,
) -> dict[str, Any]:
    """Reserve daemon headroom and permit only one memory-heavy container at a time.

    ``disabled`` preserves the normal low-risk policy by setting Docker's total
    memory+swap limit equal to the RAM limit.  ``daemon`` is intentionally
    explicit and is reserved for release certification workloads that cannot be
    timerange-split without changing portfolio semantics.
    """
    if daemon.get("schema_version") != DOCKER_RESOURCE_VERSION:
        raise SpecValidationError("unsupported Docker resource record")
    if swap_mode not in {"disabled", "daemon"}:
        raise SpecValidationError("Docker swap mode must be 'disabled' or 'daemon'")
    total = _positive_int(daemon.get("total_memory_bytes"), "Docker total memory")
    active_memory = _nonnegative_int(
        daemon.get("active_container_memory_bytes", 0),
        "active Docker container memory",
    )
    if memory_cap_bytes is not None and memory_cap_bytes < GIB:
        raise SpecValidationError("Docker memory cap must be at least 1 GiB")
    if swap_cap_bytes is not None and swap_cap_bytes < 0:
        raise SpecValidationError("Docker swap cap must be non-negative")

    # Docker Desktop runs a daemon, filesystem cache, and networking services in
    # the same VM. A proportional reserve scales from small CI VMs to workstations,
    # while the upper bound avoids wasting memory on large native Linux hosts.
    reserve = max(GIB, min(6 * GIB, total // 5))
    automatic_budget = total - reserve - active_memory
    if automatic_budget < GIB:
        raise SpecValidationError(
            "less than 1 GiB remains after Docker daemon reserve and active containers"
        )
    working_memory = min(automatic_budget, memory_cap_bytes or automatic_budget)
    swap_supported = bool(
        daemon.get("memory_limit_supported") and daemon.get("swap_limit_supported")
    )
    detected_swap = 0 if daemon_swap_bytes is None else _nonnegative_int(
        daemon_swap_bytes,
        "Docker daemon swap",
    )
    if swap_mode == "daemon":
        if not swap_supported:
            raise SpecValidationError(
                "Docker daemon does not support the memory+swap limit required "
                "for certification mode"
            )
        if daemon_swap_bytes is None:
            raise SpecValidationError(
                "certification swap mode requires measured Docker daemon swap"
            )
        if detected_swap == 0:
            raise SpecValidationError(
                "certification swap mode requested but Docker exposes no swap"
            )
        permitted_swap = min(detected_swap, swap_cap_bytes or detected_swap)
    else:
        permitted_swap = 0
    memory_swap_limit = working_memory + permitted_swap
    return {
        "schema_version": DOCKER_RESOURCE_VERSION,
        "execution_mode": "sequential",
        "maximum_parallel_containers": 1,
        "daemon_total_memory_bytes": total,
        "daemon_reserve_bytes": reserve,
        "active_container_memory_bytes": active_memory,
        "container_memory_limit_bytes": working_memory,
        "swap_mode": swap_mode,
        "daemon_swap_bytes": detected_swap,
        "container_swap_limit_bytes": permitted_swap,
        # Docker's --memory-swap value is the combined RAM and swap limit.
        "container_memory_swap_limit_bytes": memory_swap_limit,
        "memory_limit_enforced": bool(daemon.get("memory_limit_supported")),
        "swap_limit_enforced": swap_supported,
    }


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BenchmarkError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BenchmarkError(f"{label} must be a non-negative integer")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _inspect_active_container_usage(
    *,
    docker: str,
    docker_config: Path,
    timeout_seconds: int,
) -> tuple[int, int]:
    try:
        completed = subprocess.run(
            [
                docker,
                "--config",
                str(docker_config),
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"cannot inspect active Docker memory: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker stats failed"
        raise BenchmarkError(f"cannot inspect active Docker memory: {detail}")
    count = 0
    total = 0
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("Docker returned invalid active-memory metadata") from exc
        if not isinstance(item, dict) or not isinstance(item.get("MemUsage"), str):
            raise BenchmarkError("Docker active-memory metadata has no MemUsage")
        used = item["MemUsage"].split("/", 1)[0].strip()
        total += _parse_memory_value(used)
        count += 1
    return count, total


def _parse_memory_value(value: str) -> int:
    match = _MEMORY_VALUE.fullmatch(value)
    if match is None:
        raise BenchmarkError(f"unsupported Docker memory value: {value!r}")
    multiplier = _MEMORY_MULTIPLIERS.get(match.group("unit"))
    if multiplier is None:
        raise BenchmarkError(f"unsupported Docker memory unit: {match.group('unit')!r}")
    return int(float(match.group("number")) * multiplier)
