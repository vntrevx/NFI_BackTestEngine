"""Canonical event projection for traces captured by official Freqtrade."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError, TraceError
from .fixture import fixture_input_sha256, sha256_file, validate_fixture
from .freqtrade_semantic_profile import load_freqtrade_semantic_profile
from .specs import SEMANTIC_OBSERVER_REPORT_SCHEMA, validate_schema
from .state_trace import StateTraceWriter, iter_validated_trace_events, trace_summary
from .trace_projection import _reference_state

SEMANTIC_OBSERVER_REPORT_VERSION = "semantic-observer-report-v1"


def project_official_semantic_trace(
    manifest_path: str | Path,
    profile_path: str | Path,
    destination: str | Path,
    *,
    report_path: str | Path | None = None,
    source_trace_path: str | Path | None = None,
) -> dict[str, Any]:
    """Project official callback/state events under one digest-bound profile."""
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file, validate_trace_semantics=False)
    profile = load_freqtrade_semantic_profile(profile_path)
    _validate_fixture_identity(manifest, profile)
    root = manifest_file.parent

    if source_trace_path is None:
        root = manifest_file.parent
        trace_record = manifest["artifacts"].get("state_trace")
        if not isinstance(trace_record, dict) or not isinstance(trace_record.get("path"), str):
            raise TraceError("official semantic observer requires a captured state trace")
        source_trace = root / trace_record["path"]
    else:
        source_trace = Path(source_trace_path).resolve()
    source_summary = trace_summary(source_trace)
    if not source_summary["include_state"]:
        raise TraceError("official semantic observer requires materialized reference state")

    config = read_json(root / _one_input(manifest, "config")["path"])
    quote_currency = config.get("stake_currency") if isinstance(config, dict) else None
    if not isinstance(quote_currency, str) or not quote_currency:
        raise TraceError("official semantic observer requires config.stake_currency")
    trading_mode = manifest["freqtrade"]["trading_mode"]
    allowed_events = {
        event["phase"]: set(event["callbacks"])
        for event in profile["observer"]["events"]
    }
    strategy = _one_input(manifest, "strategy")
    writer = StateTraceWriter(
        destination,
        source="freqtrade-semantic-observer",
        run_id=manifest["fixture_id"],
        input_sha256=fixture_input_sha256(manifest["inputs"]),
        strategy_sha256=strategy["sha256"],
        profile_sha256=profile["fingerprint"],
        trading_mode=trading_mode,
        include_state=True,
    )
    phase_counts: Counter[str] = Counter()
    callback_counts: Counter[str] = Counter()
    try:
        for event in iter_validated_trace_events(source_trace):
            phase = event["phase"]
            callback = event["callback"]
            _validate_observed_event(allowed_events, phase, callback)
            state = event.get("state")
            if not isinstance(state, dict):
                raise TraceError("official semantic observer requires materialized event state")
            writer.append(
                timestamp_ms=event["timestamp_ms"],
                phase=phase,
                pair=event["pair"],
                callback=callback,
                state=_complete_reference_state(state, quote_currency, trading_mode),
            )
            phase_counts[phase] += 1
            if callback is not None:
                callback_counts[callback] += 1
    finally:
        output_trailer = writer.close()

    destination_path = Path(destination)
    report: dict[str, Any] = {
        "schema_version": SEMANTIC_OBSERVER_REPORT_VERSION,
        "semantic_profile_sha256": profile["fingerprint"],
        "fixture_id": manifest["fixture_id"],
        "trading_mode": trading_mode,
        "source_trace": {
            "sha256": sha256_file(source_trace),
            "stream_hash": source_summary["stream_hash"],
            "event_count": source_summary["event_count"],
        },
        "projected_trace": {
            "sha256": sha256_file(destination_path),
            "stream_hash": output_trailer["stream_hash"],
            "event_count": output_trailer["event_count"],
        },
        "phase_counts": [
            {"phase": phase, "count": phase_counts[phase]}
            for phase in sorted(phase_counts)
        ],
        "callback_counts": [
            {"callback": callback, "count": callback_counts[callback]}
            for callback in sorted(callback_counts)
        ],
    }
    report["fingerprint"] = _report_fingerprint(report)
    validate_schema(report, SEMANTIC_OBSERVER_REPORT_SCHEMA)
    if report_path is not None:
        write_json(report_path, report)
    return report


def _complete_reference_state(
    state: dict[str, Any],
    quote_currency: str,
    trading_mode: str,
) -> dict[str, Any]:
    trades = state.get("trades")
    locks = state.get("locks")
    counters = state.get("counters")
    balances = state.get("wallets")
    if not isinstance(trades, list):
        raise TraceError("official semantic state requires materialized trades")
    if not isinstance(locks, list):
        raise TraceError("official semantic state requires materialized locks")
    if not isinstance(counters, dict):
        raise TraceError("official semantic state requires materialized counters")
    if not isinstance(balances, dict):
        raise TraceError("official semantic state requires materialized balances")

    orders: list[dict[str, Any]] = []
    funding: list[dict[str, Any]] = []
    liquidation: list[dict[str, Any]] = []
    custom_state: list[dict[str, Any]] = []
    for trade_index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            raise TraceError("official semantic state trade must be an object")
        trade_orders = trade.get("orders", [])
        if not isinstance(trade_orders, list):
            raise TraceError("official semantic state trade orders must be an array")
        orders.append({"trade_index": trade_index, "records": trade_orders})
        funding.append(
            {
                "trade_index": trade_index,
                "funding_fees": trade.get("funding_fees"),
                "order_funding_fees": [
                    order.get("funding_fee") if isinstance(order, dict) else None
                    for order in trade_orders
                ],
            }
        )
        liquidation.append(
            {
                "trade_index": trade_index,
                "liquidation_price": trade.get("liquidation_price"),
            }
        )
        custom_state.append(
            {
                "trade_index": trade_index,
                "custom_data": trade.get("custom_data"),
            }
        )

    return {
        "balances": balances,
        "portfolio": _reference_state(state, quote_currency, trading_mode),
        "scheduler": {
            "open_trade_count": state.get("open_trade_count"),
            "counters": counters,
        },
        "trades": trades,
        "orders": orders,
        "custom_state": custom_state,
        "funding": funding,
        "liquidation": liquidation,
        "protections": {"locks": locks},
    }


def _validate_fixture_identity(manifest: dict[str, Any], profile: dict[str, Any]) -> None:
    freqtrade = manifest["freqtrade"]
    reference = profile["reference"]
    expected = {
        "version": reference["version"],
        "image": reference["image"],
        "image_index_digest": reference["image_index_digest"],
        "image_platform_digest": reference["image_platform_digest"],
        "platform": reference["platform"],
        "tracer_version": profile["observer"]["tracer_version"],
    }
    for field, value in expected.items():
        if freqtrade.get(field) != value:
            raise SpecValidationError(
                f"official fixture {field} differs from semantic profile: "
                f"expected {value!r}, actual {freqtrade.get(field)!r}"
            )


def _validate_observed_event(
    allowed_events: dict[str, set[str]],
    phase: str,
    callback: str | None,
) -> None:
    callbacks = allowed_events.get(phase)
    if callbacks is None:
        raise TraceError(f"official observer emitted unprofiled phase {phase!r}")
    if callback is not None and callback not in callbacks:
        raise TraceError(
            f"official observer emitted unprofiled callback {callback!r} for phase {phase!r}"
        )


def _one_input(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == role]
    if len(candidates) != 1:
        raise TraceError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


def _report_fingerprint(report: dict[str, Any]) -> str:
    identity = {key: value for key, value in report.items() if key != "fingerprint"}
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
