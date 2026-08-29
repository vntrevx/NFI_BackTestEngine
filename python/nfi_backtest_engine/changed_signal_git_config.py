"""Stable repository-local config boundary for changed-signal Git authority."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .changed_signal_filesystem_trust import read_stable_file
from .errors import SpecValidationError

_PRIVATE_CONFIG: ContextVar[Path | None] = ContextVar(
    "changed_signal_private_git_config", default=None
)


@contextmanager
def private_git_configuration(git_root: Path) -> Generator[dict[str, list[str]], None, None]:
    """Inspect one stable direct config and activate an empty private Git view."""
    config_path = git_root / ".git/config"
    snapshot = read_stable_file(config_path, config_path)
    with tempfile.TemporaryDirectory(prefix="nfi-changed-signal-git-") as directory:
        private_root = Path(directory)
        direct = private_root / "direct.config"
        safe = private_root / "safe.config"
        _write_private(direct, snapshot.payload)
        _write_private(safe, b"")
        entries = _parse_direct_config(direct)
        _validate_direct_config(entries)
        token = _PRIVATE_CONFIG.set(safe)
        try:
            yield entries
        finally:
            _PRIVATE_CONFIG.reset(token)


def active_private_git_configuration() -> Path | None:
    """Return the private config active for Git authority plumbing, if any."""
    return _PRIVATE_CONFIG.get()


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o400)


def _parse_direct_config(path: Path) -> dict[str, list[str]]:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    try:
        completed = subprocess.run(
            ("git", "config", "--file", path.as_posix(), "--no-includes", "--null", "--list"),
            capture_output=True,
            check=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpecValidationError("changed signal upstream Git config is invalid") from exc
    entries: dict[str, list[str]] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        name, separator, value = record.partition(b"\n")
        if not separator:
            raise SpecValidationError("changed signal upstream Git config is invalid")
        try:
            key = name.decode("utf-8").lower()
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpecValidationError("changed signal upstream Git config is invalid") from exc
        entries.setdefault(key, []).append(decoded)
    return entries


def _validate_direct_config(entries: dict[str, list[str]]) -> None:
    names = tuple(entries)
    if any(
        name == "include.path" or (name.startswith("includeif.") and name.endswith(".path"))
        for name in names
    ):
        raise SpecValidationError("changed signal upstream Git config include is forbidden")
