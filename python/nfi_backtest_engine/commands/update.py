"""Installed CLI update command."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import NfiBacktestError

COMMAND_NAMES = frozenset({"update"})
PACKAGE_NAME = "nfi-backtest-engine"


def _is_source_checkout() -> bool:
    repository_root = Path(__file__).resolve().parents[3]
    return (repository_root / "pyproject.toml").is_file() and (
        repository_root / ".git"
    ).exists()


def _select_upgrade_command() -> list[str]:
    executable = Path(sys.executable).resolve().as_posix().lower()
    uv = shutil.which("uv")
    if uv is not None and "/uv/tools/" in executable:
        return [uv, "tool", "upgrade", PACKAGE_NAME]

    pipx = shutil.which("pipx")
    if pipx is not None and "/pipx/venvs/" in executable:
        return [pipx, "upgrade", PACKAGE_NAME]

    if uv is not None:
        return [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            PACKAGE_NAME,
        ]
    return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME]


def execute(args: object) -> int:
    """Upgrade an installed CLI through the environment's package manager."""
    if getattr(args, "command_name", None) != "update":
        raise AssertionError("unhandled update command")
    if _is_source_checkout():
        raise NfiBacktestError(
            "self-update is unavailable in a source checkout; update the checkout "
            "and run `uv sync --extra dev --frozen`"
        )

    command = _select_upgrade_command()
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise NfiBacktestError("update failed; the installed version was left unchanged")

    print("Update complete.")
    return 0
