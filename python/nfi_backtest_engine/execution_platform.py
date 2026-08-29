"""Supported host boundary for product execution."""

from __future__ import annotations

import os
import platform

from .errors import BenchmarkError

NATIVE_WINDOWS_UNSUPPORTED_MESSAGE = (
    "native Windows is unsupported; run nfi-bte under WSL2 (Linux)"
)
SUPPORTED_EXECUTION_SYSTEMS = frozenset({"darwin", "linux"})


def require_supported_execution_platform() -> None:
    """Reject execution on hosts outside the supported Linux/macOS boundary."""
    system = platform.system().lower()
    if os.name == "nt" or system == "windows":
        raise BenchmarkError(NATIVE_WINDOWS_UNSUPPORTED_MESSAGE)
    if system not in SUPPORTED_EXECUTION_SYSTEMS:
        raise BenchmarkError(f"unsupported execution platform: {system}")
