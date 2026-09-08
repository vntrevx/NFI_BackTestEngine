from __future__ import annotations

import ast

import pytest
from nfi_backtest_engine.callback_order_state import _lower_x7_order_filled


def _compile(guard: str, *, system: str = "new") -> dict | None:
    node = ast.parse(f'''
def order_filled(self, pair, trade, order, current_time, **kwargs):
    active = self.system_name_use
    old = self.old_system
    new = self.new_system
    if {guard}:
        if active == new:
            trade.set_custom_data(key="system_version", value=new)
        elif active == old:
            trade.set_custom_data(key="system_version", value=old)
    if active == old or active == new:
        order_tag = order.ft_order_tag
        if order_tag is None:
            return None
        order_mode = order_tag.split(" ", 1)[0]
        if order_mode in ["grind_1_exit"]:
            trade.set_custom_data(key="maximum", value=0.0)
    return None
''').body[0]
    assert isinstance(node, ast.FunctionDef)
    return _lower_x7_order_filled(
        node, constants={"system_name_use": system, "old_system": "old", "new_system": "new"},
    )


@pytest.mark.parametrize("system", ["old", "new"])
def test_order_filled_preserves_exit_guard_and_static_system_union(system: str) -> None:
    result = _compile(
        "trade.nr_of_successful_entries == 1 and trade.nr_of_successful_exits == 0",
        system=system,
    )
    assert result is not None
    assert result["operation"]["initial_entry_requires_no_exits"] is True
    assert result["operation"]["initial_successful_entry_writes"] == [
        {"key": "system_version", "value": system}
    ]
    assert result["operation"]["order_tag_actions"]["grind_1_exit"] == [
        {"key": "maximum", "value": 0.0}
    ]
    legacy = _compile("trade.nr_of_successful_entries == 1", system=system)
    assert legacy is not None
    assert "initial_entry_requires_no_exits" not in legacy["operation"]
    assert result["proof"]["program_sha256"] != legacy["proof"]["program_sha256"]


@pytest.mark.parametrize("guard", [
    "trade.nr_of_successful_entries == 1 or trade.nr_of_successful_exits == 0",
    "trade.nr_of_successful_entries == 1 and trade.nr_of_successful_exits > 0",
    "trade.nr_of_successful_entries == 1 and trade.nr_of_successful_exits == 1",
    "trade.nr_of_successful_entries == True",
])
def test_order_filled_changed_initial_condition_fails_closed(guard: str) -> None:
    assert _compile(guard) is None
