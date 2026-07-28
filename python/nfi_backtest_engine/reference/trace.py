"""Reference parity and state-trace difference records."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parity_difference_record(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
    }


def _trace_difference_record(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "sequence": difference.sequence,
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
        "event_key": difference.event_key,
    }


def _utc_string(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
