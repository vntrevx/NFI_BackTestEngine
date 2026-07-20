from __future__ import annotations

from typing import Any

from nfi_backtest_engine.trace_projection import _engine_state, _reference_state


def _reference_fixture_state() -> dict[str, Any]:
    return {
        "wallets": {"USDT": ["USDT", "1000", "0", "1000"]},
        "open_trade_count": 0,
        "total_profit": "0",
        "trades": [],
        "counters": {
            "rejected_signals": 0,
            "trade_id": 0,
            "order_id": 0,
        },
        "locks": [],
    }


def _engine_fixture_state() -> dict[str, Any]:
    return {
        "quote_free": "1000",
        "base_balances": [],
        "open_trade_count": 0,
        "realized_profit": "0",
        "closed_trade_count": 0,
        "rejected_signals": 0,
        "trade_id_counter": 0,
        "order_id_counter": 0,
        "locks": [],
    }


def test_empty_locks_preserve_the_original_fixture_projection() -> None:
    reference = _reference_state(_reference_fixture_state(), "USDT")
    engine = _engine_state(_engine_fixture_state())

    assert reference == engine
    assert "locks" not in reference


def test_non_empty_locks_are_included_in_full_state_parity() -> None:
    reference_state = _reference_fixture_state()
    reference_state["locks"] = [
        {
            "pair": "BTC/USDT",
            "lock_timestamp": 1000,
            "lock_end_timestamp": 2000,
            "reason": "MaxDrawdown",
            "side": "long",
            "active": True,
        }
    ]
    engine_state = _engine_fixture_state()
    engine_state["locks"] = [
        {
            "pair": "BTC/USDT",
            "lock_timestamp_ms": 1000,
            "lock_end_timestamp_ms": 2000,
            "reason": "MaxDrawdown",
            "side": "long",
            "active": True,
        }
    ]

    reference = _reference_state(reference_state, "USDT")
    engine = _engine_state(engine_state)

    assert reference == engine
    assert reference["locks"] == [
        {
            "pair": "BTC/USDT",
            "lock_timestamp_ms": 1000,
            "lock_end_timestamp_ms": 2000,
            "reason": "MaxDrawdown",
            "side": "long",
            "active": True,
        }
    ]
