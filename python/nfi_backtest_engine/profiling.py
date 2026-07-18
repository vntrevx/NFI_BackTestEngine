"""Low-overhead JSONL spans for Phase 0 component timings."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import SpecValidationError

PROFILE_ENV = "NFI_BTE_PROFILE_EVENTS"
PROFILE_PHASES = ("indicators", "callbacks", "trade_scans", "event_simulation")
_write_lock = threading.Lock()


@contextmanager
def profile_phase(phase: str, *, events_path: str | Path | None = None) -> Iterator[None]:
    """Record one named span when a profile events path is configured."""
    if phase not in PROFILE_PHASES:
        raise ValueError(f"unsupported profile phase: {phase}")
    destination = Path(events_path) if events_path else _configured_path()
    started_wall = datetime.now(timezone.utc)
    started_ns = time.perf_counter_ns()
    try:
        yield
    finally:
        if destination is not None:
            event = {
                "schema_version": "1.0.0",
                "phase": phase,
                "started_at": started_wall.isoformat().replace("+00:00", "Z"),
                "duration_ns": time.perf_counter_ns() - started_ns,
                "process_id": os.getpid(),
                "thread_id": threading.get_ident(),
            }
            _append_event(destination, event)


def aggregate_profile_events(path: str | Path) -> dict[str, Any]:
    """Aggregate valid profile events without hiding missing required phases."""
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "total_ns": 0, "max_ns": 0}
    )
    seen_phases: set[str] = set()
    source = Path(path)
    if not source.is_file():
        raise SpecValidationError(f"profile event file does not exist: {source}")

    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SpecValidationError(
                    f"{source}:{line_number}: invalid JSON profile event"
                ) from exc
            phase = event.get("phase")
            duration = event.get("duration_ns")
            if phase not in PROFILE_PHASES:
                raise SpecValidationError(
                    f"{source}:{line_number}: unsupported profile phase {phase!r}"
                )
            if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
                raise SpecValidationError(
                    f"{source}:{line_number}: duration_ns must be a non-negative integer"
                )
            calls = event.get("calls", 1)
            if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
                raise SpecValidationError(
                    f"{source}:{line_number}: calls must be a non-negative integer"
                )
            max_duration = event.get("max_duration_ns", duration)
            if (
                not isinstance(max_duration, int)
                or isinstance(max_duration, bool)
                or max_duration < 0
            ):
                raise SpecValidationError(
                    f"{source}:{line_number}: max_duration_ns must be a non-negative integer"
                )
            if calls == 0 and (duration != 0 or max_duration != 0):
                raise SpecValidationError(
                    f"{source}:{line_number}: a zero-call event must have zero durations"
                )
            if calls > 0 and max_duration > duration:
                raise SpecValidationError(
                    f"{source}:{line_number}: max_duration_ns cannot exceed duration_ns"
                )
            aggregate = totals[phase]
            seen_phases.add(phase)
            aggregate["calls"] += calls
            aggregate["total_ns"] += duration
            aggregate["max_ns"] = max(aggregate["max_ns"], max_duration)

    phases = {
        phase: {
            **totals[phase],
            "total_seconds": totals[phase]["total_ns"] / 1_000_000_000,
            "max_seconds": totals[phase]["max_ns"] / 1_000_000_000,
        }
        for phase in PROFILE_PHASES
        if phase in seen_phases
    }
    return {
        "schema_version": "1.0.0",
        "phases": phases,
        "missing_phases": [phase for phase in PROFILE_PHASES if phase not in phases],
    }


def aggregate_profile_file(source: str | Path, destination: str | Path) -> dict[str, Any]:
    report = aggregate_profile_events(source)
    write_json(destination, report)
    return report


def _configured_path() -> Path | None:
    configured = os.environ.get(PROFILE_ENV)
    return Path(configured) if configured else None


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with _write_lock, path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{encoded}\n")
