from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.specs import (
    STATE_MACHINE_PROGRAM_SCHEMA,
    validate_schema,
)
from nfi_backtest_engine.state_machine_ir import (
    StateMachineCompileError,
    compile_state_machine_program,
)


def test_state_machine_ir_compiles_typed_state_dynamic_levels_and_actions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        """
class DynamicGrind(IStrategy):
    max_entry_position_adjustment = 12

    def order_filled(self, pair, trade, order, current_time, **kwargs):
        trade.set_custom_data("system_version", "3.2")
        for level in range(7, 13):
            trade.set_custom_data("last_grind_level", level)

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        level = trade.get_custom_data("grind_level_12", 0)
        if current_profit < -0.1 and level < self.max_entry_position_adjustment:
            trade.set_custom_data("grind_level_12", level + 1)
            return (max_stake, "grind_12_entry")
        if current_profit > 0.1:
            return (-min_stake, "grind_7_exit")
        return None

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        if current_profit < -0.5:
            return "stop_dynamic"
        return None
""".lstrip(),
        encoding="utf-8",
    )

    program = compile_state_machine_program(source, class_name="DynamicGrind")
    validate_schema(program, STATE_MACHINE_PROGRAM_SCHEMA)

    assert list(program["entrypoints"]) == [
        "order_filled",
        "adjust_trade_position",
        "custom_exit",
    ]
    assert {"bounded_for", "set_state", "if", "action"} <= set(
        program["opcodes"]
    )
    assert program["required_state_keys"] == [
        "grind_level_12",
        "last_grind_level",
        "system_version",
    ]
    actions = _instructions(
        program["entrypoints"]["adjust_trade_position"]["instructions"],
        "action",
    )
    assert [action["kind"] for action in actions] == [
        "add_entry",
        "partial_exit",
        "no_op",
    ]
    assert actions[0]["tag"]["value"] == "grind_12_entry"
    assert actions[1]["tag"]["value"] == "grind_7_exit"
    condition = _instructions(
        program["entrypoints"]["adjust_trade_position"]["instructions"],
        "if",
    )[0]["condition"]
    assert condition["values"][1]["right"] == {
        "kind": "literal",
        "value": 12,
    }


def test_state_machine_ir_rejects_unbounded_while_with_source_location(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        """
class UnsafeState(IStrategy):
    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        while current_profit < 0:
            current_profit += 1
        return None
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(
        StateMachineCompileError,
        match=r"strategy\.py:4:8: STATE_MACHINE_UNSUPPORTED: While",
    ):
        compile_state_machine_program(source, class_name="UnsafeState")


def _instructions(values: list[dict], opcode: str) -> list[dict]:
    result = []
    for value in values:
        if value["opcode"] == opcode:
            result.append(value)
        for key in ("then_instructions", "else_instructions", "instructions"):
            nested = value.get(key)
            if isinstance(nested, list):
                result.extend(_instructions(nested, opcode))
    return result
