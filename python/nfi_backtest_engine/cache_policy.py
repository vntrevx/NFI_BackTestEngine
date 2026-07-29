"""Configurable, filesystem-aware storage budget for reusable cache data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psutil

from .errors import SpecValidationError

CACHE_MAX_BYTES_ENV = "NFI_BTE_CACHE_MAX_BYTES"
DEFAULT_CACHE_CEILING_BYTES = 50 * 1024**3
DEFAULT_TOTAL_STORAGE_DIVISOR = 10
DEFAULT_AVAILABLE_STORAGE_DIVISOR = 4


@dataclass(frozen=True)
class CacheBudget:
    max_bytes: int
    source: str
    filesystem_path: Path
    total_bytes: int
    available_bytes: int


def resolve_cache_budget(
    cache_directory: str | Path,
    *,
    requested_bytes: int | None = None,
) -> CacheBudget:
    """Resolve an explicit, environment, or disk-aware cache ceiling."""
    path = _existing_parent(Path(cache_directory).absolute())
    usage = psutil.disk_usage(str(path))
    available_bytes = int(usage.free)
    total_bytes = int(getattr(usage, "total", available_bytes))

    if requested_bytes is not None:
        maximum = _positive_bytes(requested_bytes, source="requested cache budget")
        source = "explicit"
    else:
        environment_value = os.environ.get(CACHE_MAX_BYTES_ENV)
        if environment_value is not None:
            try:
                parsed = int(environment_value)
            except ValueError as exc:
                raise SpecValidationError(
                    f"{CACHE_MAX_BYTES_ENV} must be a positive integer byte count"
                ) from exc
            maximum = _positive_bytes(parsed, source=CACHE_MAX_BYTES_ENV)
            source = "environment"
        else:
            maximum = max(
                1,
                min(
                    DEFAULT_CACHE_CEILING_BYTES,
                    total_bytes // DEFAULT_TOTAL_STORAGE_DIVISOR,
                    available_bytes // DEFAULT_AVAILABLE_STORAGE_DIVISOR,
                ),
            )
            source = "disk-aware-default"

    return CacheBudget(
        max_bytes=maximum,
        source=source,
        filesystem_path=path,
        total_bytes=total_bytes,
        available_bytes=available_bytes,
    )


def _positive_bytes(value: int, *, source: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise SpecValidationError(f"{source} must be positive")
    return value


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise SpecValidationError(
                f"cannot find a filesystem for cache directory: {path}"
            )
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate.resolve()
