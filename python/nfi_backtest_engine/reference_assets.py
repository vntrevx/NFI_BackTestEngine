"""Locate Python assets mounted into the pinned Freqtrade container."""

from __future__ import annotations

from pathlib import Path

from .errors import BenchmarkError


def reference_package_root() -> Path:
    """Return only the engine package directory needed by the tracer.

    Mounting the surrounding ``site-packages`` directory would expose host
    binary dependencies to the pinned Freqtrade container. Those wheels may
    target a different Python or operating system, so only this package is
    mounted at ``/nfi-python/nfi_backtest_engine``.
    """

    root = Path(__file__).resolve().parent
    if not (root / "state_trace.py").is_file():
        raise BenchmarkError(f"packaged reference support is incomplete: {root}")
    return root


def reference_tracer_root() -> Path:
    """Return the packaged sitecustomize tracer directory."""

    root = Path(__file__).resolve().parent / "reference_tracer"
    required = (root / "sitecustomize.py", root / "nfi_reference_trace.py")
    if not all(path.is_file() for path in required):
        raise BenchmarkError(f"packaged Freqtrade reference tracer is incomplete: {root}")
    return root
