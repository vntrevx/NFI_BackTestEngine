from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import TraceError
from nfi_backtest_engine.state_trace import (
    StateTraceWriter,
    TraceMismatch,
    compare_state_traces,
    read_state_trace,
)

HASH = "0" * 64


def _write_trace(path: Path, *, balance: str = "1000", include_state: bool = True) -> None:
    with StateTraceWriter(
        path,
        source="engine",
        run_id="run-1",
        input_sha256=HASH,
        strategy_sha256=HASH,
        profile_sha256=HASH,
        trading_mode="futures",
        include_state=include_state,
    ) as trace:
        trace.append(
            timestamp_ms=1_700_000_000_000,
            phase="candle.close",
            pair="BTC/USDT:USDT",
            state={
                "wallet": {"available": balance, "reserved": "0", "total": balance},
                "open_slots": 6,
                "trades": [],
            },
        )


def test_materialized_trace_round_trips_and_compares(tmp_path: Path) -> None:
    expected = tmp_path / "expected.nfitrace"
    actual = tmp_path / "actual.nfitrace"
    _write_trace(expected)
    _write_trace(actual)

    trace = read_state_trace(expected)
    compare_state_traces(expected, actual)

    assert trace.trailer["event_count"] == 1
    assert trace.events[0]["state"]["wallet"]["available"] == "1000"


def test_first_state_difference_reports_field_and_event(tmp_path: Path) -> None:
    expected = tmp_path / "expected.nfitrace"
    actual = tmp_path / "actual.nfitrace"
    _write_trace(expected)
    _write_trace(actual, balance="999")

    with pytest.raises(TraceMismatch) as error:
        compare_state_traces(expected, actual)

    difference = error.value.difference
    assert difference.sequence == 0
    assert difference.path == "$.state.wallet.available"
    assert difference.expected == "1000"
    assert difference.actual == "999"


def test_hash_only_trace_is_valid_and_comparable(tmp_path: Path) -> None:
    expected = tmp_path / "expected.nfitrace"
    actual = tmp_path / "actual.nfitrace"
    _write_trace(expected, include_state=False)
    _write_trace(actual, include_state=False)

    compare_state_traces(expected, actual)


def test_binary_float_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.nfitrace"
    with (
        StateTraceWriter(
            path,
            source="engine",
            run_id="run-1",
            input_sha256=HASH,
            strategy_sha256=HASH,
            profile_sha256=HASH,
            trading_mode="spot",
        ) as trace,
        pytest.raises(TraceError, match="binary floats are forbidden"),
    ):
        trace.append(
            timestamp_ms=1,
            phase="candle.close",
            state={"wallet": {"available": 1.0}},
        )


def test_trace_comparator_does_not_materialize_complete_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / "expected.nfitrace"
    actual = tmp_path / "actual.nfitrace"
    _write_trace(expected)
    _write_trace(actual)
    monkeypatch.setattr(
        "nfi_backtest_engine.state_trace.read_state_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not materialize")),
    )

    compare_state_traces(expected, actual)
