"""Materialized Native semantic-event traces and fail-first exact verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_decimal, write_json
from .errors import TraceError
from .fixture import fixture_input_sha256, sha256_file, validate_fixture
from .state_trace import (
    StateTraceWriter,
    first_trace_difference,
    iter_validated_trace_events,
    trace_summary,
)

COMPLETE_SEMANTIC_TRACE_VERSION = "complete-semantic-trace-v1"
COMPLETE_SEMANTIC_PHASE = "semantic.after_pair"
_REQUIRED_SECTIONS = {
    "balances",
    "scheduler",
    "trades",
    "orders",
    "custom_state",
    "callbacks",
    "execution",
    "funding",
    "liquidation",
    "protections",
}
_NATIVE_EVENT_FIELDS = {
    "schema_version",
    "timestamp_ms",
    "pair",
    "state",
    "callback_events",
    "portfolio_events",
    "execution_events",
}
_NATIVE_STATE_FIELDS = {
    "quote_total",
    "quote_free",
    "quote_used",
    "tied_up_stake",
    "realized_wallet_profit",
    "base_balances",
    "configured_pair_index",
    "processing_order_index",
    "candle_index",
    "next_candle_index",
    "occupied_slots",
    "slot_limit",
    "open_trade_count",
    "open_trade_ids",
    "open_trade_pairs",
    "open_order_ids",
    "open_trades",
    "realized_profit",
    "closed_trade_count",
    "closed_trades",
    "rejected_signals",
    "trade_id_counter",
    "order_id_counter",
    "locks",
}
_NATIVE_ORDER_FIELDS = {
    "id",
    "funding_fee",
    "sequence",
    "side",
    "is_entry",
    "filled_timestamp_ms",
    "amount",
    "price",
    "cost",
    "tag",
}
_NATIVE_OPEN_TRADE_FIELDS = {
    "id",
    "pair_index",
    "pair",
    "is_short",
    "leverage",
    "amount_step",
    "price_step",
    "open_timestamp_ms",
    "open_rate",
    "amount",
    "stake_amount",
    "max_stake_amount",
    "entry_cost_with_fees",
    "first_entry_cost_with_fees",
    "adjustment_count",
    "entry_tag",
    "funding_fees",
    "funding_fees_total",
    "funding_sum_high",
    "funding_sum_low",
    "funding_rebase_seed",
    "realized_partial_profit",
    "liquidation_price",
    "liquidation_price_is_explicit",
    "initial_stop_loss",
    "stop_loss",
    "custom_stop_loss_ratio",
    "minimum_rate",
    "maximum_rate",
    "orders",
    "custom_data",
}
_NATIVE_CLOSED_TRADE_FIELDS = {
    "sequence",
    "id",
    "pair",
    "is_short",
    "leverage",
    "open_timestamp_ms",
    "close_timestamp_ms",
    "open_rate",
    "close_rate",
    "amount",
    "stake_amount",
    "max_stake_amount",
    "profit_ratio",
    "profit_abs",
    "entry_tag",
    "exit_reason",
    "fee_open",
    "fee_close",
    "funding_fees",
    "liquidation_price",
    "initial_stop_loss",
    "stop_loss",
    "minimum_rate",
    "maximum_rate",
    "orders",
}
_NATIVE_PUBLIC_ORDER_FIELDS = _NATIVE_ORDER_FIELDS - {"id", "funding_fee"}


def materialize_native_complete_trace(
    manifest_path: str | Path,
    events_path: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    """Retain every Native semantic payload plus complete pair-event state."""
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file, validate_trace_semantics=False)
    strategy = _one_input(manifest, "strategy")
    profile_sha = _profile_sha(manifest, manifest_file)
    writer = StateTraceWriter(
        destination,
        source="native-complete-semantic-observer",
        run_id=manifest["fixture_id"],
        input_sha256=fixture_input_sha256(manifest["inputs"]),
        strategy_sha256=strategy["sha256"],
        profile_sha256=profile_sha,
        trading_mode=manifest["freqtrade"]["trading_mode"],
    )
    try:
        for event in _native_events(events_path):
            writer.append(
                timestamp_ms=event["timestamp_ms"],
                phase=COMPLETE_SEMANTIC_PHASE,
                pair=event["pair"],
                state=_native_complete_state(event),
            )
    finally:
        trailer = writer.close()

    output = Path(destination)
    return {
        "schema_version": COMPLETE_SEMANTIC_TRACE_VERSION,
        "source": "native-complete-semantic-observer",
        "path": str(output),
        "sha256": sha256_file(output),
        "event_count": trailer["event_count"],
        "stream_hash": trailer["stream_hash"],
        "materialized": True,
    }


def verify_complete_semantic_traces(
    expected_path: str | Path,
    actual_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate complete records and report the first exact field or event difference."""
    expected_summary = _validate_complete_trace(expected_path, "expected")
    actual_summary = _validate_complete_trace(actual_path, "actual")
    difference = first_trace_difference(expected_path, actual_path)
    report: dict[str, Any] = {
        "schema_version": COMPLETE_SEMANTIC_TRACE_VERSION,
        "exact": difference is None,
        "expected_event_count": expected_summary["event_count"],
        "actual_event_count": actual_summary["event_count"],
        "difference": _difference_record(difference) if difference is not None else None,
    }
    identity = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    report["fingerprint"] = hashlib.sha256(identity).hexdigest()
    if output_path is not None:
        write_json(output_path, report)
    return report


def _difference_record(difference: Any) -> dict[str, Any]:
    record = asdict(difference)
    record["expected"] = _report_value(record["expected"])
    record["actual"] = _report_value(record["actual"])
    return record


def _report_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, list):
        return [_report_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _report_value(item) for key, item in value.items()}
    return {"$missing": True}


def _native_events(path: str | Path) -> Iterator[dict[str, Any]]:
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise TraceError(f"{source}:{line_number}: invalid Native event JSON") from exc
            if not isinstance(event, dict) or set(event) != _NATIVE_EVENT_FIELDS:
                raise TraceError(f"{source}:{line_number}: Native complete event fields differ")
            state = event.get("state")
            if not isinstance(state, dict) or set(state) != _NATIVE_STATE_FIELDS:
                raise TraceError(f"{source}:{line_number}: Native complete state fields differ")
            yield event


def _native_complete_state(event: Mapping[str, Any]) -> dict[str, Any]:
    state = event["state"]
    assert isinstance(state, dict)
    open_trades = state["open_trades"]
    closed_trades = state["closed_trades"]
    if not isinstance(open_trades, list) or not isinstance(closed_trades, list):
        raise TraceError("Native complete trades must be arrays")
    for trade_index, trade in enumerate(open_trades):
        _require_fields(
            trade,
            _NATIVE_OPEN_TRADE_FIELDS,
            f"Native open trade {trade_index}",
        )
        _require_orders(trade["orders"], f"Native open trade {trade_index}")
    for trade_index, closed in enumerate(closed_trades):
        closed_record = _require_fields(
            closed,
            {"trade", "orders"},
            f"Native closed trade {trade_index}",
        )
        closed_trade = _require_fields(
            closed_record["trade"],
            _NATIVE_CLOSED_TRADE_FIELDS,
            f"Native closed trade {trade_index}.trade",
        )
        _require_orders(closed_record["orders"], f"Native closed trade {trade_index}")
        public_orders = closed_trade["orders"]
        if not isinstance(public_orders, list):
            raise TraceError(f"Native closed trade {trade_index}.trade.orders must be an array")
        for order_index, order in enumerate(public_orders):
            _require_fields(
                order,
                _NATIVE_PUBLIC_ORDER_FIELDS,
                f"Native closed trade {trade_index}.trade order {order_index}",
            )

    orders = [
        {"trade_id": trade["id"], "status": "open", "records": trade["orders"]}
        for trade in open_trades
    ]
    orders.extend(
        {
            "trade_id": closed["trade"]["id"],
            "status": "closed",
            "records": closed["orders"],
        }
        for closed in closed_trades
    )
    custom_state = [
        {"trade_id": trade["id"], "custom_data": trade["custom_data"]}
        for trade in open_trades
    ]

    funding = [
        {
            "trade_id": trade["id"],
            "funding_fees": trade["funding_fees"],
            "funding_fees_total": trade["funding_fees_total"],
            "sum_high": trade["funding_sum_high"],
            "sum_low": trade["funding_sum_low"],
            "rebase_seed": trade["funding_rebase_seed"],
            "order_segments": [order["funding_fee"] for order in trade["orders"]],
        }
        for trade in open_trades
    ]
    funding.extend(
        {
            "trade_id": closed["trade"]["id"],
            "funding_fees": closed["trade"]["funding_fees"],
            "order_segments": [order["funding_fee"] for order in closed["orders"]],
        }
        for closed in closed_trades
    )
    liquidation = [
        {
            "trade_id": trade["id"],
            "price": trade["liquidation_price"],
            "explicit": trade["liquidation_price_is_explicit"],
        }
        for trade in open_trades
    ]
    liquidation.extend(
        {
            "trade_id": closed["trade"]["id"],
            "price": closed["trade"]["liquidation_price"],
            "explicit": None,
        }
        for closed in closed_trades
    )
    projected = {
        "balances": {
            "quote": {
                "total": state["quote_total"],
                "free": state["quote_free"],
                "used": state["quote_used"],
            },
            "base": state["base_balances"],
            "realized_wallet_profit": state["realized_wallet_profit"],
        },
        "scheduler": {
            "configured_pair_index": state["configured_pair_index"],
            "processing_order_index": state["processing_order_index"],
            "candle_index": state["candle_index"],
            "next_candle_index": state["next_candle_index"],
            "occupied_slots": state["occupied_slots"],
            "slot_limit": state["slot_limit"],
            "open_trade_ids": state["open_trade_ids"],
            "open_trade_pairs": state["open_trade_pairs"],
            "open_order_ids": state["open_order_ids"],
            "trade_id_counter": state["trade_id_counter"],
            "order_id_counter": state["order_id_counter"],
            "rejected_signals": state["rejected_signals"],
            "portfolio_events": event["portfolio_events"],
        },
        "trades": {"open": open_trades, "closed": closed_trades},
        "orders": orders,
        "custom_state": custom_state,
        "callbacks": event["callback_events"],
        "execution": event["execution_events"],
        "funding": funding,
        "liquidation": liquidation,
        "protections": {"locks": state["locks"]},
    }
    return _canonicalize(projected, "$.state")


def _require_orders(value: Any, path: str) -> None:
    if not isinstance(value, list):
        raise TraceError(f"{path}.orders must be an array")
    for order_index, order in enumerate(value):
        _require_fields(order, _NATIVE_ORDER_FIELDS, f"{path} order {order_index}")


def _require_fields(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise TraceError(f"{path} fields differ")
    return value



def _canonicalize(value: Any, path: str) -> Any:
    if isinstance(value, Decimal):
        return canonical_decimal(value, path=path)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, list):
        return [_canonicalize(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item, f"{path}.{key}")
            for key, item in value.items()
        }
    raise TraceError(f"{path}: unsupported complete trace value {type(value).__name__}")


def _validate_complete_trace(path: str | Path, label: str) -> dict[str, Any]:
    summary = trace_summary(path)
    if not summary["include_state"]:
        raise TraceError(f"{label} complete trace lacks materialized source records")
    for event in iter_validated_trace_events(path):
        state = event.get("state")
        if not isinstance(state, dict) or set(state) != _REQUIRED_SECTIONS:
            raise TraceError(f"{label} complete trace event {event['sequence']} sections differ")
    return summary


def _one_input(manifest: Mapping[str, Any], role: str) -> dict[str, Any]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise TraceError("fixture inputs must be an array")
    matches = [item for item in inputs if isinstance(item, dict) and item.get("role") == role]
    if len(matches) != 1:
        raise TraceError(f"fixture requires exactly one {role!r} input")
    return matches[0]


def _profile_sha(manifest: Mapping[str, Any], manifest_path: Path) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise TraceError("fixture artifacts must be an object")
    state_trace = artifacts.get("state_trace")
    if not isinstance(state_trace, dict) or not isinstance(state_trace.get("path"), str):
        raise TraceError("fixture lacks a materialized reference state trace")
    source = manifest_path.parent / state_trace["path"]
    summary = trace_summary(source)
    profile_sha = summary.get("profile_sha256")
    if not isinstance(profile_sha, str) or len(profile_sha) != 64:
        raise TraceError("fixture reference trace profile digest is invalid")
    return profile_sha
