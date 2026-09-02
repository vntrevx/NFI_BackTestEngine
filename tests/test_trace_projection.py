from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.callback_trace_projection import (
    _is_terminal_fill,
    _v2_callback_state,
)
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.reference_tracer.nfi_reference_trace import (
    REFERENCE_STATE_SCHEMA_VERSION,
)
from nfi_backtest_engine.state_trace import StateTraceWriter, iter_validated_trace_events
from nfi_backtest_engine.trace_projection import (
    _engine_state,
    _quote_balance,
    _reference_state,
    project_reference_trace,
)


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


def test_v2_callback_projection_uses_invocation_observation_and_terminal_fill() -> None:
    payload = {
        "callback": "order_filled",
        "predicate": "sell",
        "result": {"kind": "none"},
        "before": {"entries": 2, "exits": 0, "orders": 3, "stake_amount": Decimal("104.057094")},
        "after": {"entries": 2, "exits": 1, "orders": 3, "stake_amount": Decimal("104.057094")},
    }
    callback = {
        "callback_name": "order_filled",
        "custom_state_deltas": [],
    }

    assert _v2_callback_state(callback, payload) == {
        "delta": {
            "custom_state": {},
            "orders": {"before": 3, "after": 3},
            "trade": {},
        },
        "predicate": "sell",
        "result": {"kind": "none"},
        "visible_state": {
            "entries": 2,
            "exits": 1,
            "orders": 3,
            "stake_amount": "104.057094",
        },
    }
    assert _is_terminal_fill(callback, payload) is True
    payload["before"]["exits"] = 1
    assert _is_terminal_fill(callback, payload) is True


def test_empty_locks_preserve_the_original_fixture_projection() -> None:
    reference = _reference_state(_reference_fixture_state(), "USDT", "spot")
    engine = _engine_state(_engine_fixture_state(), "spot")

    assert reference == engine
    assert "locks" not in reference


def test_v2_reference_projection_counts_only_closed_trade_records() -> None:
    state = _reference_fixture_state()
    state["schema_version"] = REFERENCE_STATE_SCHEMA_VERSION
    state["open_trade_count"] = 1
    state["trades"] = [
        {"id": 1, "is_open": False},
        {"id": 2, "is_open": True},
    ]

    projected = _reference_state(state, "USDT", "spot")

    assert projected["open_trade_count"] == 1
    assert projected["closed_trade_count"] == 1


@pytest.mark.parametrize(
    ("state_update", "message"),
    [
        ({"schema_version": "reference-state-v3"}, "unsupported reference state schema"),
        (
            {
                "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
                "open_trade_count": 1,
                "trades": [{"id": 1}],
            },
            "requires boolean is_open",
        ),
        (
            {
                "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
                "open_trade_count": 0,
                "trades": [{"id": 1, "is_open": True}],
            },
            "differ from open_trade_count",
        ),
    ],
)
def test_reference_projection_rejects_malformed_state_schemas(
    state_update: dict[str, Any],
    message: str,
) -> None:
    state = _reference_fixture_state()
    state.update(state_update)

    with pytest.raises(TraceError, match=message):
        _reference_state(state, "USDT", "spot")


def test_reference_projection_uses_explicit_source_trace(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"stake_currency":"USDT"}\n', encoding="utf-8")
    manifest = {
        "fixture_id": "source-override-test",
        "freqtrade": {"trading_mode": "spot"},
        "inputs": [
            {
                "role": "strategy",
                "path": "strategy.py",
                "sha256": "1" * 64,
                "bytes": 0,
            },
            {
                "role": "config",
                "path": config.name,
                "sha256": "2" * 64,
                "bytes": 0,
            },
        ],
        "artifacts": {"state_trace": {"path": "sealed.trace"}},
    }
    for trace_path, quote_free in [
        (tmp_path / "sealed.trace", "1000"),
        (tmp_path / "fresh.trace", "2000"),
    ]:
        with StateTraceWriter(
            trace_path,
            source="freqtrade-reference",
            run_id="source-override-test",
            input_sha256="3" * 64,
            strategy_sha256="1" * 64,
            profile_sha256="2" * 64,
            trading_mode="spot",
        ) as writer:
            state = _reference_fixture_state()
            state["wallets"]["USDT"][1] = quote_free
            writer.append(
                timestamp_ms=1_700_000_000_000,
                phase="candle.after",
                pair="BTC/USDT",
                state=state,
            )

    projected = tmp_path / "projected.trace"
    project_reference_trace(
        tmp_path / "manifest.json",
        projected,
        manifest=manifest,
        source_trace_path=tmp_path / "fresh.trace",
    )

    events = list(iter_validated_trace_events(projected))
    assert events[0]["state"]["quote_free"] == "2000"


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

    reference = _reference_state(reference_state, "USDT", "spot")
    engine = _engine_state(engine_state, "spot")

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


def test_futures_projection_omits_synthetic_base_position_balances() -> None:
    reference_state = _reference_fixture_state()
    reference_state["wallets"]["APE"] = ["APE", "1422", "0", "1422"]
    engine_state = _engine_fixture_state()
    engine_state["base_balances"] = [{"currency": "APE", "free": "1422"}]

    reference = _reference_state(reference_state, "USDT", "futures")
    engine = _engine_state(engine_state, "futures")

    assert reference["base_balances"] == []
    assert engine["base_balances"] == []


def test_futures_quote_balance_normalizes_sub_nano_float_noise() -> None:
    assert _quote_balance("4592.188874112047", "futures") == "4592.188874112"
    assert _quote_balance("4592.188874112048", "futures") == "4592.188874112"


def test_futures_projection_matches_reference_despite_sub_nano_wallet_noise() -> None:
    reference_state = _reference_fixture_state()
    reference_state["wallets"]["USDT"][1] = "8022.005733333"
    engine_state = _engine_fixture_state()
    engine_state["quote_free"] = "8022.005733333333"

    assert _reference_state(reference_state, "USDT", "futures") == _engine_state(
        engine_state,
        "futures",
    )


def test_spot_quote_balance_preserves_exact_float_representation() -> None:
    assert _quote_balance("4592.188874112047", "spot") == "4592.188874112047"
    assert _quote_balance("4592.188874112048", "spot") == "4592.188874112048"


def test_lock_projection_has_one_order_for_global_and_pair_locks() -> None:
    reference_state = _reference_fixture_state()
    reference_state["locks"] = [
        {
            "pair": "BTC/USDT",
            "lock_timestamp": 1000,
            "lock_end_timestamp": 2000,
            "reason": "StoplossGuard",
            "side": "*",
            "active": True,
        },
        {
            "pair": "*",
            "lock_timestamp": 1000,
            "lock_end_timestamp": 2000,
            "reason": "StoplossGuard",
            "side": "*",
            "active": True,
        },
    ]
    engine_state = _engine_fixture_state()
    engine_state["locks"] = [
        {
            "pair": "*",
            "lock_timestamp_ms": 1000,
            "lock_end_timestamp_ms": 2000,
            "reason": "StoplossGuard",
            "side": "*",
            "active": True,
        },
        {
            "pair": "BTC/USDT",
            "lock_timestamp_ms": 1000,
            "lock_end_timestamp_ms": 2000,
            "reason": "StoplossGuard",
            "side": "*",
            "active": True,
        },
    ]

    reference = _reference_state(reference_state, "USDT", "spot")
    engine = _engine_state(engine_state, "spot")

    assert reference["locks"] == engine["locks"]
    assert [lock["pair"] for lock in reference["locks"]] == ["*", "BTC/USDT"]
