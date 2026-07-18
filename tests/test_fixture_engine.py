from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.fixture_engine import build_fixture_simulation_input

ROOT = Path(__file__).parents[1]
STOPS = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
    / "manifest.json"
)
NORMAL = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "manifest.json"
)


def test_stops_fixture_compiles_shifted_signal_arrays(tmp_path: Path) -> None:
    document = build_fixture_simulation_input(STOPS, tmp_path / "input.json")
    candles = document["pairs"][0]["candles"]

    assert len(candles) == 862
    assert sum(candle["enter_long"] is not None for candle in candles) > 0
    assert document["config"]["stoploss_ratio"] == -0.005
    assert document["config"]["adjustment_rule"] is None


def test_normal_fixture_compiles_stateful_callback_rules(tmp_path: Path) -> None:
    document = build_fixture_simulation_input(NORMAL, tmp_path / "input.json")

    assert document["config"]["custom_exit_after_ms"] == 21_600_000
    assert document["config"]["adjustment_rule"]["profit_below"] == -0.004
    assert document["config"]["adjustment_rule"]["max_adjustments"] == 1
