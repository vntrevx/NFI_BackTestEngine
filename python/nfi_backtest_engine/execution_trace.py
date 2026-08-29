"""Exact, source-bound execution semantic trace verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import canonical_decimal, read_json, write_json
from .errors import InputBoundaryError, NormalizationError, SpecValidationError, TraceError
from .execution_event_adapter import adapt_native_execution_events
from .fixture import fixture_input_sha256, validate_fixture
from .parity import first_difference
from .specs import EXECUTION_TRACE_SCHEMA, EXECUTION_VERIFICATION_SCHEMA, validate_schema

EXECUTION_TRACE_VERSION = "execution-semantic-trace-v1"
EXECUTION_VERIFICATION_VERSION = "execution-semantic-verification-v1"


class ExecutionTraceError(TraceError):
    """An execution proof input is malformed, unauthenticated, or mismatched."""


def canonical_execution_json(value: Any) -> bytes:
    """Encode semantic JSON deterministically without changing ordered arrays."""
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def verify_execution_trace(
    manifest_path: str | Path,
    official_trace: str | Path,
    native_events: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare official and Native execution boundaries exactly, with no repair path."""
    manifest = validate_fixture(manifest_path, validate_trace_semantics=False)
    manifest_bytes = getattr(manifest, "manifest_payload", None)
    if not isinstance(manifest_bytes, bytes):
        raise ExecutionTraceError("fixture validation did not retain manifest bytes")
    official, official_raw = _read_trace(official_trace, "official trace")
    native_document, native_raw = _read_document(native_events, "Native events")
    try:
        native = adapt_native_execution_events(native_document)
    except TraceError as exc:
        raise ExecutionTraceError(f"Native events adapter differs: {exc}") from exc
    _schema(native, "Native events")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    _validate_official(official, manifest, manifest_hash)
    difference = first_difference(official, native)
    report: dict[str, Any] = {
        "schema_version": EXECUTION_VERIFICATION_VERSION,
        "fixture_id": manifest["fixture_id"],
        "fixture_manifest_sha256": manifest_hash,
        "official_raw_sha256": _sha(official_raw),
        "native_raw_sha256": _sha(native_raw),
        "official_canonical_sha256": _sha(canonical_execution_json(official)),
        "native_canonical_sha256": _sha(canonical_execution_json(native)),
        "exact": difference is None,
        "event_count": len(official["events"]),
        "mismatch": None
        if difference is None
        else {
            "path": difference.path,
            "expected": difference.expected,
            "actual": difference.actual,
            "reason": difference.reason,
        },
    }
    report["fingerprint"] = _sha(
        canonical_execution_json(
            {key: value for key, value in report.items() if key != "fingerprint"}
        )
    )
    validate_schema(report, EXECUTION_VERIFICATION_SCHEMA)
    if output_path is not None:
        _write_report(output_path, report)
    return report


def _read_document(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise ExecutionTraceError(f"{label} must be a regular non-symlink file: {source}")
        raw = source.read_bytes()
        document = read_json(source)
    except InputBoundaryError as exc:
        raise ExecutionTraceError(f"{label} is outside the JSON input boundary: {exc}") from exc
    except OSError as exc:
        raise ExecutionTraceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise ExecutionTraceError(f"{label} must be an object")
    return document, raw


def _read_trace(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    trace, raw = _read_document(path, label)
    _schema(trace, label)
    return trace, raw


def _schema(document: Mapping[str, Any], label: str) -> None:
    try:
        validate_schema(document, EXECUTION_TRACE_SCHEMA)
    except SpecValidationError as exc:
        raise ExecutionTraceError(f"{label} schema differs: {exc}") from exc


def _validate_official(
    trace: Mapping[str, Any], manifest: Mapping[str, Any], manifest_hash: str
) -> None:
    header = trace["header"]
    if (
        header["fixture_id"] != manifest["fixture_id"]
        or header["fixture_manifest_sha256"] != manifest_hash
    ):
        raise ExecutionTraceError("official trace fixture identity differs")
    strategy = next((item for item in manifest["inputs"] if item["role"] == "strategy"), None)
    if not isinstance(strategy, Mapping) or header["source"] != {
        "path": strategy["path"],
        "sha256": strategy["sha256"],
    }:
        raise ExecutionTraceError("official trace source identity differs")
    if header["input"]["sha256"] != fixture_input_sha256(manifest["inputs"]):
        raise ExecutionTraceError("official trace input identity differs")
    expected_contract = Path(__file__).parent / "contracts/freqtrade-execution-contract.json"
    if header["contract"] != {
        "path": "contracts/freqtrade-execution-contract.json",
        "sha256": _sha(expected_contract.read_bytes()),
    }:
        raise ExecutionTraceError("official trace contract identity differs")
    artifacts = list(manifest["artifacts"].values())
    if header["artifact"] not in [
        {"path": item["path"], "sha256": item["sha256"]} for item in artifacts
    ]:
        raise ExecutionTraceError("official trace artifact identity differs")
    for index, event in enumerate(trace["events"]):
        _validate_event(event, index)


def _validate_event(event: Mapping[str, Any], index: int) -> None:
    if event["sequence"] != index:
        raise ExecutionTraceError(f"official trace event {index} sequence differs")
    _validate_candidates(event["candidates"], index)
    _validate_order_lifecycle(event, index)
    for key, value in _decimal_values(event):
        try:
            if canonical_decimal(value, path=key) != value:
                raise ExecutionTraceError(
                    f"official trace event {index} {key} is not canonical exact decimal"
                )
        except NormalizationError as exc:
            raise ExecutionTraceError(
                f"official trace event {index} {key} is not exact decimal"
            ) from exc


def _validate_candidates(candidates: Mapping[str, Any], index: int) -> None:
    attempts = candidates["attempts"]
    names = [attempt["candidate"] for attempt in attempts]
    if len(names) != len(set(names)):
        raise ExecutionTraceError(f"official trace event {index} candidate order differs")
    accepted = [attempt for attempt in attempts if attempt["confirmation"] == "accepted"]
    if candidates["outcome"] == "selected":
        if len(accepted) != 1 or candidates["winner"] != accepted[0]["candidate"]:
            raise ExecutionTraceError(f"official trace event {index} candidate winner differs")
        winner_index = attempts.index(accepted[0])
        if winner_index != len(attempts) - 1 or any(
            not attempt["fallthrough"] for attempt in attempts[:winner_index]
        ):
            raise ExecutionTraceError(f"official trace event {index} rejection fallthrough differs")
        return
    if (
        candidates["winner"] is not None
        or accepted
        or any(attempt["confirmation"] != "rejected" for attempt in attempts)
    ):
        raise ExecutionTraceError(f"official trace event {index} all-rejected outcome differs")
    if any(not attempt["fallthrough"] for attempt in attempts[:-1]) or attempts[-1]["fallthrough"]:
        raise ExecutionTraceError(f"official trace event {index} rejection stop differs")


def _validate_order_lifecycle(event: Mapping[str, Any], index: int) -> None:
    lifecycle = event["order_lifecycle"]
    fill_rate = event["fill"]["rate"]
    requested = lifecycle["requested_limit_rate"]
    adjusted = lifecycle["adjusted_limit_rate"]
    if (
        lifecycle["unfilled"] != (fill_rate is None)
        or lifecycle["retry"]
        and not lifecycle["unfilled"]
    ):
        raise ExecutionTraceError(f"official trace event {index} fill lifecycle differs")
    if lifecycle["mode"] == "market":
        if requested is not None or adjusted is not None or fill_rate is None:
            raise ExecutionTraceError(f"official trace event {index} market lifecycle differs")
    elif requested is None or adjusted is None:
        raise ExecutionTraceError(f"official trace event {index} limit lifecycle differs")


def _decimal_values(event: Mapping[str, Any]) -> list[tuple[str, str]]:
    candle = event["candle"]
    precision = event["precision"]
    lifecycle = event["order_lifecycle"]
    values = (
        [(f"candle.{key}", candle[key]) for key in ("open", "high", "low", "close")]
        + [
            (f"precision.{key}", precision[key])
            for key in (
                "amount_input",
                "amount_step",
                "amount_frozen_step",
                "amount_rounded",
                "price_input",
                "price_step",
                "price_frozen_step",
                "price_rounded",
            )
        ]
        + [
            ("min_stake.result", event["min_stake"]["result"]),
            ("fees.open_rate", event["fees"]["open_rate"]),
            ("fees.close_rate", event["fees"]["close_rate"]),
            *[("fees.per_fill", item) for item in event["fees"]["per_fill"]],
            *[
                (f"intermediates.{key}", event["intermediates"][key])
                for key in ("stake", "basis", "profit")
            ],
            ("partial_exit_amount", event["partial_exit_amount"]),
            *([("fill.rate", event["fill"]["rate"])] if event["fill"]["rate"] is not None else []),
            *(
                [("order_lifecycle.requested_limit_rate", lifecycle["requested_limit_rate"])]
                if lifecycle["requested_limit_rate"] is not None
                else []
            ),
            *(
                [("order_lifecycle.adjusted_limit_rate", lifecycle["adjusted_limit_rate"])]
                if lifecycle["adjusted_limit_rate"] is not None
                else []
            ),
        ]
    )
    return values


def _write_report(path: str | Path, report: Mapping[str, Any]) -> None:
    destination = Path(path)
    if destination.is_symlink():
        raise ExecutionTraceError(
            f"execution verification output must not be a symlink: {destination}"
        )
    write_json(destination, report)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
