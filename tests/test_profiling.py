from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine.profiling import (
    PROFILE_PHASES,
    aggregate_profile_events,
    profile_phase,
)


def test_profile_spans_aggregate_all_required_phases(tmp_path: Path) -> None:
    events = tmp_path / "profile.jsonl"
    for phase in PROFILE_PHASES:
        with profile_phase(phase, events_path=events):
            pass

    report = aggregate_profile_events(events)

    assert report["missing_phases"] == []
    assert list(report["phases"]) == list(PROFILE_PHASES)
    assert all(report["phases"][phase]["calls"] == 1 for phase in PROFILE_PHASES)


def test_profile_event_is_json_lines(tmp_path: Path) -> None:
    events = tmp_path / "profile.jsonl"
    with profile_phase("callbacks", events_path=events):
        pass

    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["phase"] == "callbacks"


def test_profile_aggregates_low_overhead_summary_records(tmp_path: Path) -> None:
    events = tmp_path / "profile.jsonl"
    records = [
        {
            "schema_version": "1.0.0",
            "phase": phase,
            "calls": 0 if phase == "trade_scans" else 5,
            "duration_ns": 0 if phase == "trade_scans" else 100,
            "max_duration_ns": 0 if phase == "trade_scans" else 30,
        }
        for phase in PROFILE_PHASES
    ]
    events.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    report = aggregate_profile_events(events)

    assert report["missing_phases"] == []
    assert report["phases"]["callbacks"]["calls"] == 5
    assert report["phases"]["callbacks"]["total_ns"] == 100
    assert report["phases"]["callbacks"]["max_ns"] == 30
    assert report["phases"]["trade_scans"]["calls"] == 0
