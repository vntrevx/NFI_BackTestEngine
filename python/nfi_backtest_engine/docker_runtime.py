"""Portable resource policy and lifecycle control for managed Docker workloads."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .docker_resources import (
    derive_docker_policy,
    docker_executable,
    inspect_docker_daemon,
)
from .errors import BenchmarkError, SpecValidationError

MANAGED_LABEL = "io.nfi-backtest-engine.managed=true"
ROLE_LABEL_PREFIX = "io.nfi-backtest-engine.role="
_LOCK_PATH = Path(tempfile.gettempdir()) / "nfi-bte-docker-runtime.lock"


@contextmanager
def managed_docker_run(
    *,
    docker_config: str | Path,
    role: str,
    memory_cap_bytes: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a guarded ``docker run`` prefix and always reclaim its exact container."""
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not role or any(character not in allowed for character in role):
        raise SpecValidationError(
            "managed Docker role must use lowercase letters, digits, or hyphens"
        )

    with _docker_runtime_lock():
        config = Path(docker_config)
        daemon = inspect_docker_daemon(docker_config=config)
        policy = derive_docker_policy(daemon, memory_cap_bytes=memory_cap_bytes)
        cleaned = cleanup_stopped_managed_containers(docker_config=config)
        active = list_managed_containers(docker_config=config, all_containers=False)
        if active:
            names = ", ".join(item["id"] for item in active)
            raise BenchmarkError(
                "another managed Docker workload is still running "
                f"({names}); wait for it or stop it explicitly"
            )

        with tempfile.TemporaryDirectory(prefix="nfi-bte-container-") as temporary:
            cidfile = Path(temporary) / "container.cid"
            prefix = [
                docker_executable(),
                "--config",
                str(config),
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--label",
                MANAGED_LABEL,
                "--label",
                f"{ROLE_LABEL_PREFIX}{role}",
            ]
            if policy["memory_limit_enforced"]:
                limit = str(policy["container_memory_limit_bytes"])
                prefix.extend(["--memory", limit])
                if policy["swap_limit_enforced"]:
                    prefix.extend(["--memory-swap", limit])
            try:
                yield {
                    "command_prefix": prefix,
                    "daemon": daemon,
                    "policy": policy,
                    "cleaned_stopped_containers": cleaned,
                }
            finally:
                _force_remove_cid(docker_config=config, cidfile=cidfile)


def run_managed_container(
    arguments: list[str],
    *,
    docker_config: str | Path,
    role: str,
    memory_cap_bytes: int | None = None,
    cwd: str | Path | None = None,
    text: bool = True,
    encoding: str | None = "utf-8",
    errors: str | None = "replace",
    capture_output: bool = False,
    stdout: int | TextIO | BinaryIO | None = None,
    stderr: int | TextIO | BinaryIO | None = None,
    timeout: int | None = None,
) -> tuple[subprocess.CompletedProcess[Any], dict[str, Any]]:
    """Execute one managed container and return the daemon policy used for it."""
    with managed_docker_run(
        docker_config=docker_config,
        role=role,
        memory_cap_bytes=memory_cap_bytes,
    ) as lease:
        command = [*lease["command_prefix"], *arguments]
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=text,
                encoding=encoding if text else None,
                errors=errors if text else None,
                capture_output=capture_output,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout,
            )
        except OSError as exc:
            raise BenchmarkError(f"cannot execute Docker: {exc}") from exc
        return completed, {
            "daemon": lease["daemon"],
            "policy": lease["policy"],
            "cleaned_stopped_containers": lease["cleaned_stopped_containers"],
            "command": command,
        }


def list_managed_containers(
    *,
    docker_config: str | Path,
    all_containers: bool = True,
) -> list[dict[str, str]]:
    """List only containers carrying this project's ownership label."""
    command = [
        docker_executable(),
        "--config",
        str(Path(docker_config)),
        "container",
        "ls",
    ]
    if all_containers:
        command.append("--all")
    command.extend(
        [
            "--filter",
            f"label={MANAGED_LABEL}",
            "--format",
            "{{json .}}",
        ]
    )
    completed = _run_short(command)
    if completed.returncode != 0:
        raise BenchmarkError(
            "cannot inspect managed Docker containers: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    records: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("Docker returned an invalid managed-container record") from exc
        if not isinstance(item, dict):
            raise BenchmarkError("Docker managed-container record must be an object")
        container_id = item.get("ID")
        if not isinstance(container_id, str) or not container_id:
            raise BenchmarkError("Docker managed-container record has no ID")
        records.append(
            {
                "id": container_id,
                "name": str(item.get("Names", "")),
                "status": str(item.get("Status", "")),
                "state": str(item.get("State", "")),
            }
        )
    return records


def cleanup_stopped_managed_containers(*, docker_config: str | Path) -> list[str]:
    """Remove stopped containers owned by this project and leave every other container alone."""
    stopped = [
        item
        for item in list_managed_containers(docker_config=docker_config)
        if item["state"].lower() not in {"running", "restarting", "paused"}
    ]
    identifiers = [item["id"] for item in stopped]
    if not identifiers:
        return []
    completed = _run_short(
        [
            docker_executable(),
            "--config",
            str(Path(docker_config)),
            "container",
            "rm",
            *identifiers,
        ]
    )
    if completed.returncode != 0:
        raise BenchmarkError(
            "cannot remove stopped managed Docker containers: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return identifiers


@contextmanager
def _docker_runtime_lock() -> Iterator[None]:
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK_PATH.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            _lock_file(handle)
        except OSError as exc:
            raise BenchmarkError(
                "another NFI Backtest Engine Docker workload is active; "
                "managed Docker runs are intentionally sequential"
            ) from exc
        try:
            yield
        finally:
            _unlock_file(handle)


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _force_remove_cid(*, docker_config: Path, cidfile: Path) -> None:
    if not cidfile.is_file():
        return
    container_id = cidfile.read_text(encoding="utf-8").strip()
    if not container_id:
        return
    _run_short(
        [
            docker_executable(),
            "--config",
            str(docker_config),
            "container",
            "rm",
            "--force",
            container_id,
        ]
    )


def _run_short(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"cannot execute Docker management command: {exc}") from exc
