"""Canonical changed-signal surfaces reconstructed from raw producer records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, assert_never

from .canonical import read_json
from .errors import NormalizationError, SpecValidationError, TraceError
from .generic_adapter import _surface_trade
from .normalize import normalize_freqtrade_result, read_freqtrade_export
from .state_trace import read_state_trace
from .trace_projection import PROJECTED_PHASE, _engine_state, _reference_state

StateSource = Literal["official", "native", "projection"]


@dataclass(frozen=True, slots=True)
class ReconstructedLane:
    """Trade and full-state rows derived without normalized-cache authority."""

    trades: list[dict[str, Any]]
    full_state: list[dict[str, Any]]


def reconstruct_official_lane(
    manifest_path: Path,
    execution_path: Path,
    raw_trace_path: Path,
) -> ReconstructedLane:
    """Derive official trades and state from the ZIP and freqtrade-reference trace."""
    manifest = read_json(manifest_path)
    config = _config(manifest_path, manifest)
    try:
        execution = read_freqtrade_export(execution_path)
        trace = read_state_trace(raw_trace_path)
    except (NormalizationError, TraceError) as exc:
        raise SpecValidationError("changed signal official raw replay is malformed") from exc
    if not isinstance(execution, Mapping) or trace.header.get("source") != "freqtrade-reference":
        raise SpecValidationError("changed signal official raw producer differs")
    trades = normalize_freqtrade_result(
        execution,
        strategy="CurrentChangedPredicateContract",
        surface_version="2",
    )["trades"]
    return ReconstructedLane(
        trades=trades,
        full_state=reconstruct_full_state(
            trace.events,
            source="official",
            trading_mode=manifest["freqtrade"]["trading_mode"],
            quote_currency=config["stake_currency"],
        ),
    )


def reconstruct_native_lane(
    manifest_path: Path,
    execution_path: Path,
    raw_events_path: Path,
) -> ReconstructedLane:
    """Derive Native trades and state from simulation result and raw engine events."""
    manifest = read_json(manifest_path)
    execution = read_json(execution_path)
    events = _read_engine_events(raw_events_path)
    trades = [
        _surface_trade(trade, sequence, -0.5)
        for sequence, trade in enumerate(execution.get("trades", []))
    ]
    return ReconstructedLane(
        trades=trades,
        full_state=reconstruct_full_state(
            events,
            source="native",
            trading_mode=manifest["freqtrade"]["trading_mode"],
            quote_currency=None,
        ),
    )


def reconstruct_projection_rows(path: Path) -> list[dict[str, Any]]:
    """Read a normalized trace only for cache-to-reconstruction comparison."""
    try:
        trace = read_state_trace(path)
    except TraceError as exc:
        raise SpecValidationError("changed signal state projection is malformed") from exc
    return reconstruct_full_state(
        trace.events,
        source="projection",
        trading_mode=trace.header["trading_mode"],
        quote_currency=None,
    )


def reconstruct_full_state(
    events: Iterable[Mapping[str, Any]],
    *,
    source: StateSource,
    trading_mode: str,
    quote_currency: str | None,
) -> list[dict[str, Any]]:
    """Purely project and deduplicate canonical trade/order/wallet state rows."""
    rows: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for event in events:
        state = event.get("state")
        if not isinstance(state, dict):
            raise SpecValidationError("changed signal raw state event is not materialized")
        match source:
            case "official":
                if event.get("phase") != "candle.after":
                    continue
                if quote_currency is None:
                    raise SpecValidationError("changed signal official quote identity differs")
                projected = _reference_state(state, quote_currency, trading_mode)
            case "native":
                projected = _engine_state(state, trading_mode)
            case "projection":
                projected = state
            case unreachable:
                assert_never(unreachable)
        if projected == previous:
            continue
        rows.append(_state_row(event, projected))
        previous = projected
    return rows


def _state_row(event: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_ms": event["timestamp_ms"],
        "pair": event["pair"],
        "wallet": {
            "quote_free": state["quote_free"],
            "base_balances": state["base_balances"],
        },
        "orders": {"filled_order_count": state["order_id_counter"]},
        "callbacks": {"phase": PROJECTED_PHASE, "callback": None},
        "open_trades": state["open_trade_count"],
        "execution": {
            "closed_trade_count": state["closed_trade_count"],
            "realized_profit": state["realized_profit"],
            "rejected_signals": state["rejected_signals"],
            "trade_id_counter": state["trade_id_counter"],
        },
    }


def _config(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    candidates = [item for item in manifest["inputs"] if item["role"] == "config"]
    if len(candidates) != 1:
        raise SpecValidationError("changed signal replay config role differs")
    return read_json(manifest_path.parent / candidates[0]["path"])


def _read_engine_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise SpecValidationError(
                            "changed signal Native raw event is not an object"
                        )
                    events.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecValidationError("changed signal Native raw events are malformed") from exc
    return events
