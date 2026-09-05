"""Supported host boundary for product execution."""

from __future__ import annotations

import os
import platform
import re

from .errors import BenchmarkError

NATIVE_WINDOWS_UNSUPPORTED_MESSAGE = (
    "native Windows is unsupported; run nfi-bte under WSL2 (Linux)"
)
SUPPORTED_EXECUTION_SYSTEMS = frozenset({"darwin", "linux"})
_WSL2_KERNEL_RELEASE = re.compile(
    r"^[0-9][0-9a-z._+~]*-microsoft-standard(?:-wsl2)?$",
    re.IGNORECASE,
)


def is_wsl2_kernel_release(release: object) -> bool:
    """Recognize the kernel identity emitted by genuine standard WSL2 guests."""
    return isinstance(release, str) and _WSL2_KERNEL_RELEASE.fullmatch(release) is not None


def current_execution_platform_identity() -> dict[str, str | int | bool | None]:
    """Return the supported host identity, rejecting ambiguous Microsoft kernels."""
    system = platform.system().lower()
    if os.name == "nt" or system == "windows":
        raise BenchmarkError(NATIVE_WINDOWS_UNSUPPORTED_MESSAGE)
    if system not in SUPPORTED_EXECUTION_SYSTEMS:
        raise BenchmarkError(f"unsupported execution platform: {system}")
    kernel_release = platform.release()
    wsl2 = system == "linux" and is_wsl2_kernel_release(kernel_release)
    if system == "linux" and "microsoft" in kernel_release.lower() and not wsl2:
        raise BenchmarkError(
            "Microsoft Linux kernel is not verified WSL2; use an official "
            "microsoft-standard WSL2 kernel"
        )
    return {
        "system": system,
        "kernel_release": kernel_release,
        "wsl": wsl2,
        "wsl_version": 2 if wsl2 else None,
    }


def require_supported_execution_platform() -> None:
    """Reject execution on hosts outside the supported Linux/macOS boundary."""
    current_execution_platform_identity()
