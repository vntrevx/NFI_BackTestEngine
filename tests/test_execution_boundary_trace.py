from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.engine_runtime import run_engine
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.execution_boundary_trace import load_execution_boundary_events


def _input() -> dict[str, Any]:
    candle = {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1.0,
    }
    return {
        "schema_version": "1.0.0",
        "config": {
            "starting_balance": 1000.0,
            "max_open_trades": 1,
            "stake_amount": 100.0,
            "fee_rate": 0.001,
            "entry_order_type": "limit",
            "exit_order_type": "limit",
            "fee_open_rate": 0.001,
            "fee_close_rate": 0.002,
            "stoploss_ratio": -0.99,
            "amount_step": 0.1,
            "price_step": 0.01,
        },
        "pairs": [
            {
                "pair": "TASK16/USDT",
                "execution_start_index": 1,
                "candles": [
                    {"timestamp_ms": 0, **candle},
                    {
                        "timestamp_ms": 1,
                        **candle,
                        "enter_long": {"tag": "task16"},
                    },
                    {
                        "timestamp_ms": 2,
                        **candle,
                        "adjustment": {"stake_amount": 50.0, "tag": "add"},
                    },
                    {
                        "timestamp_ms": 3,
                        **candle,
                        "exit_long": {"reason": "signal_exit"},
                    },
                    {"timestamp_ms": 4, **candle},
                ],
            }
        ],
    }


def test_run_engine_publishes_validated_direct_execution_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "simulation.json"
    result = tmp_path / "result.json"
    trace = tmp_path / "execution-events.jsonl"
    write_json(source, _input())

    report = run_engine(source, result, execution_events_path=trace)
    events = load_execution_boundary_events(trace)

    assert report["events"] is None
    assert report["execution_events"]["path"] == str(trace)
    assert [event["phase"] for event in events] == [
        "entry_candidate",
        "entry_fill",
        "adjustment_fill",
        "exit_competition",
        "exit_confirmation",
        "exit_fill",
    ]
    fills = {event["phase"]: event for event in events if event["phase"].endswith("fill")}
    assert [fills[phase]["order_id"] for phase in fills] == [1, 2, 3]
    assert fills["entry_fill"]["fee_applied"] == "0.1"
    assert fills["adjustment_fill"]["fee_applied"] == "0.05"
    assert fills["exit_fill"]["fee_applied"] == "0.3"
    assert fills["exit_fill"]["state_after"]["open_trade_ids"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda event: event.__setitem__("sequence", 2),
        lambda event: event.__setitem__("unknown", True),
        lambda event: event.__setitem__("fee_applied", "0.10"),
    ],
)
def test_execution_boundary_loader_rejects_contract_drift(
    tmp_path: Path,
    mutation,
) -> None:
    event = {
        "schema_version": "execution-boundary-event-v1",
        "sequence": 0,
        "timestamp_ms": 1,
        "pair": "TASK16/USDT",
        "phase": "entry_candidate",
        "order_type": "limit",
        "candle": {"open": "100", "high": "101", "low": "99", "close": "100"},
        "candidates": ["entry_signal"],
        "intermediates": {},
    }
    mutation(event)
    trace = tmp_path / "execution-events.jsonl"
    trace.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(TraceError):
        load_execution_boundary_events(trace)
