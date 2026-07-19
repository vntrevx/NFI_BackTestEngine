"""Deterministic, prefix-bounded evidence for the first parity mismatch."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .fixture import sha256_file
from .parity import ParityDifference
from .state_trace import TraceDifference, iter_trace_records

_TRADE_INDEX = re.compile(r"^\$\.trades\[(\d+)\]")


def create_mismatch_replay(
    destination: str | Path,
    *,
    fixture_id: str,
    manifest_path: str | Path,
    simulation_input_path: str | Path,
    expected_surface_path: str | Path,
    actual_surface_path: str | Path,
    trade_difference: ParityDifference | None,
    state_difference: TraceDifference | None,
    expected_trace_path: str | Path | None = None,
    actual_trace_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write the smallest deterministic prefix needed to inspect the first mismatch."""
    replay_root = Path(destination).resolve()
    if replay_root.exists() and any(replay_root.iterdir()):
        raise ValueError(f"mismatch replay directory must be empty: {replay_root}")
    replay_root.mkdir(parents=True, exist_ok=True)

    source_input = read_json(simulation_input_path)
    expected_surface = read_json(expected_surface_path)
    actual_surface = read_json(actual_surface_path)
    cutoff_ms = _cutoff_timestamp(
        source_input,
        expected_surface,
        actual_surface,
        trade_difference,
        state_difference,
    )
    replay_input = _prefix_input(source_input, cutoff_ms)
    replay_input_path = replay_root / "simulation-input.json"
    write_json(replay_input_path, replay_input)

    files = [replay_input_path]
    difference_document: dict[str, Any] = {
        "schema_version": "1.0.0",
        "fixture_id": fixture_id,
        "cutoff_timestamp_ms": cutoff_ms,
        "trade_surface": _trade_difference_document(trade_difference),
        "state_trace": _state_difference_document(state_difference),
    }
    if trade_difference is not None:
        expected_fragment_path = replay_root / "expected-trade-fragment.json"
        actual_fragment_path = replay_root / "actual-trade-fragment.json"
        write_json(
            expected_fragment_path,
            _surface_fragment(expected_surface, trade_difference),
        )
        write_json(
            actual_fragment_path,
            _surface_fragment(actual_surface, trade_difference),
        )
        files.extend((expected_fragment_path, actual_fragment_path))

    if state_difference is not None and state_difference.sequence is not None:
        if expected_trace_path is not None:
            expected_event = _trace_event(expected_trace_path, state_difference.sequence)
            if expected_event is not None:
                expected_event_path = replay_root / "expected-state-event.json"
                write_json(expected_event_path, expected_event)
                files.append(expected_event_path)
        if actual_trace_path is not None:
            actual_event = _trace_event(actual_trace_path, state_difference.sequence)
            if actual_event is not None:
                actual_event_path = replay_root / "actual-state-event.json"
                write_json(actual_event_path, actual_event)
                files.append(actual_event_path)

    difference_path = replay_root / "difference.json"
    write_json(difference_path, difference_document)
    files.append(difference_path)
    manifest_file = Path(manifest_path).resolve()
    candle_count = sum(len(pair["candles"]) for pair in replay_input["pairs"])
    replay_manifest = {
        "schema_version": "1.0.0",
        "fixture_id": fixture_id,
        "source": {
            "manifest_path": str(manifest_file),
            "manifest_sha256": sha256_file(manifest_file),
            "simulation_input_sha256": sha256_file(simulation_input_path),
        },
        "prefix": {
            "through_timestamp_ms": cutoff_ms,
            "pair_count": len(replay_input["pairs"]),
            "candle_count": candle_count,
        },
        "reproduce": {
            "working_directory": str(replay_root),
            "command": [
                "nfi-bte",
                "engine",
                "run",
                "simulation-input.json",
                "--output",
                "simulation-result.json",
                "--events",
                "engine-events.jsonl",
            ],
        },
        "artifacts": [_artifact_record(replay_root, path) for path in files],
    }
    replay_manifest_path = replay_root / "replay.json"
    write_json(replay_manifest_path, replay_manifest)
    return {
        "path": str(replay_root),
        "manifest_path": str(replay_manifest_path),
        "manifest_sha256": sha256_file(replay_manifest_path),
        "cutoff_timestamp_ms": cutoff_ms,
        "candle_count": candle_count,
    }


def _cutoff_timestamp(
    simulation_input: dict[str, Any],
    expected_surface: dict[str, Any],
    actual_surface: dict[str, Any],
    trade_difference: ParityDifference | None,
    state_difference: TraceDifference | None,
) -> int:
    if state_difference is not None and state_difference.event_key is not None:
        timestamp = state_difference.event_key.get("timestamp_ms")
        if isinstance(timestamp, int):
            return timestamp
    if trade_difference is not None:
        match = _TRADE_INDEX.match(trade_difference.path)
        if match is not None:
            index = int(match.group(1))
            timestamps = []
            for surface in (expected_surface, actual_surface):
                trades = surface.get("trades", [])
                if index < len(trades):
                    timestamp = trades[index].get("close_timestamp_ms")
                    if isinstance(timestamp, int):
                        timestamps.append(timestamp)
            if timestamps:
                return max(timestamps)
    return max(
        candle["timestamp_ms"] for pair in simulation_input["pairs"] for candle in pair["candles"]
    )


def _prefix_input(document: dict[str, Any], cutoff_ms: int) -> dict[str, Any]:
    pairs = []
    for pair in document["pairs"]:
        candles = [candle for candle in pair["candles"] if candle["timestamp_ms"] <= cutoff_ms]
        if candles:
            pairs.append({**pair, "candles": candles})
    return {
        **document,
        "pairs": pairs,
    }


def _surface_fragment(
    surface: dict[str, Any],
    difference: ParityDifference,
) -> dict[str, Any]:
    match = _TRADE_INDEX.match(difference.path)
    if match is not None:
        index = int(match.group(1))
        trades = surface.get("trades", [])
        return {
            "path": difference.path,
            "trade_index": index,
            "trade": trades[index] if index < len(trades) else {"missing": True},
        }
    if difference.path.startswith("$.summary"):
        return {
            "path": difference.path,
            "summary": surface.get("summary"),
        }
    return {
        "path": difference.path,
        "surface": surface,
    }


def _trace_event(path: str | Path, sequence: int) -> dict[str, Any] | None:
    for record in iter_trace_records(path):
        if record.get("kind") == "event" and record.get("sequence") == sequence:
            return record
    return None


def _trade_difference_document(
    difference: ParityDifference | None,
) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": _json_value(difference.expected),
        "actual": _json_value(difference.actual),
        "reason": difference.reason,
    }


def _state_difference_document(
    difference: TraceDifference | None,
) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "sequence": difference.sequence,
        "path": difference.path,
        "expected": _json_value(difference.expected),
        "actual": _json_value(difference.actual),
        "reason": difference.reason,
        "event_key": difference.event_key,
    }


def _json_value(value: Any) -> Any:
    if type(value).__name__ == "_Missing":
        return {"missing": True}
    return value


def _artifact_record(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
