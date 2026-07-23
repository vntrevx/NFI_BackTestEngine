"""Common every-candle state projection for official and Rust full verification."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from .canonical import canonical_decimal, read_json
from .errors import TraceError
from .fixture import fixture_input_sha256, validate_fixture
from .state_trace import StateTraceWriter, iter_validated_trace_events

PROJECTED_PHASE = "portfolio.after_candle"
FUTURES_QUOTE_BALANCE_QUANTUM = Decimal("0.000000001")


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
    trading_mode = manifest["freqtrade"]["trading_mode"]
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
                state=_reference_state(state, quote_currency, trading_mode),
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
    trading_mode = manifest["freqtrade"]["trading_mode"]
    source = Path(events_path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line, parse_float=Decimal)
                except json.JSONDecodeError as exc:
                    raise TraceError(f"{source}:{line_number}: invalid engine event JSON") from exc
                writer.append(
                    timestamp_ms=event["timestamp_ms"],
                    phase=PROJECTED_PHASE,
                    pair=event["pair"],
                    state=_engine_state(event["state"], trading_mode),
                )
    finally:
        trailer = writer.close()
    return trailer


def _reference_state(
    state: dict[str, Any],
    quote_currency: str,
    trading_mode: str,
) -> dict[str, Any]:
    wallets = state["wallets"]
    quote = wallets.get(quote_currency, [quote_currency, 0, 0, 0])
    base_balances = (
        []
        if trading_mode == "futures"
        else [
            {
                "currency": currency,
                "free": _decimal(values[1]),
            }
            for currency, values in sorted(wallets.items())
            if currency != quote_currency and Decimal(str(values[1])) != 0
        ]
    )
    counters = state["counters"]
    projected = {
        "quote_free": _quote_balance(quote[1], trading_mode),
        "base_balances": base_balances,
        "open_trade_count": state["open_trade_count"],
        "realized_profit": _decimal(state["total_profit"]),
        "closed_trade_count": len(state["trades"]),
        "rejected_signals": counters["rejected_signals"],
        "trade_id_counter": counters["trade_id"],
        "order_id_counter": counters["order_id"],
    }
    locks = _projected_locks(
        state.get("locks", []),
        timestamp_key="lock_timestamp",
        end_timestamp_key="lock_end_timestamp",
    )
    # Empty lock state was absent from the original exact-parity fixtures.
    # Omitting it preserves those immutable captures while non-empty lists still
    # make protection and pair-lock transitions part of full-state parity.
    if locks:
        projected["locks"] = locks
    return projected


def _engine_state(state: dict[str, Any], trading_mode: str) -> dict[str, Any]:
    projected = {
        "quote_free": _quote_balance(state["quote_free"], trading_mode),
        "base_balances": (
            []
            if trading_mode == "futures"
            else [
                {
                    "currency": balance["currency"],
                    "free": _decimal(balance["free"]),
                }
                for balance in state["base_balances"]
                if Decimal(str(balance["free"])) != 0
            ]
        ),
        "open_trade_count": state["open_trade_count"],
        "realized_profit": _decimal(state["realized_profit"]),
        "closed_trade_count": state["closed_trade_count"],
        "rejected_signals": state["rejected_signals"],
        "trade_id_counter": state["trade_id_counter"],
        "order_id_counter": state["order_id_counter"],
    }
    locks = _projected_locks(
        state.get("locks", []),
        timestamp_key="lock_timestamp_ms",
        end_timestamp_key="lock_end_timestamp_ms",
    )
    if locks:
        projected["locks"] = locks
    return projected


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


def _quote_balance(value: Any, trading_mode: str) -> str:
    """Canonicalize sub-exchange-precision futures wallet float noise.

    Freqtrade derives futures free balance as ``wallet total - used`` while the
    native engine derives it from realized PnL and tied stake. Both routes are
    economically identical but can differ by one f64 unit in the twelfth
    decimal place after partial exits. One nano-USDT remains finer than the
    exchange precision while giving both official and native traces one stable
    byte representation.
    """

    if trading_mode != "futures":
        return _decimal(value)
    normalized = Decimal(str(value)).quantize(
        FUTURES_QUOTE_BALANCE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    return _decimal(normalized)


def _projected_locks(
    locks: list[dict[str, Any]],
    *,
    timestamp_key: str,
    end_timestamp_key: str,
) -> list[dict[str, Any]]:
    """Canonicalize lock snapshots independently of container insertion order."""

    projected = [
        {
            "pair": lock["pair"],
            "lock_timestamp_ms": lock[timestamp_key],
            "lock_end_timestamp_ms": lock[end_timestamp_key],
            "reason": lock["reason"],
            "side": lock["side"],
            "active": lock["active"],
        }
        for lock in locks
    ]
    return sorted(
        projected,
        key=lambda lock: (
            lock["lock_timestamp_ms"],
            lock["lock_end_timestamp_ms"],
            lock["pair"],
            lock["side"],
            lock["reason"] or "",
        ),
    )
