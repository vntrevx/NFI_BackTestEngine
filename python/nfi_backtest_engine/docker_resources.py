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


def derive_docker_policy(
    daemon: dict[str, Any],
    *,
    memory_cap_bytes: int | None = None,
) -> dict[str, Any]:
    """Reserve daemon headroom and permit only one memory-heavy container at a time."""
    if daemon.get("schema_version") != DOCKER_RESOURCE_VERSION:
        raise SpecValidationError("unsupported Docker resource record")
    total = _positive_int(daemon.get("total_memory_bytes"), "Docker total memory")
    active_memory = _nonnegative_int(
        daemon.get("active_container_memory_bytes", 0),
        "active Docker container memory",
    )
    if memory_cap_bytes is not None and memory_cap_bytes < GIB:
        raise SpecValidationError("Docker memory cap must be at least 1 GiB")

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
    return {
        "schema_version": DOCKER_RESOURCE_VERSION,
        "execution_mode": "sequential",
        "maximum_parallel_containers": 1,
        "daemon_total_memory_bytes": total,
        "daemon_reserve_bytes": reserve,
        "active_container_memory_bytes": active_memory,
        "container_memory_limit_bytes": working_memory,
        "memory_limit_enforced": bool(daemon.get("memory_limit_supported")),
        "swap_limit_enforced": bool(
            daemon.get("memory_limit_supported") and daemon.get("swap_limit_supported")
        ),
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
