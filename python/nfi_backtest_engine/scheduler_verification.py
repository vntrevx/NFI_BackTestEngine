"""Exact event-order verification for the Freqtrade and Native schedulers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .canonical import write_json
from .errors import TraceError
from .fixture import sha256_file, validate_fixture
from .scheduler_contract import load_scheduler_contract
from .specs import SCHEDULER_VERIFICATION_SCHEMA, validate_schema
from .state_trace import iter_validated_trace_events, trace_summary

SCHEDULER_VERIFICATION_VERSION = "scheduler-verification-v1"


def verify_scheduler_events(
    manifest_path: str | Path,
    official_semantic_trace: str | Path,
    native_events: str | Path,
    scheduler_contract_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare only chronological pair-event keys; state parity remains separate."""
    manifest = validate_fixture(manifest_path, validate_trace_semantics=False)
    contract = load_scheduler_contract(scheduler_contract_path)
    official_path = Path(official_semantic_trace)
    native_path = Path(native_events)
    official_summary = trace_summary(official_path)
    if official_summary["source"] != "freqtrade-semantic-observer":
        raise TraceError("scheduler verification requires an official semantic projection")
    if official_summary["profile_sha256"] != contract["semantic_profile_sha256"]:
        raise TraceError("official semantic trace profile differs from scheduler contract")
    if official_summary["trading_mode"] != manifest["freqtrade"]["trading_mode"]:
        raise TraceError("official semantic trace trading mode differs from fixture")

    official_phase = contract["observer"]["official_phase"]
    expected_events = _official_pair_events(official_path, official_phase)
    actual_events = _native_pair_events(native_path)
    event_count = 0
    timestamp_batch_count = 0
    same_timestamp_batch_count = 0
    previous_timestamp: int | None = None
    current_batch_size = 0
    mismatch: dict[str, Any] | None = None
    while True:
        expected = next(expected_events, None)
        actual = next(actual_events, None)
        if expected is None or actual is None:
            if expected != actual:
                mismatch = {
                    "sequence": event_count,
                    "expected": expected,
                    "actual": actual,
                    "reason": "event stream length differs",
                }
            break
        if expected != actual:
            mismatch = {
                "sequence": event_count,
                "expected": expected,
                "actual": actual,
                "reason": "timestamp/pair order differs",
            }
            break
        timestamp = expected["timestamp_ms"]
        if timestamp != previous_timestamp:
            if current_batch_size > 1:
                same_timestamp_batch_count += 1
            timestamp_batch_count += 1
            current_batch_size = 0
            previous_timestamp = timestamp
        current_batch_size += 1
        event_count += 1
    if mismatch is None and current_batch_size > 1:
        same_timestamp_batch_count += 1

    report: dict[str, Any] = {
        "schema_version": SCHEDULER_VERIFICATION_VERSION,
        "fixture_id": manifest["fixture_id"],
        "trading_mode": manifest["freqtrade"]["trading_mode"],
        "scheduler_contract_sha256": contract["fingerprint"],
        "official_trace_sha256": sha256_file(official_path),
        "native_events_sha256": sha256_file(native_path),
        "event_order_exact": mismatch is None,
        "event_count": event_count,
        "timestamp_batch_count": timestamp_batch_count,
        "same_timestamp_batch_count": same_timestamp_batch_count,
        "mismatch": mismatch,
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, SCHEDULER_VERIFICATION_SCHEMA)
    if output_path is not None:
        write_json(output_path, report)
    return report


def _official_pair_events(path: Path, phase: str) -> Iterator[dict[str, Any]]:
    for event in iter_validated_trace_events(path):
        if event["phase"] == phase:
            yield {
                "timestamp_ms": event["timestamp_ms"],
                "pair": event["pair"],
            }


def _native_pair_events(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TraceError(f"{path}:{line_number}: invalid Native event JSON") from exc
                timestamp = event.get("timestamp_ms") if isinstance(event, dict) else None
                pair = event.get("pair") if isinstance(event, dict) else None
                if (
                    not isinstance(timestamp, int)
                    or isinstance(timestamp, bool)
                    or timestamp < 0
                    or not isinstance(pair, str)
                    or not pair
                ):
                    raise TraceError(f"{path}:{line_number}: invalid Native scheduler event")
                yield {"timestamp_ms": timestamp, "pair": pair}
    except OSError as exc:
        raise TraceError(f"cannot read Native scheduler events {path}: {exc}") from exc


def _fingerprint(report: dict[str, Any]) -> str:
    identity = {key: value for key, value in report.items() if key != "fingerprint"}
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
