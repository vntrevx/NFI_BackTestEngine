"""Common every-candle state projection for official and Rust full verification."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_decimal, read_json
from .errors import TraceError
from .fixture import fixture_input_sha256, validate_fixture
from .state_trace import StateTraceWriter, iter_validated_trace_events

PROJECTED_PHASE = "portfolio.after_candle"


def project_reference_trace(
    manifest_path: str | Path,
    destination: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    manifest = manifest or validate_fixture(
        manifest_file,
        validate_trace_semantics=False,
    )
    root = manifest_file.parent
    trace_path = root / manifest["artifacts"]["state_trace"]["path"]
    config = read_json(root / _one_input(manifest, "config")["path"])
    quote_currency = config["stake_currency"]
    writer = _projection_writer(manifest, destination, source="freqtrade-projection")
    try:
        for record in iter_validated_trace_events(trace_path):
            if record.get("phase") != "candle.after":
                continue
            state = record.get("state")
            if not isinstance(state, dict):
                raise TraceError("reference full projection requires materialized fixture state")
            writer.append(
                timestamp_ms=record["timestamp_ms"],
                phase=PROJECTED_PHASE,
                pair=record["pair"],
                state=_reference_state(state, quote_currency),
            )
    finally:
        trailer = writer.close()
    return trailer


def project_engine_events(
    manifest_path: str | Path,
    events_path: str | Path,
    destination: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    manifest = manifest or validate_fixture(
        manifest_file,
        validate_trace_semantics=False,
    )
    writer = _projection_writer(manifest, destination, source="engine-projection")
    source = Path(events_path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line, parse_float=Decimal)
                except json.JSONDecodeError as exc:
                    raise TraceError(
                        f"{source}:{line_number}: invalid engine event JSON"
                    ) from exc
                writer.append(
                    timestamp_ms=event["timestamp_ms"],
                    phase=PROJECTED_PHASE,
                    pair=event["pair"],
                    state=_engine_state(event["state"]),
                )
    finally:
        trailer = writer.close()
    return trailer


def _reference_state(state: dict[str, Any], quote_currency: str) -> dict[str, Any]:
    wallets = state["wallets"]
    quote = wallets.get(quote_currency, [quote_currency, 0, 0, 0])
    base_balances = [
        {
            "currency": currency,
            "free": _decimal(values[1]),
        }
        for currency, values in sorted(wallets.items())
        if currency != quote_currency and Decimal(str(values[1])) != 0
    ]
    counters = state["counters"]
    return {
        "quote_free": _decimal(quote[1]),
        "base_balances": base_balances,
        "open_trade_count": state["open_trade_count"],
        "realized_profit": _decimal(state["total_profit"]),
        "closed_trade_count": len(state["trades"]),
        "rejected_signals": counters["rejected_signals"],
        "trade_id_counter": counters["trade_id"],
        "order_id_counter": counters["order_id"],
    }


def _engine_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "quote_free": _decimal(state["quote_free"]),
        "base_balances": [
            {
                "currency": balance["currency"],
                "free": _decimal(balance["free"]),
            }
            for balance in state["base_balances"]
            if Decimal(str(balance["free"])) != 0
        ],
        "open_trade_count": state["open_trade_count"],
        "realized_profit": _decimal(state["realized_profit"]),
        "closed_trade_count": state["closed_trade_count"],
        "rejected_signals": state["rejected_signals"],
        "trade_id_counter": state["trade_id_counter"],
        "order_id_counter": state["order_id_counter"],
    }


def _projection_writer(
    manifest: dict[str, Any],
    destination: str | Path,
    *,
    source: str,
) -> StateTraceWriter:
    strategy = _one_input(manifest, "strategy")
    config = _one_input(manifest, "config")
    return StateTraceWriter(
        destination,
        source=source,
        run_id=manifest["fixture_id"],
        input_sha256=fixture_input_sha256(manifest["inputs"]),
        strategy_sha256=strategy["sha256"],
        profile_sha256=config["sha256"],
        trading_mode=manifest["freqtrade"]["trading_mode"],
        include_state=True,
    )


def _one_input(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == role]
    if len(candidates) != 1:
        raise TraceError(f"fixture requires exactly one {role!r} input")
    return candidates[0]


def _decimal(value: Any) -> str:
    result = canonical_decimal(value, path="$projection")
    assert result is not None
    return result
