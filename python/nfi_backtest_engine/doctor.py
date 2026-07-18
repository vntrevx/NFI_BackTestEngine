"""Read-only environment checks for reproducible engine and reference runs."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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
    checks.extend(_docker_checks())

    profile: dict[str, Any] | None = None
    if profile_path is not None:
        try:
            profile = load_execution_profile(profile_path)
            checks.append(_check("execution_profile", True, "current hardware fingerprint matches"))
        except Exception as exc:  # expected command report, not a traceback boundary
            checks.append(_check("execution_profile", False, str(exc)))

    return {
        "schema_version": "1.0.0",
        "healthy": all(item["status"] != "error" for item in checks),
        "hardware": hardware,
        "execution_profile": profile,
        "checks": checks,
    }


def _docker_checks() -> list[dict[str, str]]:
    docker = shutil.which("docker")
    if docker is None:
        return [_check("docker_cli", False, "Docker CLI is not on PATH")]
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
        return [
            _check("docker_cli", True, docker),
            _check("docker_server", False, str(exc)),
        ]
    checks = [
        _check("docker_cli", True, docker),
        _check(
            "docker_server",
            version.returncode == 0,
            version.stdout.strip() or version.stderr.strip(),
        ),
    ]
    if version.returncode != 0:
        return checks
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
    image_id = inspect.stdout.strip()
    checks.append(
        _check(
            "reference_image",
            inspect.returncode == 0 and image_id == REFERENCE_PLATFORM_DIGEST,
            image_id or "pinned image is not present locally",
            warning=True,
        )
    )
    return checks


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
