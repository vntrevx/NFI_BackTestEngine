"""Streaming exact-state trace format and fail-first comparator.

The on-disk format is deliberately small:

* a fixed magic prefix;
* UTF-8 canonical JSON records, each prefixed by an unsigned 32-bit length;
* one header, zero or more events, and one trailer.

Large production traces can omit materialized state and retain only state/event
hashes. Diagnostic fixtures keep state so the comparator can report a field path.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from blake3 import blake3

from .errors import TraceError
from .parity import ParityDifference, first_difference

MAGIC = b"NFI_STATE_TRACE\x00\x01"
TRACE_SCHEMA_VERSION = "1.0.0"
MAX_RECORD_BYTES = 64 * 1024 * 1024
_LENGTH = struct.Struct(">I")
_COMPARABLE_HEADER_FIELDS = (
    "schema_version",
    "input_sha256",
    "strategy_sha256",
    "profile_sha256",
    "trading_mode",
)


@dataclass(frozen=True)
class TraceDifference:
    """The first semantic difference between two state traces."""

    sequence: int | None
    path: str
    expected: Any
    actual: Any
    reason: str
    event_key: dict[str, Any] | None = None

    def render(self) -> str:
        location = f"event {self.sequence}" if self.sequence is not None else "trace"
        key = f" {json.dumps(self.event_key, ensure_ascii=False)}" if self.event_key else ""
        return (
            f"state parity mismatch at {location}{key} {self.path}: {self.reason}; "
            f"expected {_render(self.expected)}, actual {_render(self.actual)}"
        )


class TraceMismatch(AssertionError):
    """Raised when two valid traces differ."""

    def __init__(self, difference: TraceDifference):
        self.difference = difference
        super().__init__(difference.render())


class StateTraceWriter:
    """Write one deterministic trace without retaining events in memory."""

    def __init__(
        self,
        path: str | Path,
        *,
        source: str,
        run_id: str,
        input_sha256: str,
        strategy_sha256: str,
        profile_sha256: str,
        trading_mode: str,
        include_state: bool = False,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        self._handle.write(MAGIC)
        self._include_state = include_state
        self._sequence = 0
        self._stream_hasher = blake3()
        self._closed = False
        self.header = {
            "kind": "header",
            "schema_version": TRACE_SCHEMA_VERSION,
            "source": _nonempty_string(source, "source"),
            "run_id": _nonempty_string(run_id, "run_id"),
            "input_sha256": _sha256(input_sha256, "input_sha256"),
            "strategy_sha256": _sha256(strategy_sha256, "strategy_sha256"),
            "profile_sha256": _sha256(profile_sha256, "profile_sha256"),
            "trading_mode": _trading_mode(trading_mode),
            "include_state": include_state,
        }
        _write_record(self._handle, self.header)

    def append(
        self,
        *,
        timestamp_ms: int,
        phase: str,
        state: Mapping[str, Any],
        pair: str | None = None,
        callback: str | None = None,
    ) -> dict[str, Any]:
        """Append one event and return its compact record."""
        if self._closed:
            raise TraceError("cannot append to a closed state trace")
        if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool) or timestamp_ms < 0:
            raise TraceError("timestamp_ms must be a non-negative integer")
        event_key = {
            "sequence": self._sequence,
            "timestamp_ms": timestamp_ms,
            "phase": _nonempty_string(phase, "phase"),
            "pair": _nullable_string(pair, "pair"),
            "callback": _nullable_string(callback, "callback"),
        }
        state_document = dict(state)
        state_bytes = canonical_trace_bytes(state_document)
        state_hash = blake3(state_bytes).hexdigest()
        event_hash = blake3(canonical_trace_bytes(event_key) + b"\x00" + state_bytes).hexdigest()
        record: dict[str, Any] = {
            "kind": "event",
            **event_key,
            "state_hash": state_hash,
            "event_hash": event_hash,
        }
        if self._include_state:
            record["state"] = state_document
        _write_record(self._handle, record)
        self._stream_hasher.update(bytes.fromhex(event_hash))
        self._sequence += 1
        return record

    def close(self) -> dict[str, Any]:
        """Write the trailer and close the file. Calling twice is harmless."""
        if self._closed:
            return {
                "kind": "trailer",
                "event_count": self._sequence,
                "stream_hash": self._stream_hasher.hexdigest(),
            }
        trailer = {
            "kind": "trailer",
            "event_count": self._sequence,
            "stream_hash": self._stream_hasher.hexdigest(),
        }
        _write_record(self._handle, trailer)
        self._handle.flush()
        self._handle.close()
        self._closed = True
        return trailer

    def __enter__(self) -> StateTraceWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class StateTrace:
    """Validated trace metadata and events."""

    header: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    trailer: dict[str, Any]


def canonical_trace_bytes(value: Any) -> bytes:
    """Encode JSON-safe values canonically, rejecting binary floats."""
    _validate_canonical_value(value, "$")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def iter_trace_records(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield decoded records while enforcing framing and canonical encoding."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            magic = handle.read(len(MAGIC))
            if magic != MAGIC:
                raise TraceError(f"{source}: invalid state trace magic")
            record_index = 0
            while True:
                raw_length = handle.read(_LENGTH.size)
                if not raw_length:
                    return
                if len(raw_length) != _LENGTH.size:
                    raise TraceError(f"{source}: truncated record length at index {record_index}")
                (length,) = _LENGTH.unpack(raw_length)
                if length == 0 or length > MAX_RECORD_BYTES:
                    raise TraceError(
                        f"{source}: invalid record length {length} at index {record_index}"
                    )
                payload = handle.read(length)
                if len(payload) != length:
                    raise TraceError(f"{source}: truncated record at index {record_index}")
                try:
                    record = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise TraceError(
                        f"{source}: invalid JSON record at index {record_index}"
                    ) from exc
                if not isinstance(record, dict):
                    raise TraceError(f"{source}: record {record_index} must be an object")
                if canonical_trace_bytes(record) != payload:
                    raise TraceError(f"{source}: record {record_index} is not canonical")
                yield record
                record_index += 1
    except OSError as exc:
        raise TraceError(f"{source}: cannot read state trace: {exc}") from exc


def iter_validated_trace_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield fully validated events in one bounded-memory pass."""
    trace = _TraceStream(path)
    while True:
        event = trace.next_event()
        if event is None:
            return
        yield event


def read_state_trace(path: str | Path) -> StateTrace:
    """Read and fully validate a trace. Intended for fixtures and diagnostics."""
    records = list(iter_trace_records(path))
    if len(records) < 2:
        raise TraceError(f"{Path(path)}: expected header and trailer")
    header = records[0]
    trailer = records[-1]
    events = records[1:-1]
    _validate_header(header)
    _validate_trailer(trailer)

    stream_hasher = blake3()
    for expected_sequence, event in enumerate(events):
        _validate_event(event, expected_sequence, include_state=header["include_state"])
        stream_hasher.update(bytes.fromhex(event["event_hash"]))
    if trailer["event_count"] != len(events):
        raise TraceError(
            f"{Path(path)}: trailer event_count {trailer['event_count']} != {len(events)}"
        )
    actual_stream_hash = stream_hasher.hexdigest()
    if trailer["stream_hash"] != actual_stream_hash:
        raise TraceError(
            f"{Path(path)}: stream hash differs; expected {trailer['stream_hash']}, "
            f"actual {actual_stream_hash}"
        )
    return StateTrace(header=header, events=tuple(events), trailer=trailer)


def first_trace_difference(
    expected_path: str | Path, actual_path: str | Path
) -> TraceDifference | None:
    """Return the first exact difference using bounded memory."""
    expected = _TraceStream(expected_path)
    actual = _TraceStream(actual_path)

    for field in _COMPARABLE_HEADER_FIELDS:
        if expected.header[field] != actual.header[field]:
            return TraceDifference(
                sequence=None,
                path=f"$.header.{field}",
                expected=expected.header[field],
                actual=actual.header[field],
                reason="header value differs",
            )

    index = 0
    while True:
        expected_event = expected.next_event()
        actual_event = actual.next_event()
        if expected_event is None or actual_event is None:
            break
        expected_key = _event_key(expected_event)
        actual_key = _event_key(actual_event)
        if expected_key != actual_key:
            difference = first_difference(expected_key, actual_key, "$.event_key")
            assert difference is not None
            return _trace_difference(index, difference, expected_key)
        if expected_event["event_hash"] == actual_event["event_hash"]:
            index += 1
            continue
        if "state" in expected_event and "state" in actual_event:
            difference = first_difference(expected_event["state"], actual_event["state"], "$.state")
            if difference is not None:
                return _trace_difference(index, difference, expected_key)
        return TraceDifference(
            sequence=index,
            path="$.event_hash",
            expected=expected_event["event_hash"],
            actual=actual_event["event_hash"],
            reason="event state differs",
            event_key=expected_key,
        )

    if expected_event is not None or actual_event is not None:
        expected_count = expected.finish()
        actual_count = actual.finish()
        return TraceDifference(
            sequence=index,
            path="$.events.length",
            expected=expected_count,
            actual=actual_count,
            reason="event count differs",
        )
    return None


def compare_state_traces(expected_path: str | Path, actual_path: str | Path) -> None:
    """Raise on the first exact state-trace difference."""
    difference = first_trace_difference(expected_path, actual_path)
    if difference is not None:
        raise TraceMismatch(difference)


def trace_summary(path: str | Path) -> dict[str, Any]:
    trace = _TraceStream(path)
    event_count = trace.finish()
    assert trace.trailer is not None
    return {
        "schema_version": trace.header["schema_version"],
        "source": trace.header["source"],
        "run_id": trace.header["run_id"],
        "input_sha256": trace.header["input_sha256"],
        "strategy_sha256": trace.header["strategy_sha256"],
        "profile_sha256": trace.header["profile_sha256"],
        "trading_mode": trace.header["trading_mode"],
        "include_state": trace.header["include_state"],
        "event_count": event_count,
        "stream_hash": trace.trailer["stream_hash"],
    }


class _TraceStream:
    """Validate one trace incrementally while exposing one event at a time."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._records = iter(iter_trace_records(self.path))
        try:
            self.header = next(self._records)
        except StopIteration as exc:
            raise TraceError(f"{self.path}: expected header and trailer") from exc
        _validate_header(self.header)
        self._sequence = 0
        self._stream_hasher = blake3()
        self.trailer: dict[str, Any] | None = None

    def next_event(self) -> dict[str, Any] | None:
        if self.trailer is not None:
            return None
        try:
            record = next(self._records)
        except StopIteration as exc:
            raise TraceError(f"{self.path}: missing trailer") from exc
        if record.get("kind") == "trailer":
            self._finish_trailer(record)
            return None
        _validate_event(
            record,
            self._sequence,
            include_state=self.header["include_state"],
        )
        self._stream_hasher.update(bytes.fromhex(record["event_hash"]))
        self._sequence += 1
        return record

    def finish(self) -> int:
        while self.next_event() is not None:
            pass
        return self._sequence

    def _finish_trailer(self, record: dict[str, Any]) -> None:
        _validate_trailer(record)
        if record["event_count"] != self._sequence:
            raise TraceError(
                f"{self.path}: trailer event_count {record['event_count']} != {self._sequence}"
            )
        stream_hash = self._stream_hasher.hexdigest()
        if record["stream_hash"] != stream_hash:
            raise TraceError(
                f"{self.path}: stream hash differs; expected {record['stream_hash']}, "
                f"actual {stream_hash}"
            )
        try:
            extra = next(self._records)
        except StopIteration:
            self.trailer = record
            return
        raise TraceError(f"{self.path}: unexpected record after trailer: {extra.get('kind')!r}")


def _write_record(handle: BinaryIO, record: Mapping[str, Any]) -> None:
    payload = canonical_trace_bytes(dict(record))
    if len(payload) > MAX_RECORD_BYTES:
        raise TraceError(f"state trace record exceeds {MAX_RECORD_BYTES} bytes")
    handle.write(_LENGTH.pack(len(payload)))
    handle.write(payload)


def _validate_header(record: dict[str, Any]) -> None:
    required = {
        "kind",
        "schema_version",
        "source",
        "run_id",
        "input_sha256",
        "strategy_sha256",
        "profile_sha256",
        "trading_mode",
        "include_state",
    }
    _exact_keys(record, required, "$.header")
    if record["kind"] != "header":
        raise TraceError("$.header.kind: expected 'header'")
    if record["schema_version"] != TRACE_SCHEMA_VERSION:
        raise TraceError(
            f"$.header.schema_version: unsupported version {record['schema_version']!r}"
        )
    _nonempty_string(record["source"], "$.header.source")
    _nonempty_string(record["run_id"], "$.header.run_id")
    for field in ("input_sha256", "strategy_sha256", "profile_sha256"):
        _sha256(record[field], f"$.header.{field}")
    _trading_mode(record["trading_mode"])
    if not isinstance(record["include_state"], bool):
        raise TraceError("$.header.include_state: expected a boolean")


def _validate_event(record: dict[str, Any], sequence: int, *, include_state: bool) -> None:
    required = {
        "kind",
        "sequence",
        "timestamp_ms",
        "phase",
        "pair",
        "callback",
        "state_hash",
        "event_hash",
    }
    allowed = {*required, "state"}
    _keys(record, required, allowed, f"$.events[{sequence}]")
    if record["kind"] != "event":
        raise TraceError(f"$.events[{sequence}].kind: expected 'event'")
    if record["sequence"] != sequence:
        raise TraceError(
            f"$.events[{sequence}].sequence: expected {sequence}, got {record['sequence']!r}"
        )
    if (
        not isinstance(record["timestamp_ms"], int)
        or isinstance(record["timestamp_ms"], bool)
        or record["timestamp_ms"] < 0
    ):
        raise TraceError(f"$.events[{sequence}].timestamp_ms: expected non-negative integer")
    _nonempty_string(record["phase"], f"$.events[{sequence}].phase")
    _nullable_string(record["pair"], f"$.events[{sequence}].pair")
    _nullable_string(record["callback"], f"$.events[{sequence}].callback")
    _digest(record["state_hash"], f"$.events[{sequence}].state_hash")
    _digest(record["event_hash"], f"$.events[{sequence}].event_hash")
    if include_state != ("state" in record):
        raise TraceError(
            f"$.events[{sequence}].state: materialization disagrees with header include_state"
        )
    if "state" in record:
        state_bytes = canonical_trace_bytes(record["state"])
        actual_state_hash = blake3(state_bytes).hexdigest()
        if actual_state_hash != record["state_hash"]:
            raise TraceError(
                f"$.events[{sequence}].state_hash: expected {record['state_hash']}, "
                f"actual {actual_state_hash}"
            )
        actual_event_hash = blake3(
            canonical_trace_bytes(_event_key(record)) + b"\x00" + state_bytes
        ).hexdigest()
        if actual_event_hash != record["event_hash"]:
            raise TraceError(
                f"$.events[{sequence}].event_hash: expected {record['event_hash']}, "
                f"actual {actual_event_hash}"
            )


def _validate_trailer(record: dict[str, Any]) -> None:
    _exact_keys(record, {"kind", "event_count", "stream_hash"}, "$.trailer")
    if record["kind"] != "trailer":
        raise TraceError("$.trailer.kind: expected 'trailer'")
    if (
        not isinstance(record["event_count"], int)
        or isinstance(record["event_count"], bool)
        or record["event_count"] < 0
    ):
        raise TraceError("$.trailer.event_count: expected non-negative integer")
    _digest(record["stream_hash"], "$.trailer.stream_hash")


def _validate_canonical_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise TraceError(f"{path}: binary floats are forbidden; use canonical decimal strings")
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TraceError(f"{path}: object keys must be strings")
            _validate_canonical_value(item, f"{path}.{key}")
        return
    raise TraceError(f"{path}: unsupported trace value type {type(value).__name__}")


def _event_key(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": event["sequence"],
        "timestamp_ms": event["timestamp_ms"],
        "phase": event["phase"],
        "pair": event["pair"],
        "callback": event["callback"],
    }


def _trace_difference(
    sequence: int, difference: ParityDifference, event_key: dict[str, Any]
) -> TraceDifference:
    return TraceDifference(
        sequence=sequence,
        path=difference.path,
        expected=difference.expected,
        actual=difference.actual,
        reason=difference.reason,
        event_key=event_key,
    )


def _keys(value: Mapping[str, Any], required: set[str], allowed: set[str], path: str) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - allowed)
    if missing:
        raise TraceError(f"{path}: missing keys: {', '.join(missing)}")
    if unexpected:
        raise TraceError(f"{path}: unexpected keys: {', '.join(unexpected)}")


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    _keys(value, expected, expected, path)


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise TraceError(f"{path}: expected a non-empty string")
    return value


def _nullable_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, path)


def _trading_mode(value: Any) -> str:
    if value not in {"spot", "futures"}:
        raise TraceError("trading_mode must be 'spot' or 'futures'")
    return value


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise TraceError(f"{path}: expected a 64-character SHA-256")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise TraceError(f"{path}: expected lowercase hexadecimal SHA-256") from exc
    if value != value.lower():
        raise TraceError(f"{path}: expected lowercase hexadecimal SHA-256")
    return value


def _digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise TraceError(f"{path}: expected a 64-character BLAKE3 digest")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise TraceError(f"{path}: expected lowercase hexadecimal digest") from exc
    if value != value.lower():
        raise TraceError(f"{path}: expected lowercase hexadecimal digest")
    return value


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
