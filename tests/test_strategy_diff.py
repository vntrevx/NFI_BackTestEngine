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
    assert report["changes"]["callbacks"]["changed"] == ["populate_entry_trend"]
    assert report["changes"]["dataframe_columns"]["added"] == []
    targets = {
        (target["kind"], target["change"], target["value"]): target
        for target in report["behavior_targets"]
    }
    assert targets[("signal", "removed", "62")]["runtime_observable"] is True
    assert targets[("signal", "added", "63")]["runtime_observable"] is True
    assert targets[("signal", "removed", "62")]["proof"]["mode"] == "absence"
    assert targets[("signal", "added", "63")]["proof"]["mode"] == "presence"
    assert targets[("callback", "changed", "populate_entry_trend")]["tags"] == ["63"]
    callback_proof = targets[("callback", "changed", "populate_entry_trend")]["proof"]
    assert callback_proof["mode"] == "transition"
    assert callback_proof["old_source_spans"][0]["method"] == "populate_entry_trend"
    assert callback_proof["new_source_spans"][0]["method"] == "populate_entry_trend"


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
    targets = report["behavior_targets"]
    state_target = next(
        target
        for target in targets
        if target["kind"] == "custom_state_key" and target["value"] == "grind_level_12"
    )
    assert state_target["methods"] == ["adjust_trade_position"]
    assert state_target["tags"] == ["grind_level_12_entry"]
    assert state_target["runtime_observable"] is True


def test_strategy_diff_maps_changed_route_guard_to_runtime_tag(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text(
        """
class Demo(IStrategy):
    def populate_entry_trend(self, dataframe, metadata):
        for entry_condition_index in range(100):
            if entry_condition_index == 63:
                condition = dataframe["close"] > 10
        return dataframe
""".lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        old.read_text(encoding="utf-8").replace(
            'dataframe["close"] > 10',
            'dataframe["close"] > 11',
        ),
        encoding="utf-8",
    )

    report = diff_strategies(old, new, class_name="Demo")

    target = next(target for target in report["behavior_targets"] if target["kind"] == "callback")
    assert target["value"] == "populate_entry_trend"
    assert target["tags"] == ["63"]
    assert target["runtime_observable"] is True


def test_strategy_diff_follows_helpers_but_ignores_metadata_methods(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text(
        """
class Demo(IStrategy):
    def version(self):
        return "v1"

    def entry_route(self, dataframe):
        if self.signal_id == 63:
            return "signal-63-entry"
        return ""

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_tag"] = self.entry_route(dataframe)
        return dataframe
""".lstrip(),
        encoding="utf-8",
    )
    new.write_text(
        old.read_text(encoding="utf-8")
        .replace('return "v1"', 'return "v2"')
        .replace('return "signal-63-entry"', 'return "signal-63-entry-v2"'),
        encoding="utf-8",
    )

    report = diff_strategies(old, new, class_name="Demo")

    targets = report["behavior_targets"]
    assert all(target["value"] != "version" for target in targets)
    helper = next(
        target
        for target in targets
        if target["kind"] == "callback" and target["value"] == "entry_route"
    )
    assert "63" in helper["tags"]
    assert helper["runtime_observable"] is True
