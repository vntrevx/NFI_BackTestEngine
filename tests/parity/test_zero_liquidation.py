from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.fixture_engine import run_fixture_engine
from nfi_backtest_engine.state_trace import iter_validated_trace_events

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "benchmarks/fixtures/captured/x7-v17.4.435-zero-liquidation-algo-futures"
)


def test_computed_zero_liquidation_matches_official_state(tmp_path: Path) -> None:
    report = run_fixture_engine(
        FIXTURE / "manifest.json",
        tmp_path / "native",
        timeout_seconds=300,
        verification_level="full",
    )
    assert report["complete"]
    assert report["parity"]["trade_surface"]["equal"]
    assert report["parity"]["state_trace"]["checked"]
    assert report["parity"]["state_trace"]["equal"]

    native_root = tmp_path / "native/research"
    inputs = read_json(native_root / "simulation-input.manifest.json")
    assert inputs["config"]["is_futures"]
    assert inputs["config"]["liquidation_model"]

    # The shared projection omits liquidation_price. Compare the actual
    # entry states as well so None cannot masquerade as an inactive zero.
    official_entries = {}
    for event in iter_validated_trace_events(FIXTURE / "artifacts/state-trace.nfitrace"):
        if event["phase"] != "trade.entry":
            continue
        for trade in event["state"]["trades"]:
            if trade["open_timestamp"] == event["timestamp_ms"]:
                official_entries[(trade["pair"], trade["open_timestamp"])] = tuple(
                    Decimal(str(trade[field]))
                    for field in ("liquidation_price", "stake_amount", "leverage")
                )
    assert official_entries
    assert all(values[0] == 0 for values in official_entries.values())

    native_entries = {}
    with (native_root / "engine-events.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            for trade in event["state"]["open_trades"]:
                if trade["open_timestamp_ms"] == event["timestamp_ms"]:
                    native_entries[(trade["pair"], trade["open_timestamp_ms"])] = tuple(
                        Decimal(str(trade[field]))
                        for field in ("liquidation_price", "stake_amount", "leverage")
                    )
    assert native_entries == official_entries
