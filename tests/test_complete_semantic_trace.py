from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.complete_semantic_trace import (
    materialize_native_complete_trace,
    verify_complete_semantic_traces,
)
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.state_trace import StateTraceWriter, read_state_trace

ROOT = Path(__file__).parents[1]
MANIFEST = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20"
    / "manifest.json"
)
HASH = "0" * 64


def test_native_complete_trace_materializes_hidden_semantic_fields(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps(_native_event()) + "\n", encoding="utf-8")
    destination = tmp_path / "complete.nfitrace"

    report = materialize_native_complete_trace(MANIFEST, source, destination)
    state = read_state_trace(destination).events[0]["state"]

    assert report["materialized"] is True
    assert report["event_count"] == 1
    assert state["balances"]["quote"] == {"total": "1000", "free": "750", "used": "250"}
    assert state["scheduler"]["candle_index"] == 7
    assert state["trades"]["open"][0]["custom_data"] == {"mode": "grind"}
    assert state["orders"][0]["records"][0]["id"] == 11
    assert state["funding"][0]["order_segments"] == ["0.25"]
    assert state["liquidation"][0]["price"] == "80"
    assert state["protections"]["locks"][0]["pair"] == "*"
    assert state["callbacks"][0]["predicate_ids"] == ["p7"]
    assert state["execution"][0]["phase"] == "entry_fill"


def test_native_complete_trace_rejects_an_omitted_internal_trade_field(
    tmp_path: Path,
) -> None:
    event = _native_event()
    del event["state"]["open_trades"][0]["adjustment_count"]
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(TraceError, match="Native open trade 0 fields differ"):
        materialize_native_complete_trace(MANIFEST, source, tmp_path / "complete.nfitrace")


def test_complete_trace_cli_materializes_and_verifies(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps(_native_event()) + "\n", encoding="utf-8")
    trace = tmp_path / "complete.nfitrace"
    report = tmp_path / "verification.json"

    assert (
        cli.main(
            [
                "trace",
                "materialize-complete",
                str(MANIFEST),
                str(source),
                "--output",
                str(trace),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "trace",
                "verify-complete",
                str(trace),
                str(trace),
                "--output",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text(encoding="utf-8"))["exact"] is True


@pytest.mark.parametrize(
    ("target", "expected_path"),
    [
        ("order", ".orders"),
        ("custom", ".custom_state"),
        ("funding", ".funding"),
        ("protection", ".protections"),
    ],
)
def test_complete_trace_reports_first_omitted_or_altered_field(
    tmp_path: Path,
    target: str,
    expected_path: str,
) -> None:
    expected_state = _complete_state()
    actual_state = copy.deepcopy(expected_state)
    if target == "order":
        del actual_state["orders"][0]["records"][0]["id"]
    elif target == "custom":
        del actual_state["custom_state"][0]["custom_data"]["mode"]
    elif target == "funding":
        actual_state["funding"][0]["segments"] = ["0.26"]
    else:
        actual_state["protections"]["locks"] = []
    expected = _write_trace(tmp_path / "expected.trace", [expected_state])
    actual = _write_trace(tmp_path / "actual.trace", [actual_state])

    report = verify_complete_semantic_traces(expected, actual)

    assert report["exact"] is False
    assert expected_path in report["difference"]["path"]


def test_complete_trace_reports_an_omitted_semantic_event(tmp_path: Path) -> None:
    state = _complete_state()
    expected = _write_trace(tmp_path / "expected.trace", [state, state])
    actual = _write_trace(tmp_path / "actual.trace", [state])

    report = verify_complete_semantic_traces(expected, actual)

    assert report["exact"] is False
    assert report["difference"]["sequence"] == 1
    assert report["difference"]["path"] == "$.events.length"
    assert report["difference"]["expected"] == 2
    assert report["difference"]["actual"] == 1


def _write_trace(path: Path, states: list[dict[str, Any]]) -> Path:
    with StateTraceWriter(
        path,
        source="complete-test",
        run_id="complete-test",
        input_sha256=HASH,
        strategy_sha256=HASH,
        profile_sha256=HASH,
        trading_mode="futures",
    ) as writer:
        for index, state in enumerate(states):
            writer.append(
                timestamp_ms=1_700_000_000_000 + index,
                phase="semantic.after_pair",
                pair="BTC/USDT:USDT",
                state=state,
            )
    return path


def _complete_state() -> dict[str, Any]:
    return {
        "balances": {"quote": {"total": "1000", "free": "750", "used": "250"}},
        "scheduler": {"candle_index": 7, "occupied_slots": 1, "slot_limit": 1},
        "trades": {"open": [{"id": 1}], "closed": []},
        "orders": [{"trade_id": 1, "records": [{"id": 11, "funding_fee": "0.25"}]}],
        "custom_state": [{"trade_id": 1, "custom_data": {"mode": "grind"}}],
        "callbacks": [{"predicate_ids": ["p7"]}],
        "execution": [{"phase": "entry_fill"}],
        "funding": [{"trade_id": 1, "segments": ["0.25"]}],
        "liquidation": [{"trade_id": 1, "price": "80"}],
        "protections": {"locks": [{"pair": "*"}]},
    }


def _native_event() -> dict[str, Any]:
    order = {
        "id": 11,
        "funding_fee": 0.25,
        "sequence": 0,
        "side": "buy",
        "is_entry": True,
        "filled_timestamp_ms": 1_700_000_000_000,
        "amount": 2.5,
        "price": 100.0,
        "cost": 250.0,
        "tag": "entry",
    }
    trade = {
        "id": 1,
        "pair_index": 0,
        "pair": "BTC/USDT:USDT",
        "is_short": False,
        "leverage": 3.0,
        "amount_step": 0.1,
        "price_step": 0.01,
        "open_timestamp_ms": 1_700_000_000_000,
        "open_rate": 100.0,
        "amount": 2.5,
        "stake_amount": 250.0,
        "max_stake_amount": 250.0,
        "entry_cost_with_fees": 250.125,
        "first_entry_cost_with_fees": 250.125,
        "adjustment_count": 0,
        "entry_tag": "entry",
        "funding_fees": 0.25,
        "funding_fees_total": 0.25,
        "funding_sum_high": 0.25,
        "funding_sum_low": 0.0,
        "funding_rebase_seed": None,
        "realized_partial_profit": 0.0,
        "liquidation_price": 80.0,
        "liquidation_price_is_explicit": False,
        "initial_stop_loss": 90.0,
        "stop_loss": 91.0,
        "custom_stop_loss_ratio": None,
        "minimum_rate": 99.0,
        "maximum_rate": 101.0,
        "orders": [order],
        "custom_data": {"mode": "grind"},
    }
    return {
        "schema_version": "portfolio-scheduler-event-v1",
        "timestamp_ms": 1_700_000_000_000,
        "pair": "BTC/USDT:USDT",
        "state": {
            "quote_total": 1000.0,
            "quote_free": 750.0,
            "quote_used": 250.0,
            "tied_up_stake": 250.0,
            "realized_wallet_profit": 0.0,
            "base_balances": [{"currency": "BTC", "free": 2.5}],
            "configured_pair_index": 0,
            "processing_order_index": 0,
            "candle_index": 7,
            "next_candle_index": 8,
            "occupied_slots": 1,
            "slot_limit": 1,
            "open_trade_count": 1,
            "open_trade_ids": [1],
            "open_trade_pairs": ["BTC/USDT:USDT"],
            "open_order_ids": [11],
            "open_trades": [trade],
            "realized_profit": 0.0,
            "closed_trade_count": 0,
            "closed_trades": [],
            "rejected_signals": 0,
            "trade_id_counter": 1,
            "order_id_counter": 11,
            "locks": [
                {
                    "pair": "*",
                    "lock_timestamp_ms": 1_700_000_000_000,
                    "lock_end_timestamp_ms": 1_700_000_300_000,
                    "reason": "guard",
                    "side": "*",
                    "active": True,
                }
            ],
        },
        "callback_events": [{"predicate_ids": ["p7"]}],
        "portfolio_events": [{"boundary": "entry_accepted"}],
        "execution_events": [{"phase": "entry_fill"}],
    }
