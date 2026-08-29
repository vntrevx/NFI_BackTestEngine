"""Narrow, lossless boundary from Native portfolio events to proof traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import TraceError

NATIVE_PORTFOLIO_EVENTS_VERSION = "native-portfolio-events-v1"
_NATIVE_HEADER_FIELDS = {
    "fixture_id",
    "fixture_manifest_sha256",
    "scheduler_contract_sha256",
    "scheduler_contract_fingerprint",
    "portfolio_contract_sha256",
    "portfolio_contract_fingerprint",
    "source_sha256",
    "config_sha256",
    "data_sha256",
    "official_trace_sha256",
    "native_input_sha256",
    "native_timerange",
    "configured_pairs",
    "slot_limit",
    "native_binary_sha256",
}
_EVENT_REQUIRED = {
    "schema_version",
    "sequence",
    "timestamp_ms",
    "boundary",
    "pair",
    "configured_pair_index",
    "processing_order_index",
    "state_before",
    "state_after",
}
_EVENT_OPTIONAL = {
    "rejection_reason",
    "allocated_trade_id",
    "allocated_order_id",
    "proposed_stake",
    "compounding_base",
    "partial_exit_slot_retained",
    "force_exit_index",
    "force_exit_trade_id",
    "force_exit_order_ids",
}


def adapt_native_portfolio_boundary_envelope(document: Mapping[str, Any]) -> dict[str, Any]:
    """Consume the direct Rust envelope without sorting, defaults, or reconstruction."""
    expected = {
        "schema_version",
        "portfolio_header",
        "portfolio_events",
        "final_force_exit_trade_ids",
        "final_trades",
    }
    if (
        document.get("schema_version") != NATIVE_PORTFOLIO_EVENTS_VERSION
        or set(document) != expected
    ):
        raise TraceError("Native portfolio event envelope differs from its versioned contract")
    header = document["portfolio_header"]
    events = document["portfolio_events"]
    final_ids = document["final_force_exit_trade_ids"]
    trades = document["final_trades"]
    if not isinstance(header, dict) or set(header) != _NATIVE_HEADER_FIELDS:
        raise TraceError("Native portfolio event header differs from its versioned contract")
    if (
        not isinstance(events, list)
        or not isinstance(final_ids, list)
        or not isinstance(trades, list)
    ):
        raise TraceError("Native portfolio event envelope has invalid ordered collections")
    for sequence, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or set(event) - _EVENT_REQUIRED - _EVENT_OPTIONAL
            or not set(event) >= _EVENT_REQUIRED
            or event.get("schema_version") != "portfolio-mutation-event-v1"
            or event.get("sequence") != sequence
        ):
            raise TraceError(f"Native portfolio mutation event {sequence} differs")
    return dict(document)


def adapt_native_portfolio_events(document: Mapping[str, Any]) -> dict[str, Any]:
    """Map only the legacy proof envelope; never derive, sort, or default fields."""
    version = document.get("schema_version")
    if version == "portfolio-semantic-trace-v1":
        return dict(document)
    expected = {
        "schema_version",
        "portfolio_header",
        "portfolio_events",
        "final_force_exit_trade_ids",
        "final_trades",
    }
    if version != NATIVE_PORTFOLIO_EVENTS_VERSION or set(document) != expected:
        raise TraceError("Native portfolio event envelope differs from its versioned contract")
    header = document["portfolio_header"]
    events = document["portfolio_events"]
    final_ids = document["final_force_exit_trade_ids"]
    trades = document["final_trades"]
    if not isinstance(header, dict) or not isinstance(events, list):
        raise TraceError("Native portfolio event envelope has invalid header or event list")
    if not isinstance(final_ids, list) or not isinstance(trades, list):
        raise TraceError("Native portfolio event envelope has invalid final state")
    return {
        "schema_version": "portfolio-semantic-trace-v1",
        "header": header,
        "events": events,
        "final_force_exit_trade_ids": final_ids,
        "final_trades": trades,
    }
