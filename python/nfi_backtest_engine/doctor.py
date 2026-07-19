"""Read-only environment checks for reproducible engine and reference runs."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .docker_resources import (
    derive_docker_policy,
    inspect_docker_daemon,
)
from .docker_runtime import list_managed_containers
from .hardware import inspect_hardware, load_execution_profile
from .reference_runtime import (
    REFERENCE_IMAGE_REF,
    REFERENCE_PLATFORM_DIGEST,
    ensure_docker_config,
)


def run_doctor(
    *,
    workspace: str | Path | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return checks without pulling images or changing external state."""
    hardware = inspect_hardware(workspace)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "python",
            sys.version_info >= (3, 10),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    checks.append(
        _check(
            "memory",
            hardware["memory"]["available_bytes"] >= 2 * 1024**3,
            f"{hardware['memory']['available_bytes']} bytes available",
        )
    )
    docker_checks, docker_runtime = _docker_checks()
    checks.extend(docker_checks)

    profile: dict[str, Any] | None = None
    if profile_path is not None:
        try:
            profile = load_execution_profile(profile_path)
            checks.append(_check("execution_profile", True, "current hardware fingerprint matches"))
        except Exception as exc:  # expected command report, not a traceback boundary
            checks.append(_check("execution_profile", False, str(exc)))

    return {
        "schema_version": "1.1.0",
        "healthy": all(item["status"] != "error" for item in checks),
        "hardware": hardware,
        "docker": docker_runtime,
        "execution_profile": profile,
        "checks": checks,
    }


def _docker_checks() -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    docker = shutil.which("docker")
    if docker is None:
        return [_check("docker_cli", False, "Docker CLI is not on PATH")], None
    config = ensure_docker_config()
    try:
        version = subprocess.run(
            [docker, "--config", str(config), "version", "--format", "{{.Server.Version}}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            [
                _check("docker_cli", True, docker),
                _check("docker_server", False, str(exc)),
            ],
            None,
        )
    checks = [
        _check("docker_cli", True, docker),
        _check(
            "docker_server",
            version.returncode == 0,
            version.stdout.strip() or version.stderr.strip(),
        ),
    ]
    if version.returncode != 0:
        return checks, None
    try:
        daemon = inspect_docker_daemon(docker_config=config)
        policy = derive_docker_policy(daemon)
        managed = list_managed_containers(docker_config=config)
    except Exception as exc:  # doctor converts expected environment failures to checks
        checks.append(_check("docker_resources", False, str(exc)))
        return checks, None
    checks.append(
        _check(
            "docker_resources",
            policy["container_memory_limit_bytes"] >= 2 * 1024**3,
            (
                f"{daemon['total_memory_bytes']} bytes visible, "
                f"{policy['container_memory_limit_bytes']} bytes per managed workload"
            ),
        )
    )
    active = [
        item
        for item in managed
        if item["state"].lower() in {"running", "restarting", "paused"}
    ]
    checks.append(
        _check(
            "managed_containers",
            not active,
            (
                "none active"
                if not active
                else "active: " + ", ".join(item["id"] for item in active)
            ),
            warning=True,
        )
    )
    try:
        inspect = subprocess.run(
            [
                docker,
                "--config",
                str(config),
                "image",
                "inspect",
                REFERENCE_IMAGE_REF,
                "--format",
                "{{.Id}}",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks.append(_check("reference_image", False, str(exc), warning=True))
        return checks, {"daemon": daemon, "policy": policy, "managed_containers": managed}
    image_id = inspect.stdout.strip()
    checks.append(
        _check(
            "reference_image",
            inspect.returncode == 0 and image_id == REFERENCE_PLATFORM_DIGEST,
            image_id or "pinned image is not present locally",
            warning=True,
        )
    )
    return checks, {"daemon": daemon, "policy": policy, "managed_containers": managed}


def _check(
    name: str,
    success: bool,
    detail: str,
    *,
    warning: bool = False,
) -> dict[str, str]:
    return {
        "name": name,
        "status": "ok" if success else ("warning" if warning else "error"),
        "detail": detail,
    }
