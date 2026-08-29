from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.probe_vector import overlay_execution_signals

_SIGNALS = {
    "enter_tag": ["base", None],
    "enter_long": [1, 0],
    "enter_short": [0, 0],
    "exit_tag": [None, "base-exit"],
    "exit_long": [0, 1],
    "exit_short": [0, 0],
    "nfi_exec_enter_long": [1, 0],
    "nfi_exec_enter_short": [0, 0],
    "nfi_exec_exit_long": [0, 1],
    "nfi_exec_exit_short": [0, 0],
    "nfi_exec_enter_tag": ["base", None],
    "nfi_exec_exit_tag": [None, "base-exit"],
}


def _frame(*, close_delta: float = 0.0, scenario: bool = False) -> pl.DataFrame:
    values = {
        "date": [1_648_531_200_000, 1_648_531_500_000],
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0 + close_delta],
        "volume": [10.0, 11.0],
        "nfi_exec_funding_rate": [0.0, 0.001],
        "nfi_exec_funding_mark_price": [101.0, 102.0],
        "RSI_14": [40.0, 41.0],
        **_SIGNALS,
    }
    frame = pl.DataFrame(values)
    if not scenario:
        return frame
    return frame.with_columns(
        pl.Series("enter_tag", ["120 ", None]),
        pl.Series("enter_long", [1, 0]),
        pl.Series("exit_tag", [None, "exit_signal"]),
        pl.Series("exit_long", [0, 1]),
        pl.Series("nfi_exec_enter_tag", ["120 ", None]),
        pl.Series("nfi_exec_enter_long", [1, 0]),
        pl.Series("nfi_exec_exit_tag", [None, "exit_signal"]),
        pl.Series("nfi_exec_exit_long", [0, 1]),
    )


def test_overlay_execution_signals_preserves_current_features(tmp_path: Path) -> None:
    # Given: current-source features and an exact-market branch-probe scenario.
    base = tmp_path / "base.feather"
    scenario = tmp_path / "scenario.feather"
    output = tmp_path / "overlay.feather"
    _frame().write_ipc(base)
    _frame(scenario=True).drop("RSI_14").write_ipc(scenario)

    # When: only execution signal/tag columns are overlaid.
    overlay_execution_signals(base, scenario, output)

    # Then: current-source features remain while scenario execution is exact.
    result = pl.read_ipc(output)
    assert result.get_column("RSI_14").to_list() == [40.0, 41.0]
    assert result.get_column("nfi_exec_enter_tag").to_list() == ["120 ", None]
    assert result.get_column("nfi_exec_exit_tag").to_list() == [None, "exit_signal"]


def test_overlay_execution_signals_rejects_market_state_mismatch(tmp_path: Path) -> None:
    # Given: a scenario whose market state differs by one exact close.
    base = tmp_path / "base.feather"
    scenario = tmp_path / "scenario.feather"
    _frame().write_ipc(base)
    _frame(close_delta=0.1, scenario=True).drop("RSI_14").write_ipc(scenario)

    # When/Then: the provenance boundary fails before output publication.
    with pytest.raises(StrategyAnalysisError, match="market-state columns differ"):
        overlay_execution_signals(base, scenario, tmp_path / "overlay.feather")


def test_overlay_execution_signals_returns_exact_startup_offset(tmp_path: Path) -> None:
    # Given: one current-source startup row before the exact scenario market window.
    base = tmp_path / "base.feather"
    scenario = tmp_path / "scenario.feather"
    output = tmp_path / "overlay.feather"
    startup = _frame().head(1).with_columns(pl.col("date") - 300_000)
    current = pl.concat([startup, _frame()])
    current.write_ipc(base)
    _frame(scenario=True).drop("RSI_14").write_ipc(scenario)

    # When: the scenario is aligned by its exact date/market surface.
    offset = overlay_execution_signals(base, scenario, output)

    # Then: callers can shift execution past startup while retaining current features.
    assert offset == 1
    result = pl.read_ipc(output)
    assert result.height == 3
    assert result.get_column("nfi_exec_enter_tag").to_list() == [
        "base",
        "120 ",
        None,
    ]
