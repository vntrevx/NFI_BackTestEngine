"""Exact, source-bound portfolio semantic trace verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import InputBoundaryError, SpecValidationError, TraceError
from .fixture import validate_fixture
from .parity import first_difference
from .portfolio_event_adapter import (
    adapt_native_portfolio_boundary_envelope,
    adapt_native_portfolio_events,
)
from .portfolio_official_projection import project_official_portfolio_boundaries
from .specs import PORTFOLIO_TRACE_SCHEMA, PORTFOLIO_VERIFICATION_SCHEMA, validate_schema

PORTFOLIO_TRACE_VERSION = "portfolio-semantic-trace-v1"
PORTFOLIO_VERIFICATION_VERSION = "portfolio-semantic-verification-v1"


class PortfolioTraceError(TraceError):
    """A portfolio proof input is unauthenticated, malformed, or inconsistent."""


def canonical_portfolio_json(value: Any) -> bytes:
    """Encode a JSON value without treating whitespace or object layout as semantic."""
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def verify_portfolio_trace(
    manifest_path: str | Path,
    official_trace: str | Path,
    native_events: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify exact portfolio transitions without sorting or reconstructing either lane."""
    manifest = validate_fixture(manifest_path, validate_trace_semantics=False)
    official_document, official_raw = _read_document(official_trace, "official trace")
    if official_document.get("schema_version") == "freqtrade-portfolio-pressure-trace-v1":
        native_document, native_raw = _read_document(native_events, "Native events")
        return _verify_direct_portfolio_capture(
            Path(manifest_path).resolve(),
            manifest,
            official_document,
            official_raw,
            native_document,
            native_raw,
            output_path=output_path,
        )
    official, official_raw = _validated_trace_document(
        official_document, official_raw, "official trace"
    )
    native, native_raw = _read_trace(native_events, "Native events", adapt_native=True)
    manifest_payload = getattr(manifest, "manifest_payload", None)
    if not isinstance(manifest_payload, bytes):
        raise PortfolioTraceError("fixture validation did not retain manifest bytes")
    manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
    # The official lane is the authenticated semantic oracle. Native output is
    # schema-checked but deliberately not normalized or repaired before the
    # comparison: its malformed schedule must surface at the exact mismatch.
    _validate_trace(official, manifest["fixture_id"], manifest_hash, "official trace")
    difference = first_difference(official, native)
    report: dict[str, Any] = {
        "schema_version": PORTFOLIO_VERIFICATION_VERSION,
        "fixture_id": manifest["fixture_id"],
        "fixture_manifest_sha256": manifest_hash,
        "official_raw_sha256": hashlib.sha256(official_raw).hexdigest(),
        "native_raw_sha256": hashlib.sha256(native_raw).hexdigest(),
        "official_canonical_sha256": hashlib.sha256(canonical_portfolio_json(official)).hexdigest(),
        "native_canonical_sha256": hashlib.sha256(canonical_portfolio_json(native)).hexdigest(),
        "exact": difference is None,
        "event_count": len(official["events"]),
        "mismatch": (
            None
            if difference is None
            else {
                "path": difference.path,
                "expected": difference.expected,
                "actual": difference.actual,
                "reason": difference.reason,
            }
        ),
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, PORTFOLIO_VERIFICATION_SCHEMA)
    if output_path is not None:
        _write_report(output_path, report)
    return report


def _verify_direct_portfolio_capture(
    manifest_path: Path,
    manifest: dict[str, Any],
    official: dict[str, Any],
    official_raw: bytes,
    native_document: dict[str, Any],
    native_raw: bytes,
    *,
    output_path: str | Path | None,
) -> dict[str, Any]:
    try:
        native = adapt_native_portfolio_boundary_envelope(native_document)
    except TraceError as exc:
        raise PortfolioTraceError(f"Native events adapter differs: {exc}") from exc
    root = manifest_path.parent
    official_sha = hashlib.sha256(official_raw).hexdigest()
    official_inputs = [
        item
        for item in manifest["inputs"]
        if item["role"] == "auxiliary" and item["sha256"] == official_sha
    ]
    if len(official_inputs) != 1:
        raise PortfolioTraceError("official portfolio trace is not a sealed fixture input")
    auxiliary = [
        read_json(root / item["path"])
        for item in manifest["inputs"]
        if item["role"] == "auxiliary"
    ]
    authentication = next(
        (
            item
            for item in auxiliary
            if isinstance(item, dict)
            and item.get("schema_version") == "official-source-authentication-v1"
        ),
        None,
    )
    if not isinstance(authentication, dict):
        raise PortfolioTraceError("official source authentication is missing")
    contract_path = (
        Path(__file__).parent
        / "contracts/freqtrade-portfolio-pressure-contract-v1.json"
    )
    contract = read_json(contract_path)
    surface = read_json(root / manifest["artifacts"]["trade_surface"]["path"])
    config_input = next(item for item in manifest["inputs"] if item["role"] == "config")
    strategy_input = next(item for item in manifest["inputs"] if item["role"] == "strategy")
    config = read_json(root / config_input["path"])
    from .fixture_engine import official_consumed_interval

    interval = official_consumed_interval(manifest_path, manifest)
    events = project_official_portfolio_boundaries(
        official,
        surface,
        slot_limit=int(config["max_open_trades"]),
        contract=contract,
        authentication=authentication,
    )
    manifest_payload = getattr(manifest, "manifest_payload", None)
    if not isinstance(manifest_payload, bytes):
        raise PortfolioTraceError("fixture validation did not retain manifest bytes")
    manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
    header = native["portfolio_header"]
    expected_header = {
        "fixture_id": manifest["fixture_id"],
        "fixture_manifest_sha256": manifest_hash,
        "scheduler_contract_sha256": interval["scheduler_contract_sha256"],
        "scheduler_contract_fingerprint": interval["scheduler_contract_fingerprint"],
        "portfolio_contract_sha256": interval["portfolio_contract_sha256"],
        "portfolio_contract_fingerprint": interval["portfolio_contract_fingerprint"],
        "source_sha256": strategy_input["sha256"],
        "config_sha256": config_input["sha256"],
        "data_sha256": interval["data_sha256"],
        "official_trace_sha256": official_sha,
        "native_timerange": interval["native_timerange"],
        "configured_pairs": config["exchange"]["pair_whitelist"],
        "slot_limit": int(config["max_open_trades"]),
    }
    if any(header.get(key) != value for key, value in expected_header.items()):
        raise PortfolioTraceError("Native portfolio envelope authenticated identity differs")
    for name in ("native_input_sha256", "native_binary_sha256"):
        value = header[name]
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise PortfolioTraceError(f"Native portfolio envelope {name} differs")
    force_events = [event for event in events if event["boundary"] == "force_exit"]
    official_projection = {
        "schema_version": "native-portfolio-events-v1",
        "portfolio_header": header,
        "portfolio_events": events,
        "final_force_exit_trade_ids": [event["force_exit_trade_id"] for event in force_events],
        "final_trades": [
            {
                "force_exit_sequence": index,
                "trade_id": event["force_exit_trade_id"],
                "pair": event["pair"],
                "order_ids": event["force_exit_order_ids"],
            }
            for index, event in enumerate(force_events)
        ],
    }
    difference = first_difference(official_projection, native)
    report: dict[str, Any] = {
        "schema_version": PORTFOLIO_VERIFICATION_VERSION,
        "fixture_id": manifest["fixture_id"],
        "fixture_manifest_sha256": manifest_hash,
        "official_raw_sha256": official_sha,
        "native_raw_sha256": hashlib.sha256(native_raw).hexdigest(),
        "official_canonical_sha256": hashlib.sha256(
            canonical_portfolio_json(official_projection)
        ).hexdigest(),
        "native_canonical_sha256": hashlib.sha256(canonical_portfolio_json(native)).hexdigest(),
        "exact": difference is None,
        "event_count": len(events),
        "mismatch": (
            None
            if difference is None
            else {
                "path": difference.path,
                "expected": difference.expected,
                "actual": difference.actual,
                "reason": difference.reason,
            }
        ),
    }
    report["fingerprint"] = _fingerprint(report)
    validate_schema(report, PORTFOLIO_VERIFICATION_SCHEMA)
    if output_path is not None:
        _write_report(output_path, report)
    return report


def _read_document(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise PortfolioTraceError(f"{label} must be a regular non-symlink file: {source}")
        raw = source.read_bytes()
        document = read_json(source)
    except InputBoundaryError as exc:
        raise PortfolioTraceError(f"{label} is outside the JSON input boundary: {exc}") from exc
    except OSError as exc:
        raise PortfolioTraceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise PortfolioTraceError(f"{label} must be an object")
    return document, raw


def _validated_trace_document(
    document: dict[str, Any], raw: bytes, label: str
) -> tuple[dict[str, Any], bytes]:
    try:
        validate_schema(document, PORTFOLIO_TRACE_SCHEMA)
    except SpecValidationError as exc:
        raise PortfolioTraceError(f"{label} schema differs: {exc}") from exc
    return document, raw


def _read_trace(
    path: str | Path, label: str, *, adapt_native: bool = False
) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise PortfolioTraceError(f"{label} must be a regular non-symlink file: {source}")
        raw = source.read_bytes()
        document = read_json(source)
    except InputBoundaryError as exc:
        raise PortfolioTraceError(f"{label} is outside the JSON input boundary: {exc}") from exc
    except OSError as exc:
        raise PortfolioTraceError(f"cannot read {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise PortfolioTraceError(f"{label} must be an object")
    if adapt_native:
        try:
            document = adapt_native_portfolio_events(document)
        except TraceError as exc:
            raise PortfolioTraceError(f"{label} adapter differs: {exc}") from exc
    try:
        validate_schema(document, PORTFOLIO_TRACE_SCHEMA)
    except SpecValidationError as exc:
        raise PortfolioTraceError(f"{label} schema differs: {exc}") from exc
    return document, raw


def _validate_trace(
    trace: Mapping[str, Any], fixture_id: str, manifest_hash: str, label: str
) -> None:
    header = trace["header"]
    if header["fixture_id"] != fixture_id or header["fixture_manifest_sha256"] != manifest_hash:
        raise PortfolioTraceError(f"{label} fixture artifact identity differs")
    configured = header["configured_pairs"]
    if len(configured) != len(set(configured)):
        raise PortfolioTraceError(f"{label} configured pair order contains a duplicate")
    previous_timestamp = -1
    expected_batch_index = 0
    batch: list[Mapping[str, Any]] = []
    for index, event in enumerate(trace["events"]):
        if event["sequence"] != index:
            raise PortfolioTraceError(f"{label} event {index} sequence differs")
        timestamp = event["timestamp_ms"]
        if timestamp < previous_timestamp:
            raise PortfolioTraceError(f"{label} event {index} timestamp order differs")
        if batch and timestamp != previous_timestamp:
            _validate_batch(batch, configured, label, expected_batch_index)
            expected_batch_index += 1
            batch = []
        _validate_event(event, configured, header["slot_limit"], label, index)
        batch.append(event)
        previous_timestamp = timestamp
    if batch:
        _validate_batch(batch, configured, label, expected_batch_index)
    _validate_final_trades(
        trace["final_trades"], trace["final_force_exit_trade_ids"], configured, label
    )


def _validate_event(
    event: Mapping[str, Any], configured: list[str], slot_limit: int, label: str, index: int
) -> None:
    open_trades = event["open_trade_insertion_order"]
    open_pairs = [trade["pair"] for trade in open_trades]
    trade_ids = [trade["trade_id"] for trade in open_trades]
    if (
        len(open_pairs) != len(set(open_pairs))
        or len(trade_ids) != len(set(trade_ids))
        or any(pair not in configured for pair in open_pairs)
    ):
        raise PortfolioTraceError(f"{label} event {index} open-trade insertion order differs")
    expected_phase = "open-trade" if event["pair"] in open_pairs else "remaining-pair"
    if event["phase"] != expected_phase:
        raise PortfolioTraceError(f"{label} event {index} phase differs")
    for point in ("before", "after"):
        if event["slots"][point]["limit"] != slot_limit:
            raise PortfolioTraceError(f"{label} event {index} slot limit differs")
    if event["slots"]["before"]["occupied"] != len(open_trades):
        raise PortfolioTraceError(f"{label} event {index} slot occupancy differs")
    decision = event["decision"]
    if (decision["accepted"] and decision["rejection_reason"] is not None) or (
        not decision["accepted"] and decision["rejection_reason"] is None
    ):
        raise PortfolioTraceError(f"{label} event {index} rejection reason differs")


def _validate_batch(
    batch: list[Mapping[str, Any]], configured: list[str], label: str, expected_index: int
) -> None:
    open_trades = batch[0]["open_trade_insertion_order"]
    if any(event["open_trade_insertion_order"] != open_trades for event in batch):
        raise PortfolioTraceError(f"{label} timestamp batch changes its open-trade phase")
    open_pairs = [trade["pair"] for trade in open_trades]
    expected = [*open_pairs, *(pair for pair in configured if pair not in open_pairs)]
    actual = [event["pair"] for event in batch]
    positions = [event["batch_position"] for event in batch]
    batch_indexes = [event["batch_index"] for event in batch]
    configured_indexes = [event["configured_pair_index"] for event in batch]
    if (
        actual != expected
        or positions != list(range(len(batch)))
        or batch_indexes != [expected_index] * len(batch)
    ):
        raise PortfolioTraceError(f"{label} timestamp batch pair order differs")
    if configured_indexes != [configured.index(pair) for pair in actual]:
        raise PortfolioTraceError(f"{label} timestamp batch configured pair index differs")


def _validate_final_trades(
    trades: list[Mapping[str, Any]],
    force_exit_ids: list[int],
    configured: list[str],
    label: str,
) -> None:
    identifiers = [trade["trade_id"] for trade in trades]
    orders = [order for trade in trades for order in trade["order_ids"]]
    if len(identifiers) != len(set(identifiers)) or len(orders) != len(set(orders)):
        raise PortfolioTraceError(f"{label} final trade or order identity is duplicated")
    if any(trade["pair"] not in configured for trade in trades):
        raise PortfolioTraceError(f"{label} final trade pair is not configured")
    if identifiers != force_exit_ids or [trade["force_exit_sequence"] for trade in trades] != list(
        range(len(trades))
    ):
        raise PortfolioTraceError(f"{label} final force-exit order differs")


def _write_report(path: str | Path, report: Mapping[str, Any]) -> None:
    destination = Path(path)
    if destination.is_symlink():
        raise PortfolioTraceError(
            f"portfolio verification output must not be a symlink: {destination}"
        )
    write_json(destination, report)


def _fingerprint(report: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_portfolio_json(
            {key: value for key, value in report.items() if key != "fingerprint"}
        )
    ).hexdigest()
