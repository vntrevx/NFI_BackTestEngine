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

from .docker_environment import docker_subprocess_environment
from .docker_resources import (
    derive_docker_policy,
    docker_executable,
    inspect_docker_daemon,
    inspect_docker_swap_capacity,
)
from .errors import BenchmarkError, SpecValidationError

MANAGED_LABEL = "io.nfi-backtest-engine.managed=true"
ROLE_LABEL_PREFIX = "io.nfi-backtest-engine.role="
_LOCK_PATH = Path(tempfile.gettempdir()) / "nfi-bte-docker-runtime.lock"

BIND_OWNER_EXECUTABLE_FUNCTION = """\
run_as_bind_owner() {
  case "${NFI_BIND_UID:-}" in
    ""|*[!0-9]*) echo "invalid NFI_BIND_UID" >&2; return 126 ;;
  esac
  case "${NFI_BIND_GID:-}" in
    ""|*[!0-9]*) echo "invalid NFI_BIND_GID" >&2; return 126 ;;
  esac
  if [ "$(id -u)" != "${NFI_BIND_UID}" ] || [ "$(id -g)" != "${NFI_BIND_GID}" ]; then
    echo "container user differs from declared bind owner" >&2
    return 126
  fi
  nfi_command="$1"
  shift
  nfi_runtime_xdg="$(mktemp -d /tmp/nfi-bind-owner.XXXXXX)" || return 126
  export HOME="${nfi_runtime_xdg}/home"
  export XDG_CACHE_HOME="${nfi_runtime_xdg}/cache"
  export XDG_CONFIG_HOME="${nfi_runtime_xdg}/config"
  export XDG_DATA_HOME="${nfi_runtime_xdg}/data"
  mkdir -p "${HOME}" || return 126
  if [ "${nfi_command}" = "freqtrade" ]; then
    /usr/local/bin/python -m freqtrade "$@"
  else
    nfi_executable="$(command -v "${nfi_command}")" || {
      echo "container executable not found: ${nfi_command}" >&2
      return 127
    }
    "${nfi_executable}" "$@"
  fi
}
"""

RUN_AS_BIND_OWNER_SCRIPT = (
    BIND_OWNER_EXECUTABLE_FUNCTION + 'run_as_bind_owner "$@"\n'
)


def docker_bind_owner_arguments(path: str | Path) -> list[str]:
    """Run a container as the owner of a writable bind mount."""
    owner = Path(path).stat()
    return ["--user", f"{owner.st_uid}:{owner.st_gid}"]


_DOCKER_VALUE_OPTIONS = frozenset(
    {
        "--cidfile",
        "--label",
        "--tmpfs",
        "--cap-drop",
        "--security-opt",
        "--pids-limit",
        "--ulimit",
        "--memory",
        "--memory-swap",
        "--platform",
        "--network",
        "--user",
        "--env",
        "--workdir",
        "--volume",
        "--mount",
        "--entrypoint",
    }
)
_DOCKER_BOOLEAN_OPTIONS = frozenset({"--rm", "--read-only"})
_MANDATORY_TMPFS = [
    "/tmp:rw,noexec,nosuid,nodev",
    "/nfi-deps:rw,exec,nosuid,nodev",
]


def _parse_docker_run_options(
    argv: list[str],
    *,
    image: str | None = None,
) -> dict[str, list[str | None]]:
    """Parse only the Docker option grammar used by managed Oracle commands."""
    if argv.count("run") != 1:
        raise BenchmarkError("managed Docker sandbox requires exactly one run command")
    run_index = argv.index("run")
    global_options = argv[1:run_index]
    if global_options and (
        len(global_options) != 2
        or global_options[0] != "--config"
        or not global_options[1]
    ):
        raise BenchmarkError("managed Docker sandbox has an untrusted daemon selector")
    option_tokens = argv[run_index + 1 :]
    if image is not None:
        if option_tokens.count(image) != 1:
            raise BenchmarkError("managed Docker sandbox has an invalid pinned image")
        option_tokens = option_tokens[: option_tokens.index(image)]
    parsed: dict[str, list[str | None]] = {}
    index = 0
    while index < len(option_tokens):
        flag = option_tokens[index]
        if "=" in flag or not flag.startswith("--"):
            raise BenchmarkError(f"managed Docker sandbox contains unknown option {flag}")
        if flag in _DOCKER_BOOLEAN_OPTIONS:
            parsed.setdefault(flag, []).append(None)
            index += 1
            continue
        if flag not in _DOCKER_VALUE_OPTIONS or index + 1 >= len(option_tokens):
            raise BenchmarkError(f"managed Docker sandbox contains unknown option {flag}")
        value = option_tokens[index + 1]
        if value.startswith("--"):
            raise BenchmarkError(f"managed Docker sandbox option {flag} has no value")
        parsed.setdefault(flag, []).append(value)
        index += 2
    return parsed


def _require_single_option(
    parsed: dict[str, list[str | None]],
    flag: str,
    expected: str | None = None,
) -> str | None:
    values = parsed.get(flag, [])
    if len(values) != 1 or (expected is not None and values[0] != expected):
        raise BenchmarkError(f"managed Docker sandbox has invalid singleton option {flag}")
    return values[0]


def validate_managed_run_prefix(prefix: list[str]) -> None:
    """Accept only the exact typed prefix emitted by ``managed_docker_run``."""
    parsed = _parse_docker_run_options(prefix)
    _require_single_option(parsed, "--rm")
    _require_single_option(parsed, "--cidfile")
    _require_single_option(parsed, "--read-only")
    _require_single_option(parsed, "--cap-drop", "ALL")
    _require_single_option(parsed, "--security-opt", "no-new-privileges=true")
    _require_single_option(parsed, "--pids-limit", "512")
    _require_single_option(parsed, "--ulimit", "nofile=4096:4096")
    memory = _require_single_option(parsed, "--memory")
    memory_swap = _require_single_option(parsed, "--memory-swap")
    try:
        memory_bytes = int(str(memory))
        memory_swap_bytes = int(str(memory_swap))
    except ValueError as exc:
        raise BenchmarkError("managed Docker sandbox resource limits are invalid") from exc
    if memory_bytes < 1024**3 or memory_swap_bytes < memory_bytes:
        raise BenchmarkError("managed Docker sandbox resource limits are weakened")
    if parsed.get("--tmpfs") != _MANDATORY_TMPFS:
        raise BenchmarkError("managed Docker sandbox has invalid tmpfs options")
    labels = parsed.get("--label", [])
    if labels.count(MANAGED_LABEL) != 1 or len(labels) != 2:
        raise BenchmarkError("managed Docker sandbox has invalid ownership labels")
    roles = [
        value
        for value in labels
        if isinstance(value, str) and value.startswith(ROLE_LABEL_PREFIX)
    ]
    if len(roles) != 1 or not roles[0].removeprefix(ROLE_LABEL_PREFIX):
        raise BenchmarkError("managed Docker sandbox has invalid role label")
    allowed = {
        "--rm",
        "--cidfile",
        "--label",
        "--read-only",
        "--tmpfs",
        "--cap-drop",
        "--security-opt",
        "--pids-limit",
        "--ulimit",
        "--memory",
        "--memory-swap",
    }
    unknown = set(parsed) - allowed
    if unknown:
        raise BenchmarkError(
            f"managed Docker sandbox prefix contains forbidden options: {sorted(unknown)}"
        )


def validate_final_managed_command(
    command: list[str],
    *,
    image: str,
    platform: str,
    user: str,
    workdir: str,
    entrypoint: str,
    volumes: list[str],
    mounts: list[str],
    environment: list[str],
) -> None:
    """Validate the complete effective Docker argv immediately before execution."""
    parsed = _parse_docker_run_options(command, image=image)
    validate_managed_run_prefix(command[: command.index("--platform")])
    _require_single_option(parsed, "--platform", platform)
    _require_single_option(parsed, "--network", "none")
    _require_single_option(parsed, "--user", user)
    _require_single_option(parsed, "--workdir", workdir)
    _require_single_option(parsed, "--entrypoint", entrypoint)
    if parsed.get("--volume", []) != volumes:
        raise BenchmarkError("managed Docker sandbox has undeclared or writable bind mounts")
    if parsed.get("--mount", []) != mounts:
        raise BenchmarkError("managed Docker sandbox has undeclared mounts")
    if parsed.get("--env", []) != environment:
        raise BenchmarkError("managed Docker sandbox has undeclared environment variables")
    allowed = {
        "--rm",
        "--cidfile",
        "--label",
        "--read-only",
        "--tmpfs",
        "--cap-drop",
        "--security-opt",
        "--pids-limit",
        "--ulimit",
        "--memory",
        "--memory-swap",
        "--platform",
        "--network",
        "--user",
        "--workdir",
        "--volume",
        "--mount",
        "--env",
        "--entrypoint",
    }
    unknown = set(parsed) - allowed
    if unknown:
        raise BenchmarkError(
            f"managed Docker sandbox command has unknown options: {sorted(unknown)}"
        )


def docker_root_with_bind_owner_arguments(path: str | Path) -> list[str]:
    """Run directly as the writable bind owner without retaining root capabilities."""
    owner = Path(path).stat()
    return [
        "--user",
        f"{owner.st_uid}:{owner.st_gid}",
        "--env",
        f"NFI_BIND_UID={owner.st_uid}",
        "--env",
        f"NFI_BIND_GID={owner.st_gid}",
    ]


@contextmanager
def managed_docker_run(
    *,
    docker_config: str | Path,
    role: str,
    memory_cap_bytes: int | None = None,
    swap_mode: str = "disabled",
    swap_cap_bytes: int | None = None,
    swap_probe_image: str | None = None,
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
        daemon_swap_bytes = None
        if swap_mode == "daemon":
            if swap_probe_image is None:
                raise SpecValidationError(
                    "certification swap mode requires a Docker swap probe image"
                )
            daemon_swap_bytes = inspect_docker_swap_capacity(
                docker_config=config,
                image=swap_probe_image,
            )
        policy = derive_docker_policy(
            daemon,
            memory_cap_bytes=memory_cap_bytes,
            swap_mode=swap_mode,
            daemon_swap_bytes=daemon_swap_bytes,
            swap_cap_bytes=swap_cap_bytes,
        )
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
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev",
                "--tmpfs",
                "/nfi-deps:rw,exec,nosuid,nodev",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "512",
                "--ulimit",
                "nofile=4096:4096",
            ]
            if not policy["memory_limit_enforced"] or not policy["swap_limit_enforced"]:
                raise BenchmarkError(
                    "managed Docker sandbox requires daemon memory and swap limits"
                )
            limit = str(policy["container_memory_limit_bytes"])
            prefix.extend(
                [
                    "--memory",
                    limit,
                    "--memory-swap",
                    str(policy["container_memory_swap_limit_bytes"]),
                ]
            )
            validate_managed_run_prefix(prefix)
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
    swap_mode: str = "disabled",
    swap_cap_bytes: int | None = None,
    swap_probe_image: str | None = None,
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
        swap_mode=swap_mode,
        swap_cap_bytes=swap_cap_bytes,
        swap_probe_image=swap_probe_image,
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
                env=docker_subprocess_environment(),
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
            env=docker_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BenchmarkError(f"cannot execute Docker management command: {exc}") from exc
