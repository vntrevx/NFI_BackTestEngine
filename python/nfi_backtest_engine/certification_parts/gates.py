"""Pure certification parity gates."""

from __future__ import annotations

from typing import Any


def _full_state_equal(performance: dict[str, Any]) -> bool:
    reports: list[Any] = []
    for lane in ("engine", "reference"):
        lane_record = performance.get(lane)
        runs = lane_record.get("runs") if isinstance(lane_record, dict) else None
        if not isinstance(runs, list) or not runs:
            return False
        reports.extend(
            run.get("report") if isinstance(run, dict) else None
            for run in runs
        )
    return all(
        isinstance(report, dict)
        and report.get("verification_level", report.get("trace_mode")) == "full"
        and isinstance(report.get("parity"), dict)
        and isinstance(report["parity"].get("state_trace"), dict)
        and report["parity"]["state_trace"].get("equal") is True
        for report in reports
    )
