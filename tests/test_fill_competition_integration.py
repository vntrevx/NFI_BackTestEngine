from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from nfi_backtest_engine.engine_runtime import run_engine
from nfi_backtest_engine.execution_boundary_trace import load_execution_boundary_events

_MATRIX = Path("benchmarks/evidence/task16/fill-competition-matrix.json")
_ACCEPTED_EXIT_REASON = {
    "limit-fallthrough": "stop_loss",
    "limit-all-rejected": None,
    "limit-trailing-fallthrough": "trailing_stop_loss",
    "limit-signal-fallthrough": "stop_loss",
    "limit-primary-accept": "task16-primary",
    "market-primary-accept": "task16-primary",
}


def _decimal(value: object) -> str | None:
    if value is None:
        return None
    normalized = format(Decimal(str(value)).normalize(), "f")
    return "0" if normalized == "-0" else normalized


def _confirmation_program(accepted_reason: str | None) -> dict[str, Any]:
    value: dict[str, Any]
    if accepted_reason is None:
        value = {"op": "literal", "value": False}
    else:
        value = {
            "op": "equal",
            "left": {"op": "variable", "name": "exit_reason"},
            "right": {"op": "literal", "value": accepted_reason},
        }
    return {"statements": [{"op": "return", "value": value}], "functions": {}}


def _native_input(lane: str, fixture: Path) -> dict[str, Any]:
    frame = pd.read_feather(fixture / "inputs/data/BTC_USDT-5m.feather")
    candles: list[dict[str, Any]] = []
    timestamps: list[int] = []
    for index, row in frame.iterrows():
        timestamp_ms = int(row["date"].timestamp() * 1000)
        timestamps.append(timestamp_ms)
        candle: dict[str, Any] = {
            "timestamp_ms": timestamp_ms,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        if index == 1:
            candle["enter_long"] = {"tag": "task16-entry"}
        if index == 2:
            reason = "task16-explicit" if lane == "limit-signal-fallthrough" else "task16-primary"
            candle["exit_long"] = {"reason": reason}
        candles.append(candle)

    market = lane == "market-primary-accept"
    trailing = lane == "limit-trailing-fallthrough"
    config: dict[str, Any] = {
        "starting_balance": 1000.0,
        "max_open_trades": 1,
        "stake_amount": 100.0,
        "fee_rate": 0.001,
        "fee_open_rate": 0.001,
        "fee_close_rate": 0.001,
        "entry_order_type": "market" if market else "limit",
        "exit_order_type": "market" if market else "limit",
        "entry_rates_by_pair": {} if market else {"BTC/USDT": {str(timestamps[1]): 99.237}},
        "exit_rates_by_pair": {} if market else {"BTC/USDT": {str(timestamps[2]): 101.237}},
        "minimal_roi": {"0": 0.01},
        "trailing_stop": trailing,
        "trailing_stop_positive": 0.02 if trailing else None,
        "trailing_stop_positive_offset": 0.03 if trailing else None,
        "trailing_only_offset_is_reached": trailing,
        "stoploss_ratio": -0.05,
        "amount_step": 0.00001,
        "price_step": 0.01,
        "exit_confirmation_program": _confirmation_program(_ACCEPTED_EXIT_REASON[lane]),
    }
    return {
        "schema_version": "1.0.0",
        "config": config,
        "pairs": [
            {
                "pair": "BTC/USDT",
                "execution_start_index": 1,
                "amount_step": 0.00001,
                "price_step": 0.01,
                "price_steps": [],
                "candles": candles,
            }
        ],
    }


def _trade_projection(trade: dict[str, Any], *, official: bool) -> dict[str, Any]:
    if official:
        fees = trade["fees"]
        profit = trade["profit"]
        orders = trade["orders"]
        return {
            "sequence": trade["sequence"],
            "pair": trade["pair"],
            "is_short": trade["direction"] == "short",
            "open_timestamp_ms": trade["open_timestamp_ms"],
            "close_timestamp_ms": trade["close_timestamp_ms"],
            "open_rate": _decimal(trade["open_rate"]),
            "close_rate": _decimal(trade["close_rate"]),
            "amount": _decimal(trade["amount"]),
            "stake_amount": _decimal(trade["stake_amount"]),
            "max_stake_amount": _decimal(trade["max_stake_amount"]),
            "leverage": _decimal(trade["leverage"]),
            "entry_tag": trade["entry_tag"],
            "exit_reason": trade["exit_reason"],
            "fee_open": _decimal(fees["open_rate"]),
            "fee_close": _decimal(fees["close_rate"]),
            "funding_fees": _decimal(fees["funding"]),
            "liquidation_price": _decimal(trade["liquidation_price"]),
            "profit_abs": _decimal(profit["absolute"]),
            "profit_ratio": _decimal(profit["ratio"]),
            "initial_stop_loss": _decimal(trade["initial_stop_loss"]),
            "stop_loss": _decimal(trade["stop_loss"]),
            "minimum_rate": _decimal(trade["minimum_rate"]),
            "maximum_rate": _decimal(trade["maximum_rate"]),
            "orders": [
                {
                    "sequence": order["sequence"],
                    "side": order["side"],
                    "is_entry": order["is_entry"],
                    "filled_timestamp_ms": order["filled_timestamp_ms"],
                    "amount": _decimal(order["amount"]),
                    "price": _decimal(order["price"]),
                    "cost": _decimal(order["cost"]),
                    "tag": order["tag"],
                }
                for order in orders
            ],
        }
    return {
        "sequence": trade["sequence"],
        "pair": trade["pair"],
        "is_short": trade["is_short"],
        "open_timestamp_ms": trade["open_timestamp_ms"],
        "close_timestamp_ms": trade["close_timestamp_ms"],
        "open_rate": _decimal(trade["open_rate"]),
        "close_rate": _decimal(trade["close_rate"]),
        "amount": _decimal(trade["amount"]),
        "stake_amount": _decimal(trade["stake_amount"]),
        "max_stake_amount": _decimal(trade["max_stake_amount"]),
        "leverage": _decimal(trade["leverage"]),
        "entry_tag": trade["entry_tag"],
        "exit_reason": trade["exit_reason"],
        "fee_open": _decimal(trade["fee_open"]),
        "fee_close": _decimal(trade["fee_close"]),
        "funding_fees": _decimal(trade["funding_fees"]),
        "liquidation_price": _decimal(trade["liquidation_price"]),
        "profit_abs": _decimal(trade["profit_abs"]),
        "profit_ratio": _decimal(trade["profit_ratio"]),
        "initial_stop_loss": _decimal(trade["initial_stop_loss"]),
        "stop_loss": _decimal(trade["stop_loss"]),
        "minimum_rate": _decimal(trade["minimum_rate"]),
        "maximum_rate": _decimal(trade["maximum_rate"]),
        "orders": [
            {
                "sequence": order["sequence"],
                "side": order["side"],
                "is_entry": order["is_entry"],
                "filled_timestamp_ms": order["filled_timestamp_ms"],
                "amount": _decimal(order["amount"]),
                "price": _decimal(order["price"]),
                "cost": _decimal(order["cost"]),
                "tag": order["tag"],
            }
            for order in trade["orders"]
        ],
    }


def _summary_projection(value: dict[str, Any], *, official: bool) -> dict[str, Any]:
    if official:
        return {
            "starting_balance": _decimal(value["starting_balance"]),
            "final_balance": _decimal(value["final_balance"]),
            "profit_total_abs": _decimal(value["profit_total_abs"]),
            "total_volume": _decimal(value["total_volume"]),
            "rejected_signals": value["rejected_signals"],
            "maximum_concurrent_trades": value["max_open_trades"],
        }
    return {
        "starting_balance": _decimal(value["starting_balance"]),
        "final_balance": _decimal(value["final_balance"]),
        "profit_total_abs": _decimal(value["profit_total_abs"]),
        "total_volume": _decimal(value["total_volume"]),
        "rejected_signals": value["rejected_signals"],
        "maximum_concurrent_trades": value["maximum_concurrent_trades"],
    }


def test_official_fill_competition_lanes_match_direct_native_execution(tmp_path: Path) -> None:
    matrix = json.loads(_MATRIX.read_text(encoding="utf-8"))
    observed_lanes: set[str] = set()
    for lane_spec in matrix["official_fixtures"]:
        lane = lane_spec["lane"]
        if lane not in _ACCEPTED_EXIT_REASON:
            continue
        observed_lanes.add(lane)
        manifest = Path(lane_spec["manifest"])
        fixture = manifest.parent
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        trace_input = next(
            value["path"]
            for value in lane_spec["auxiliary_inputs"]
            if value["schema_version"] == "freqtrade-fill-competition-trace-v1"
        )
        official_trace = json.loads((fixture / trace_input).read_text(encoding="utf-8"))
        official_surface = json.loads(
            (fixture / manifest_value["artifacts"]["trade_surface"]["path"]).read_text(
                encoding="utf-8"
            )
        )

        lane_dir = tmp_path / lane
        lane_dir.mkdir()
        input_path = lane_dir / "input.json"
        result_path = lane_dir / "result.json"
        events_path = lane_dir / "execution-events.jsonl"
        input_path.write_text(json.dumps(_native_input(lane, fixture)), encoding="utf-8")
        run_engine(
            input_path=input_path,
            output_path=result_path,
            execution_events_path=events_path,
        )
        native_result = json.loads(result_path.read_text(encoding="utf-8"))
        native_events = load_execution_boundary_events(events_path)
        official_entry = next(
            event
            for event in official_trace["callbacks"]
            if event["callback"] == "confirm_trade_entry"
        )
        native_entry = next(event for event in native_events if event["phase"] == "entry_fill")
        assert {
            "timestamp_ms": native_entry["timestamp_ms"],
            "order_type": native_entry["order_type"],
            "amount": _decimal(native_entry["amount_output"]),
            "rate": _decimal(native_entry["precision_rate"]),
            "accepted": True,
        } == {
            "timestamp_ms": official_entry["timestamp_ms"],
            "order_type": official_entry["order_type"],
            "amount": _decimal(official_entry["amount"]),
            "rate": _decimal(official_entry["rate"]),
            "accepted": official_entry["result"],
        }
        custom_entry = next(
            (
                event
                for event in official_trace["callbacks"]
                if event["callback"] == "custom_entry_price"
            ),
            None,
        )
        if custom_entry is not None:
            assert _decimal(native_entry["clamped_rate"]) == _decimal(custom_entry["result"])

        official_attempts = [
            {
                "timestamp_ms": event["timestamp_ms"],
                "order_type": event["order_type"],
                "reason": event["exit_reason"],
                "rate": _decimal(event["rate"]),
                "accepted": event["result"],
            }
            for event in official_trace["callbacks"]
            if event["callback"] == "confirm_trade_exit"
        ]
        native_attempts = [
            {
                "timestamp_ms": event["timestamp_ms"],
                "order_type": event["order_type"],
                "reason": event["winner"],
                "rate": _decimal(event["price_input"]),
                "accepted": event["confirmation"],
            }
            for event in native_events
            if event["phase"] == "exit_confirmation"
        ]
        assert native_attempts == official_attempts, lane

        assert _summary_projection(native_result, official=False) == _summary_projection(
            official_surface["summary"], official=True
        ), lane
        assert [_trade_projection(trade, official=False) for trade in native_result["trades"]] == [
            _trade_projection(trade, official=True) for trade in official_surface["trades"]
        ], lane

        official_trade = next(
            (event["trades"][0] for event in reversed(official_trace["events"]) if event["trades"]),
            None,
        )
        if official_trade is None:
            final_order_id = official_trace["events"][-1]["counters"]["order_id"]
            official_order_ids = list(range(1, final_order_id + 1))
        else:
            official_order_ids = [int(order["order_id"]) for order in official_trade["orders"]]
        native_order_ids = [
            event["order_id"]
            for event in native_events
            if event["phase"] in {"entry_fill", "exit_fill"}
        ]
        assert native_order_ids == official_order_ids, lane

    assert observed_lanes == set(_ACCEPTED_EXIT_REASON)
