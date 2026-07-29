from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.strategy_diff import diff_strategies


def _strategy(path: Path, *, signal: int, stateful: str = "") -> None:
    path.write_text(
        f"""
class Demo(IStrategy):
    timeframe = "5m"

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] > 0, "enter_tag"] += "{signal} "
        return dataframe

{stateful}
""".lstrip(),
        encoding="utf-8",
    )


def test_strategy_diff_classifies_vector_signal_change(tmp_path: Path) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    _strategy(old, signal=62)
    _strategy(new, signal=63)

    report = diff_strategies(old, new, class_name="Demo")

    assert report == diff_strategies(old, new, class_name="Demo")
    assert report["classification"] == "vector-only"
    assert report["changes"]["signals"] == {
        "added": ["63"],
        "removed": ["62"],
    }
    assert report["changes"]["callbacks"]["changed"] == [
        "populate_entry_trend"
    ]
    assert report["changes"]["dataframe_columns"]["added"] == []


def test_strategy_diff_flags_dynamic_grind_state_for_review(tmp_path: Path) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    _strategy(old, signal=63)
    _strategy(
        new,
        signal=63,
        stateful="""
    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit, **kwargs):
        level = trade.get_custom_data("grind_level_12", 0)
        trade.set_custom_data("grind_level_12", level + 1)
        return (10, "grind_level_12_entry")
""",
    )

    report = diff_strategies(old, new, class_name="Demo")

    assert report["classification"] == "stateful-review"
    assert report["changes"]["custom_state_keys"]["added"] == ["grind_level_12"]
    assert report["changes"]["grind_levels"]["added"] == [12]
    assert "adjust_trade_position" in report["changes"]["callbacks"]["added"]
    assert "call:set_custom_data" in report["changes"]["opcodes"]["added"]
