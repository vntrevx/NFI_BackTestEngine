"""Validate direct, source-ordered Native execution-boundary events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import MAX_JSON_BYTES, canonical_decimal, loads_json_bytes
from .errors import InputBoundaryError, NormalizationError, SpecValidationError, TraceError
from .specs import EXECUTION_BOUNDARY_EVENT_SCHEMA, validate_schema

EXECUTION_BOUNDARY_EVENT_VERSION = "execution-boundary-event-v1"

_FILL_PHASES = {"entry_fill", "adjustment_fill", "partial_exit_fill", "exit_fill"}
_DECIMAL_FIELDS = {
    "proposed_rate",
    "clamped_rate",
    "precision_rate",
    "amount_input",
    "amount_step",
    "amount_output",
    "price_input",
    "current_price_step",
    "price_step",
    "price_output",
    "minimum_stake",
    "fee_open",
    "fee_close",
    "fee_applied",
}


def load_execution_boundary_events(source: str | Path) -> list[dict[str, Any]]:
    """Load one bounded Native JSONL stream without reordering or filling fields."""
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise TraceError(f"execution event source must be a regular non-symlink file: {path}")
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise TraceError(f"execution event byte limit exceeded ({size} > {MAX_JSON_BYTES})")

    events: list[dict[str, Any]] = []
    consumed = 0
    with path.open("rb") as handle:
        for line_number, payload in enumerate(handle, start=1):
            consumed += len(payload)
            if consumed > MAX_JSON_BYTES:
                raise TraceError("execution event byte limit exceeded while reading")
            if not payload.strip():
                raise TraceError(f"execution event line {line_number} is blank")
            try:
                event = loads_json_bytes(payload, max_bytes=MAX_JSON_BYTES)
            except (InputBoundaryError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TraceError(f"execution event line {line_number} is invalid JSON") from exc
            if not isinstance(event, dict):
                raise TraceError(f"execution event line {line_number} is not an object")
            try:
                validate_schema(event, EXECUTION_BOUNDARY_EVENT_SCHEMA)
            except SpecValidationError as exc:
                raise TraceError(f"execution event line {line_number} differs: {exc}") from exc
            _validate_event(event, len(events))
            events.append(event)
    if not events:
        raise TraceError("execution event stream is empty")
    return events


def _validate_event(event: Mapping[str, Any], index: int) -> None:
    if event["schema_version"] != EXECUTION_BOUNDARY_EVENT_VERSION:
        raise TraceError(f"execution event {index} version differs")
    if event["sequence"] != index:
        raise TraceError(f"execution event {index} sequence differs")
    if ("state_before" in event) != ("state_after" in event):
        raise TraceError(f"execution event {index} state boundary is incomplete")
    phase = event["phase"]
    if phase in _FILL_PHASES:
        required = {
            "order_status",
            "trade_id",
            "order_id",
            "amount_output",
            "price_output",
            "fee_applied",
        }
        if event.get("order_status") != "filled" or not required.issubset(event):
            raise TraceError(f"execution event {index} fill boundary is incomplete")
    if phase == "exit_competition" and not event["candidates"]:
        raise TraceError(f"execution event {index} exit competition has no candidates")
    if phase == "exit_confirmation" and ("winner" not in event or "confirmation" not in event):
        raise TraceError(f"execution event {index} exit confirmation is incomplete")
    for key, value in event.items():
        if key in _DECIMAL_FIELDS:
            _validate_decimal(value, index=index, path=key)
    for key, value in event["candle"].items():
        _validate_decimal(value, index=index, path=f"candle.{key}")
    for key, value in event["intermediates"].items():
        _validate_decimal(value, index=index, path=f"intermediates.{key}")


def _validate_decimal(value: object, *, index: int, path: str) -> None:
    if not isinstance(value, str):
        raise TraceError(f"execution event {index} {path} is not an exact decimal token")
    try:
        canonical = canonical_decimal(value, path=path)
    except NormalizationError as exc:
        raise TraceError(f"execution event {index} {path} is not an exact decimal") from exc
    if canonical != value:
        raise TraceError(f"execution event {index} {path} is not canonical exact decimal")
