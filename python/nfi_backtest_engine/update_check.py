"""Low-overhead latest-release checks for the CLI."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.request import Request, urlopen

GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/vntrevx/NFI_BackTestEngine/releases/latest"
)
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
NETWORK_TIMEOUT_SECONDS = 1.0
_RELEASE_PATTERN = re.compile(r"^\d+(?:\.\d+)*")
_STABLE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class ReleaseAsset:
    """One checksum-addressed GitHub release asset."""

    name: str
    download_url: str
    sha256: str


@dataclass(frozen=True)
class LatestRelease:
    """The latest stable GitHub release and its downloadable assets."""

    version: str
    assets: tuple[ReleaseAsset, ...]


def _release_tuple(version: str) -> tuple[int, ...]:
    match = _RELEASE_PATTERN.match(version)
    if match is None:
        raise ValueError(f"unsupported version: {version}")
    return tuple(int(part) for part in match.group().split("."))


def is_newer_release(latest_version: str, current_version: str) -> bool:
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


def parse_latest_release(payload: object) -> LatestRelease:
    """Parse the stable GitHub release contract without guessing."""
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned a non-object release")
    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.startswith("v"):
        raise ValueError("GitHub release has no stable version tag")
    version = tag_name.removeprefix("v")
    if _STABLE_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(f"GitHub latest release tag is not stable: {tag_name}")

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("GitHub release has no asset list")
    assets: list[ReleaseAsset] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise ValueError("GitHub release contains a non-object asset")
        name = raw_asset.get("name")
        download_url = raw_asset.get("browser_download_url")
        digest = raw_asset.get("digest")
        if (
            not isinstance(name, str)
            or not isinstance(download_url, str)
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
        ):
            raise ValueError("GitHub release asset is missing its SHA-256 identity")
        sha256 = digest.removeprefix("sha256:")
        if len(sha256) != 64:
            raise ValueError(f"GitHub release asset has an invalid SHA-256 digest: {name}")
        assets.append(
            ReleaseAsset(
                name=name,
                download_url=download_url,
                sha256=sha256,
            )
        )
    return LatestRelease(version=version, assets=tuple(assets))


def fetch_latest_release() -> LatestRelease:
    """Read the latest checksum-addressed stable release from GitHub."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nfi-bte update-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        GITHUB_LATEST_RELEASE_URL,
        headers=headers,
    )
    with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        payload: object = json.load(response)
    return parse_latest_release(payload)


def fetch_latest_version() -> str:
    """Read the latest published GitHub release version."""
    return fetch_latest_release().version


def available_update_notice(
    current_version: str,
    *,
    cache_path: Path,
    now_epoch: float,
    fetch_latest: Callable[[], str],
) -> str | None:
    """Return a notice only when GitHub has a newer stable release."""
    latest_version = _read_cached_version(cache_path, now_epoch=now_epoch)
    if latest_version is None:
        latest_version = fetch_latest()
        _write_cached_version(
            cache_path,
            now_epoch=now_epoch,
            latest_version=latest_version,
        )
    if not is_newer_release(latest_version, current_version):
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
