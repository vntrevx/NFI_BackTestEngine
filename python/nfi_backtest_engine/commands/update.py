"""Installed CLI update command."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from .. import __version__
from ..errors import NfiBacktestError
from ..update_check import LatestRelease, fetch_latest_release, is_newer_release

COMMAND_NAMES = frozenset({"update"})
DOWNLOAD_TIMEOUT_SECONDS = 30.0


def _is_source_checkout() -> bool:
    repository_root = Path(__file__).resolve().parents[3]
    return (repository_root / "pyproject.toml").is_file() and (
        repository_root / ".git"
    ).exists()


def _wheel_suffix() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "manylinux2014_x86_64.whl"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "manylinux2014_aarch64.whl"
    if system == "Darwin" and machine == "arm64":
        return "macosx_11_0_arm64.whl"
    raise NfiBacktestError(f"no release wheel is available for {system} {machine}")


def _download_release_wheel(release: LatestRelease, destination: Path) -> Path:
    suffix = _wheel_suffix()
    matching = [asset for asset in release.assets if asset.name.endswith(suffix)]
    if len(matching) != 1:
        raise NfiBacktestError(
            f"expected one {suffix} wheel in v{release.version}; found {len(matching)}"
        )
    asset = matching[0]
    headers = {"User-Agent": "nfi-bte updater"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(asset.download_url, headers=headers)
    wheel_path = destination / asset.name
    hasher = hashlib.sha256()
    with (
        urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
        wheel_path.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            hasher.update(chunk)
    if hasher.hexdigest() != asset.sha256:
        wheel_path.unlink(missing_ok=True)
        raise NfiBacktestError("downloaded wheel SHA-256 differs from the GitHub release")
    return wheel_path


def _select_upgrade_command(wheel_path: Path) -> list[str]:
    executable = Path(sys.executable).resolve().as_posix().lower()
    uv = shutil.which("uv")
    if uv is not None and "/uv/tools/" in executable:
        return [
            uv,
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            str(wheel_path),
        ]

    pipx = shutil.which("pipx")
    if pipx is not None and "/pipx/venvs/" in executable:
        return [pipx, "install", "--force", str(wheel_path)]

    if uv is not None:
        return [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            str(wheel_path),
        ]
    return [sys.executable, "-m", "pip", "install", "--upgrade", str(wheel_path)]


def execute(args: object) -> int:
    """Upgrade an installed CLI through the environment's package manager."""
    if getattr(args, "command_name", None) != "update":
        raise AssertionError("unhandled update command")
    if _is_source_checkout():
        raise NfiBacktestError(
            "self-update is unavailable in a source checkout; update the checkout "
            "and run `uv sync --extra dev --frozen`"
        )

    try:
        release = fetch_latest_release()
    except (OSError, ValueError) as exc:
        raise NfiBacktestError(f"could not read the latest GitHub release: {exc}") from exc
    newer = is_newer_release(release.version, __version__)
    if getattr(args, "check", False):
        if newer:
            print(f"Update available: {__version__} -> {release.version}.")
        else:
            print(f"Already up to date: {__version__}.")
        return 0
    if not newer:
        print(f"Already up to date: {__version__}.")
        return 0

    with tempfile.TemporaryDirectory(prefix="nfi-bte-update-") as temporary_directory:
        wheel_path = _download_release_wheel(release, Path(temporary_directory))
        command = _select_upgrade_command(wheel_path)
        completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise NfiBacktestError("update failed; the installed version was left unchanged")

    print(f"Updated to NFI Backtest Engine {release.version}.")
    return 0
