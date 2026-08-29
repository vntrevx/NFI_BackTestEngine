"""Closed, lossless Native execution-event envelope adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import SpecValidationError, TraceError
from .specs import NATIVE_EXECUTION_EVENTS_SCHEMA, validate_schema

NATIVE_EXECUTION_EVENTS_VERSION = "native-execution-events-v1"


def adapt_native_execution_events(document: Mapping[str, Any]) -> dict[str, Any]:
    """Map only the versioned envelope; never sort, infer, or default a field."""
    if document.get("schema_version") == "execution-semantic-trace-v1":
        return dict(document)
    expected = {"schema_version", "execution_header", "execution_events"}
    if (
        set(document) != expected
        or document.get("schema_version") != NATIVE_EXECUTION_EVENTS_VERSION
    ):
        raise TraceError("Native execution event envelope differs from its versioned contract")
    try:
        validate_schema(document, NATIVE_EXECUTION_EVENTS_SCHEMA)
    except SpecValidationError as exc:
        raise TraceError(f"Native execution event envelope schema differs: {exc}") from exc
    header, events = document["execution_header"], document["execution_events"]
    if not isinstance(header, dict) or not isinstance(events, list):
        raise TraceError("Native execution event envelope has invalid ordered collections")
    return {"schema_version": "execution-semantic-trace-v1", "header": header, "events": events}
