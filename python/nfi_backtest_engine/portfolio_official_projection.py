"""Project authenticated official portfolio captures into Native boundary semantics."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .errors import TraceError


def project_official_portfolio_boundaries(
    trace: Mapping[str, Any],
    surface: Mapping[str, Any],
    *,
    slot_limit: int,
    contract: Mapping[str, Any],
    authentication: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Preserve official emission order while projecting each production boundary."""
    _validate_projection_contract(trace, contract, authentication)
    configured = trace.get("configured_pair_order")
    raw_events = trace.get("events")
    callbacks = trace.get("callbacks")
    trades = surface.get("trades")
    if not isinstance(configured, list) or not isinstance(raw_events, list):
        raise TraceError("official portfolio capture structure differs")
    if not isinstance(callbacks, list) or not isinstance(trades, list):
        raise TraceError("official portfolio capture final state differs")
    after_events = [
        event
        for event in raw_events
        if isinstance(event, dict) and event.get("phase") == "candle.after"
    ]
    if not after_events:
        raise TraceError("official portfolio capture has no candle boundaries")
    phase_index: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in raw_events:
        if isinstance(event, dict):
            phase_index[(event["timestamp_ms"], event["pair"], event["phase"])].append(event)
    callback_index: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for callback in callbacks:
        if not isinstance(callback, dict):
            raise TraceError("official callback event differs")
        callback_index[
            (callback["timestamp_ms"], callback.get("pair", ""), callback["callback"])
        ].append(callback)
    trade_by_id = {index + 1: trade for index, trade in enumerate(trades)}
    open_trades: list[dict[str, Any]] = []
    closed_ids: list[int] = []
    partial_profit: dict[int, float] = {}
    previous = _empty_official_state(after_events[0]["state"], slot_limit)
    projected: list[dict[str, Any]] = []
    positions: dict[int, int] = defaultdict(int)

    for event in after_events:
        timestamp = event["timestamp_ms"]
        pair = event["pair"]
        position = positions[timestamp]
        positions[timestamp] += 1
        pair_index = _configured_index(configured, pair)
        projected.append(
            _event(
                len(projected),
                timestamp,
                "pair_visit",
                pair,
                pair_index,
                position,
                previous,
                previous,
            )
        )
        raw_after = event["state"]
        entries = phase_index.get((timestamp, pair, "trade.entry"), [])
        exits = phase_index.get((timestamp, pair, "trade.exit_order"), [])
        if entries and exits and raw_after["counters"]["trade_id"] >= previous["next_trade_id"]:
            if len(entries) != 1 or len(exits) != 1:
                raise TraceError("official same-pair entry/exit observable multiplicity differs")
            entry_raw = entries[0]["state"]
            before_entry = previous
            _apply_official_mutation(
                "entry_accepted",
                entry_raw,
                pair,
                trade_by_id,
                open_trades,
                closed_ids,
                partial_profit,
            )
            after_entry = _state(
                entry_raw,
                slot_limit,
                open_trades,
                closed_ids,
                partial_profit,
                trade_by_id,
            )
            after_entry["wallet_free"] = before_entry["wallet_free"] - after_entry[
                "wallet_tied"
            ]
            projected.append(
                _event(
                    len(projected),
                    timestamp,
                    "entry_accepted",
                    pair,
                    pair_index,
                    position,
                    before_entry,
                    after_entry,
                    **_detail(
                        "entry_accepted",
                        before_entry,
                        after_entry,
                        timestamp,
                        pair,
                        callback_index,
                    ),
                )
            )
            _apply_official_mutation(
                "trade_close",
                raw_after,
                pair,
                trade_by_id,
                open_trades,
                closed_ids,
                partial_profit,
            )
            after_close = _state(
                raw_after,
                slot_limit,
                open_trades,
                closed_ids,
                partial_profit,
                trade_by_id,
            )
            projected.append(
                _event(
                    len(projected),
                    timestamp,
                    "trade_close",
                    pair,
                    pair_index,
                    position,
                    after_entry,
                    after_close,
                    **_detail(
                        "trade_close",
                        after_entry,
                        after_close,
                        timestamp,
                        pair,
                        callback_index,
                    ),
                )
            )
            previous = after_close
            continue
        mutation = _mutation_kind(previous, raw_after, callback_index, timestamp, pair)
        _require_mutation_observable(
            mutation,
            timestamp,
            pair,
            phase_index,
            callback_index,
        )
        if mutation is not None:
            before = previous
            _apply_official_mutation(
                mutation,
                raw_after,
                pair,
                trade_by_id,
                open_trades,
                closed_ids,
                partial_profit,
            )
            after = _state(
                raw_after,
                slot_limit,
                open_trades,
                closed_ids,
                partial_profit,
                trade_by_id,
            )
            detail = _detail(
                mutation,
                before,
                after,
                timestamp,
                pair,
                callback_index,
            )
            projected.append(
                _event(
                    len(projected),
                    timestamp,
                    mutation,
                    pair,
                    pair_index,
                    position,
                    before,
                    after,
                    **detail,
                )
            )
            previous = after
        else:
            previous = _state(
                raw_after,
                slot_limit,
                open_trades,
                closed_ids,
                partial_profit,
                trade_by_id,
            )
    if open_trades:
        timestamp = after_events[-1]["timestamp_ms"]
        force_records = phase_index.get((timestamp, "", "trade.exit_order"), [])
        if not force_records:
            force_records = [
                event
                for event in raw_events
                if isinstance(event, dict)
                and event.get("timestamp_ms") == timestamp
                and event.get("phase") == "trade.exit_order"
            ]
        if [event["pair"] for event in force_records] != [
            item["pair"] for item in reversed(open_trades)
        ]:
            raise TraceError(
                "official force-exit observable order differs: "
                f"captured={[event['pair'] for event in force_records]!r}, "
                f"open={[item['pair'] for item in reversed(open_trades)]!r}"
            )
        for force_index, force_record in enumerate(force_records):
            opened = _open_pair(open_trades, force_record["pair"])
            before = previous
            trade_id = opened["trade_id"]
            close_order_id = force_record["state"]["counters"]["order_id"]
            if close_order_id != before["next_order_id"]:
                raise TraceError("official force-exit allocator transition differs")
            opened["order_ids"].append(close_order_id)
            open_trades.remove(opened)
            closed_ids.append(trade_id)
            partial_profit.pop(trade_id, None)
            after = _final_state(
                surface,
                slot_limit,
                open_trades,
                closed_ids,
                partial_profit,
                trade_by_id,
                before["next_trade_id"],
                close_order_id + 1,
                before["rejected_signals"],
            )
            projected.append(
                _event(
                    len(projected),
                    timestamp,
                    "force_exit",
                    opened["pair"],
                    _configured_index(configured, opened["pair"]),
                    force_index,
                    before,
                    after,
                    allocated_order_id=close_order_id,
                    force_exit_index=force_index,
                    force_exit_trade_id=trade_id,
                    force_exit_order_ids=opened["order_ids"],
                )
            )
            previous = after
    return projected


def _validate_projection_contract(
    trace: Mapping[str, Any],
    contract: Mapping[str, Any],
    authentication: Mapping[str, Any],
) -> None:
    if trace.get("schema_version") != "freqtrade-portfolio-pressure-trace-v1":
        raise TraceError("official portfolio capture schema differs")
    if trace.get("source") != "pinned-official-freqtrade-full-state-trace":
        raise TraceError("official portfolio tracer source differs")
    if contract.get("schema_version") != "freqtrade-portfolio-pressure-contract-v1":
        raise TraceError("portfolio projection contract schema differs")
    unsigned = {key: value for key, value in contract.items() if key != "fingerprint"}
    fingerprint = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if contract.get("fingerprint") != fingerprint:
        raise TraceError("portfolio projection contract fingerprint differs")
    closure = contract.get("source_closure")
    if not isinstance(closure, list) or any(
        not isinstance(item, dict)
        or hashlib.sha256(item.get("source", "").encode()).hexdigest()
        != item.get("source_sha256")
        for item in closure
    ):
        raise TraceError("portfolio projection source closure differs")
    if (
        authentication.get("schema_version") != "official-source-authentication-v1"
        or authentication.get("reference", {}).get("tracer_version") != "1.1.0"
        or authentication.get("portfolio_contract", {}).get("sha256")
        != "8d83507e14f6fb5dbcd70133235e7e80360690055476e2bd82c8e67fc71ef0aa"
        or authentication.get("scheduler_contract_fingerprint")
        != contract.get("scheduler_contract", {}).get("fingerprint")
    ):
        raise TraceError("official projection authentication differs")


def _require_mutation_observable(
    mutation: str | None,
    timestamp: int,
    pair: str,
    phases: Mapping[tuple[int, str, str], list[dict[str, Any]]],
    callbacks: Mapping[tuple[int, str, str], list[dict[str, Any]]],
) -> None:
    if mutation is None or mutation == "entry_rejected":
        return
    required_phase = "trade.entry" if mutation == "entry_accepted" else "trade.exit_order"
    if not phases.get((timestamp, pair, required_phase)):
        raise TraceError(
            f"official {mutation} boundary lacks captured {required_phase} observable"
        )
    if mutation == "partial_exit" and not callbacks.get(
        (timestamp, pair, "adjust_trade_position")
    ):
        raise TraceError("official partial_exit boundary lacks captured callback observable")
    if mutation == "trade_close" and not callbacks.get((timestamp, pair, "custom_exit")):
        raise TraceError("official trade_close boundary lacks captured callback observable")


def _mutation_kind(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    callbacks: Mapping[tuple[int, str, str], list[dict[str, Any]]],
    timestamp: int,
    pair: str,
) -> str | None:
    counters = after["counters"]
    if counters["rejected_signals"] > before["rejected_signals"]:
        return "entry_rejected"
    stakes = callbacks.get((timestamp, pair, "custom_stake_amount"), [])
    if any(item.get("decision") == "reject" for item in stakes):
        return "entry_rejected"
    if counters["order_id"] == before["next_order_id"] - 1:
        return None
    open_count = after["open_trade_count"]
    if counters["trade_id"] >= before["next_trade_id"]:
        return "entry_accepted"
    if open_count < before["occupied_slots"]:
        return "trade_close"
    return "partial_exit"


def _apply_official_mutation(
    mutation: str,
    raw_after: Mapping[str, Any],
    pair: str,
    trades: Mapping[int, Mapping[str, Any]],
    open_trades: list[dict[str, Any]],
    closed_ids: list[int],
    partial_profit: dict[int, float],
) -> None:
    counters = raw_after["counters"]
    if mutation == "entry_accepted":
        trade_id = counters["trade_id"]
        open_trades.append(
            {
                "trade_id": trade_id,
                "pair": pair,
                "order_ids": [counters["order_id"]],
                "stake": float(trades[trade_id]["max_stake_amount"]),
            }
        )
    elif mutation == "partial_exit":
        opened = _open_pair(open_trades, pair)
        opened["order_ids"].append(counters["order_id"])
        trade = trades[opened["trade_id"]]
        opened["stake"] = float(trade["stake_amount"])
        maximum = Decimal(str(trade["max_stake_amount"]))
        release_order = trade["orders"][1]
        release = Decimal(str(release_order["amount"])) * Decimal(
            str(release_order["price"])
        )
        remaining = Decimal(str(opened["stake"]))
        partial_profit[opened["trade_id"]] = float(release - (maximum - remaining))
    elif mutation == "trade_close":
        opened = _open_pair(open_trades, pair)
        opened["order_ids"].append(counters["order_id"])
        open_trades.remove(opened)
        closed_ids.append(opened["trade_id"])
        partial_profit.pop(opened["trade_id"], None)


def _detail(
    mutation: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    timestamp: int,
    pair: str,
    callbacks: Mapping[tuple[int, str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    if mutation == "entry_accepted":
        stakes = callbacks.get((timestamp, pair, "custom_stake_amount"), [])
        proposed = _one_stake(stakes, "accept")["proposed_stake"]
        return {
            "allocated_trade_id": after["next_trade_id"] - 1,
            "allocated_order_id": after["next_order_id"] - 1,
            "proposed_stake": proposed,
            "compounding_base": before["wallet_free"] + before["wallet_tied"],
        }
    if mutation == "entry_rejected":
        stakes = callbacks.get((timestamp, pair, "custom_stake_amount"), [])
        rejected = [item for item in stakes if item.get("decision") == "reject"]
        if rejected:
            return {
                "rejection_reason": "stake_precision",
                "proposed_stake": rejected[0]["proposed_stake"],
                "compounding_base": before["wallet_free"] + before["wallet_tied"],
            }
        return {"rejection_reason": "slot_limit"}
    if mutation == "partial_exit":
        return {
            "allocated_order_id": after["next_order_id"] - 1,
            "partial_exit_slot_retained": True,
        }
    if mutation == "trade_close":
        return {"allocated_order_id": after["next_order_id"] - 1}
    return {}


def _state(
    raw: Mapping[str, Any],
    slot_limit: int,
    open_trades: list[dict[str, Any]],
    closed_ids: list[int],
    partial_profit: Mapping[int, float],
    trades: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    counters = raw["counters"]
    return {
        "wallet_free": float(raw["quote_wallet"][1]),
        "wallet_tied": sum((item["stake"] for item in open_trades), 0.0),
        "realized_closed": sum(
            (float(trades[item]["profit"]["absolute"]) for item in closed_ids), 0.0
        ),
        "realized_partial": sum(partial_profit.values(), 0.0),
        "occupied_slots": len(open_trades),
        "slot_limit": slot_limit,
        "open_trade_ids": [item["trade_id"] for item in open_trades],
        "open_trade_pairs": [item["pair"] for item in open_trades],
        "open_order_ids": [order for item in open_trades for order in item["order_ids"]],
        "next_trade_id": counters["trade_id"] + 1,
        "next_order_id": counters["order_id"] + 1,
        "rejected_signals": counters["rejected_signals"],
    }


def _empty_official_state(raw: Mapping[str, Any], slot_limit: int) -> dict[str, Any]:
    return {
        "wallet_free": float(raw["quote_wallet"][1]),
        "wallet_tied": 0.0,
        "realized_closed": 0.0,
        "realized_partial": 0.0,
        "occupied_slots": 0,
        "slot_limit": slot_limit,
        "open_trade_ids": [],
        "open_trade_pairs": [],
        "open_order_ids": [],
        "next_trade_id": 1,
        "next_order_id": 1,
        "rejected_signals": 0,
    }


def _final_state(
    surface: Mapping[str, Any],
    slot_limit: int,
    open_trades: list[dict[str, Any]],
    closed_ids: list[int],
    partial_profit: Mapping[int, float],
    trades: Mapping[int, Mapping[str, Any]],
    next_trade_id: int,
    next_order_id: int,
    rejected: int,
) -> dict[str, Any]:
    return {
        "wallet_free": float(surface["summary"]["final_balance"]),
        "wallet_tied": sum((item["stake"] for item in open_trades), 0.0),
        "realized_closed": sum(
            (float(trades[item]["profit"]["absolute"]) for item in closed_ids), 0.0
        ),
        "realized_partial": sum(partial_profit.values(), 0.0),
        "occupied_slots": len(open_trades),
        "slot_limit": slot_limit,
        "open_trade_ids": [item["trade_id"] for item in open_trades],
        "open_trade_pairs": [item["pair"] for item in open_trades],
        "open_order_ids": [order for item in open_trades for order in item["order_ids"]],
        "next_trade_id": next_trade_id,
        "next_order_id": next_order_id,
        "rejected_signals": rejected,
    }


def _event(
    sequence: int,
    timestamp_ms: int,
    boundary: str,
    pair: str,
    configured_pair_index: int,
    processing_order_index: int,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    **detail: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "portfolio-mutation-event-v1",
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "boundary": boundary,
        "pair": pair,
        "configured_pair_index": configured_pair_index,
        "processing_order_index": processing_order_index,
        "state_before": dict(before),
        "state_after": dict(after),
        **detail,
    }


def _configured_index(configured: list[Any], pair: str) -> int:
    try:
        return configured.index(pair)
    except ValueError as exc:
        raise TraceError(f"official event pair is not configured: {pair}") from exc


def _open_pair(open_trades: list[dict[str, Any]], pair: str) -> dict[str, Any]:
    matches = [item for item in open_trades if item["pair"] == pair]
    if len(matches) != 1:
        raise TraceError(f"official open-trade identity differs for {pair}")
    return matches[0]


def _one_stake(values: list[dict[str, Any]], decision: str) -> dict[str, Any]:
    matches = [item for item in values if item.get("decision") == decision]
    if len(matches) != 1:
        raise TraceError(f"official {decision} stake callback identity differs")
    return matches[0]
