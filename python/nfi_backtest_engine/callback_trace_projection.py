"""Direct semantic projection of invocation-time executable callback events."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import TraceError
from .fixture import validate_fixture
from .trace_projection import _engine_state, _projection_writer


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
    executable_trace = False
    terminal_fill = False
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
                callbacks = event.get("callback_events", [])
                v2_events = [
                    callback
                    for callback in callbacks
                    if callback.get("schema_version") == "callback-semantic-trace-v2"
                ]
                if v2_events:
                    executable_trace = True
                    terminal_fill = _append_v2_events(writer, event, v2_events)
                elif not executable_trace:
                    writer.append(
                        timestamp_ms=event["timestamp_ms"],
                        phase="portfolio.after_candle",
                        pair=event["pair"],
                        state=_engine_state(event["state"], trading_mode),
                    )
                if terminal_fill:
                    break
    finally:
        trailer = writer.close()
    return trailer


def _append_v2_events(writer: Any, event: dict[str, Any], callbacks: list[dict[str, Any]]) -> bool:
    for callback in callbacks:
        for observation in callback.get("observations", []):
            if observation.get("channel") != "strategy_stdout_json":
                continue
            payload = observation.get("payload")
            if not isinstance(payload, dict):
                raise TraceError("v2 callback observation payload must be an object")
            writer.append(
                timestamp_ms=event["timestamp_ms"],
                phase=f"callback.{callback['callback_name']}",
                pair=event["pair"],
                callback=callback["callback_name"],
                state=_v2_callback_state(callback, payload),
            )
            if _is_terminal_fill(callback, payload):
                return True
    return False


def _v2_callback_state(
    callback: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    before, after = payload.get("before"), payload.get("after")
    custom_state = {
        delta["key"]: {
            "before": _semantic_value(delta.get("before")),
            "after": _semantic_value(delta.get("after")),
        }
        for delta in callback.get("custom_state_deltas", [])
    }
    order_delta = None
    trade_delta: dict[str, Any] = {}
    if isinstance(before, dict) and isinstance(after, dict):
        if isinstance(before.get("orders"), int) and isinstance(after.get("orders"), int):
            order_delta = {"before": before["orders"], "after": after["orders"]}
        ignored = {"entries", "exits", "orders", "system_version", "derisk_level_1"}
        for key in sorted(before.keys() & after.keys()):
            if key not in ignored and before[key] != after[key]:
                trade_delta[key] = {
                    "before": _semantic_value(before[key]),
                    "after": _semantic_value(after[key]),
                }
    visible = after if isinstance(after, dict) else payload.get("state")
    return {
        "delta": {
            "custom_state": custom_state,
            "orders": order_delta,
            "trade": trade_delta,
        },
        "predicate": payload.get("predicate"),
        "result": _semantic_value(payload.get("result")),
        "visible_state": _semantic_value(visible) if isinstance(visible, dict) else None,
    }


def _semantic_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    return value


def _is_terminal_fill(callback: dict[str, Any], payload: dict[str, Any]) -> bool:
    if callback.get("callback_name") != "order_filled":
        return False
    before, after = payload.get("before"), payload.get("after")
    return (
        isinstance(before, dict)
        and isinstance(after, dict)
        and isinstance(before.get("exits"), int)
        and isinstance(after.get("exits"), int)
        and after["exits"] > 0
    )
