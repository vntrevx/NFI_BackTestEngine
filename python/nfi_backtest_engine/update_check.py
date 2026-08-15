"""Low-overhead latest-release checks for the CLI."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO
from urllib.request import Request, urlopen

PYPI_JSON_URL = "https://pypi.org/pypi/nfi-backtest-engine/json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
NETWORK_TIMEOUT_SECONDS = 1.0
_RELEASE_PATTERN = re.compile(r"^\d+(?:\.\d+)*")


def _release_tuple(version: str) -> tuple[int, ...]:
    match = _RELEASE_PATTERN.match(version)
    if match is None:
        raise ValueError(f"unsupported version: {version}")
    return tuple(int(part) for part in match.group().split("."))


def _is_newer(latest_version: str, current_version: str) -> bool:
    latest = _release_tuple(latest_version)
    current = _release_tuple(current_version)
    width = max(len(latest), len(current))
    return latest + (0,) * (width - len(latest)) > current + (0,) * (width - len(current))


def _read_cached_version(cache_path: Path, *, now_epoch: float) -> str | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    checked_at = payload.get("checked_at")
    latest_version = payload.get("latest_version")
    if not isinstance(checked_at, (int, float)) or not isinstance(latest_version, str):
        return None
    age = now_epoch - float(checked_at)
    if not 0 <= age < CHECK_INTERVAL_SECONDS:
        return None
    return latest_version


def _write_cached_version(cache_path: Path, *, now_epoch: float, latest_version: str) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(f"{cache_path.suffix}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "checked_at": now_epoch,
                "latest_version": latest_version,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)


def fetch_latest_version() -> str:
    """Read the latest published version from PyPI."""
    request = Request(
        PYPI_JSON_URL,
        headers={"User-Agent": "nfi-bte update-check"},
    )
    with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        payload: object = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("PyPI returned a non-object response")
    info = payload.get("info")
    if not isinstance(info, dict):
        raise ValueError("PyPI response has no info object")
    latest_version = info.get("version")
    if not isinstance(latest_version, str):
        raise ValueError("PyPI response has no version")
    _release_tuple(latest_version)
    return latest_version


def available_update_notice(
    current_version: str,
    *,
    cache_path: Path,
    now_epoch: float,
    fetch_latest: Callable[[], str],
) -> str | None:
    """Return a notice only when PyPI has a newer stable release."""
    latest_version = _read_cached_version(cache_path, now_epoch=now_epoch)
    if latest_version is None:
        latest_version = fetch_latest()
        _write_cached_version(
            cache_path,
            now_epoch=now_epoch,
            latest_version=latest_version,
        )
    if not _is_newer(latest_version, current_version):
        return None
    return (
        f"Update available: {current_version} -> {latest_version}. "
        "Run `nfi-bte update`."
    )


def _cache_path(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_CACHE_HOME")
    cache_root = Path(configured) if configured else Path.home() / ".cache"
    return cache_root / "nfi-backtest-engine" / "update-check.json"


def _is_source_checkout() -> bool:
    repository_root = Path(__file__).resolve().parents[2]
    return (repository_root / "pyproject.toml").is_file() and (
        repository_root / ".git"
    ).exists()


def maybe_print_update_notice(
    current_version: str,
    *,
    environment: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Print a cached daily update notice without changing command status."""
    active_environment = os.environ if environment is None else environment
    output = sys.stderr if stderr is None else stderr
    if (
        active_environment.get("NFI_BTE_DISABLE_UPDATE_CHECK") == "1"
        or active_environment.get("CI")
        or _is_source_checkout()
    ):
        return

    try:
        notice = available_update_notice(
            current_version,
            cache_path=_cache_path(active_environment),
            now_epoch=time.time(),
            fetch_latest=fetch_latest_version,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"warning: update check failed: {exc}", file=output)
        return
    if notice is not None:
        print(notice, file=output)
