from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.specs import (
    STATE_MACHINE_PROGRAM_SCHEMA,
    STATE_MACHINE_PROGRAM_V2_SCHEMA,
    STATE_MACHINE_PROGRAM_V3_SCHEMA,
    validate_schema,
)
from nfi_backtest_engine.state_machine_ir import (
    STATE_MACHINE_PROGRAM_V3_VERSION,
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
    validate_schema(program, STATE_MACHINE_PROGRAM_V2_SCHEMA)

    assert program["schema_version"] == "state-machine-program-v2"
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


def test_state_machine_v2_inlines_transitive_pure_helpers_and_class_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        """
class HelperGrind(IStrategy):
    thresholds = {"entry": -0.12}
    routes = {"entry": "grind_dynamic_entry"}

    def base_threshold(self, scale=1.0):
        return self.thresholds["entry"] * scale

    def entry_threshold(self, leverage):
        return self.base_threshold() / leverage

    def entry_route(self):
        return self.routes["entry"]

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        threshold = self.entry_threshold(trade.leverage)
        if current_profit < threshold:
            return (max_stake, self.entry_route())
        return None
""".lstrip(),
        encoding="utf-8",
    )

    program = compile_state_machine_program(source, class_name="HelperGrind")
    validate_schema(program, STATE_MACHINE_PROGRAM_V2_SCHEMA)

    instructions = program["entrypoints"]["adjust_trade_position"]["instructions"]
    local = _instructions(instructions, "set_local")[0]["value"]
    assert local["operator"] == "divide"
    assert local["left"]["left"] == {"kind": "literal", "value": -0.12}
    actions = _instructions(instructions, "action")
    assert actions[0]["tag"] == {
        "kind": "literal",
        "value": "grind_dynamic_entry",
    }
    assert program["required_reads"] == [
        {"source": "candle", "key": "current_profit"},
        {"source": "trade", "key": "leverage"},
        {"source": "wallet", "key": "max_stake"},
    ]


def test_state_machine_v2_tracks_x7_helper_mutations_without_hash_allowlist(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before.py"
    after = tmp_path / "after.py"
    template = """
class NostalgiaForInfinityX7(IStrategy):
    thresholds = {{"entry": {threshold}}}

    def entry_threshold(self):
        return self.thresholds["entry"]

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        if current_profit < self.entry_threshold():
            return (max_stake, "route_from_source")
        return None
""".lstrip()
    before.write_text(template.format(threshold="-0.10"), encoding="utf-8")
    after.write_text(template.format(threshold="-0.15"), encoding="utf-8")

    old_program = compile_state_machine_program(
        before,
        class_name="NostalgiaForInfinityX7",
    )
    new_program = compile_state_machine_program(
        after,
        class_name="NostalgiaForInfinityX7",
    )

    old_condition = _instructions(
        old_program["entrypoints"]["adjust_trade_position"]["instructions"],
        "if",
    )[0]["condition"]
    new_condition = _instructions(
        new_program["entrypoints"]["adjust_trade_position"]["instructions"],
        "if",
    )[0]["condition"]
    assert old_condition["right"]["value"] == -0.10
    assert new_condition["right"]["value"] == -0.15
    assert old_program != new_program


def test_state_machine_v1_schema_remains_readable(tmp_path: Path) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        """
class LegacyProgram(IStrategy):
    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        if current_profit < -0.5:
            return "legacy_stop"
        return None
""".lstrip(),
        encoding="utf-8",
    )
    program = compile_state_machine_program(source, class_name="LegacyProgram")
    legacy = {**program, "schema_version": "state-machine-program-v1"}

    validate_schema(legacy, STATE_MACHINE_PROGRAM_SCHEMA)


def test_state_machine_v3_compiles_finite_order_filter_and_accumulation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "FiniteOrders.py"
    source.write_text(
        '''class FiniteOrders(IStrategy):
    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        filled_entries = trade.select_filled_orders(trade.entry_side)
        tagged_cost = 0.0
        tagged_count = 0
        for entry_order in filled_entries:
            if entry_order.ft_order_tag == "grind_entry":
                tagged_cost += entry_order.safe_filled * entry_order.safe_price
                tagged_count += 1
        trade.set_custom_data("tagged_cost", tagged_cost)
        if tagged_count > 1:
            return "ordered_exit"
        return None
''',
        encoding="utf-8",
    )

    program = compile_state_machine_program(
        source,
        class_name="FiniteOrders",
        schema_version=STATE_MACHINE_PROGRAM_V3_VERSION,
        max_order_iterations=32,
    )
    validate_schema(program, STATE_MACHINE_PROGRAM_V3_SCHEMA)

    assert program["schema_version"] == "state-machine-program-v3"
    assert program["limits"] == {"max_order_iterations": 32}
    assert program["custom_state_transaction"] == "entrypoint_atomic"
    assert program["required_order_fields"] == [
        {"field": "ft_order_tag", "value_type": "string_or_null"},
        {"field": "safe_filled", "value_type": "number"},
        {"field": "safe_price", "value_type": "number"},
    ]
    assert {"for_each_order", "if", "set_local", "set_state", "action"} <= set(
        program["opcodes"]
    )
    loop = _instructions(
        program["entrypoints"]["custom_exit"]["instructions"],
        "for_each_order",
    )[0]
    assert loop["variable"] == "entry_order"
    assert loop["collection"] == {
        "kind": "read",
        "source": "local",
        "key": "filled_entries",
        "default": None,
    }
    assert loop["max_iterations"] == 32
    assert _instructions(loop["instructions"], "if")[0]["condition"]["left"] == {
        "kind": "order_field",
        "order": {
            "kind": "read",
            "source": "local",
            "key": "entry_order",
            "default": None,
        },
        "field": "ft_order_tag",
        "value_type": "string_or_null",
    }
    assert program["required_reads"] == [
        {"source": "orders", "key": "filled_entries"}
    ]


def test_state_machine_v3_rejects_order_iteration_without_a_finite_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "UnboundedOrders.py"
    source.write_text(
        '''class UnboundedOrders(IStrategy):
    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        orders = trade.select_filled_orders(None)
        for order in orders:
            if order.safe_filled > 0:
                return "exit"
        return None
''',
        encoding="utf-8",
    )

    with pytest.raises(
        StateMachineCompileError,
        match="order loop requires max_order_iterations",
    ):
        compile_state_machine_program(
            source,
            class_name="UnboundedOrders",
            schema_version=STATE_MACHINE_PROGRAM_V3_VERSION,
        )


def test_state_machine_v3_rejects_unknown_order_fields(tmp_path: Path) -> None:
    source = tmp_path / "UnknownOrderField.py"
    source.write_text(
        '''class UnknownOrderField(IStrategy):
    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        orders = trade.select_filled_orders(None)
        for order in orders:
            if order.future_exchange_field:
                return "exit"
        return None
''',
        encoding="utf-8",
    )

    with pytest.raises(
        StateMachineCompileError,
        match="order field future_exchange_field",
    ):
        compile_state_machine_program(
            source,
            class_name="UnknownOrderField",
            schema_version=STATE_MACHINE_PROGRAM_V3_VERSION,
            max_order_iterations=8,
        )


def test_state_machine_default_remains_byte_equivalent_to_explicit_v2(
    tmp_path: Path,
) -> None:
    source = tmp_path / "V2Replay.py"
    source.write_text(
        '''class V2Replay(IStrategy):
    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        if current_profit > 0.1:
            return "exit"
        return None
''',
        encoding="utf-8",
    )

    default = compile_state_machine_program(source, class_name="V2Replay")
    explicit = compile_state_machine_program(
        source,
        class_name="V2Replay",
        schema_version="state-machine-program-v2",
    )

    assert default == explicit
    validate_schema(default, STATE_MACHINE_PROGRAM_V2_SCHEMA)


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


def test_state_machine_v2_rejects_stateful_helper_with_source_location(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strategy.py"
    source.write_text(
        """
class UnsafeHelper(IStrategy):
    def threshold(self, trade):
        trade.set_custom_data("visited", True)
        return -0.1

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake, **kwargs):
        threshold = self.threshold(trade)
        if current_profit < threshold:
            return (max_stake, "entry")
        return None
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(
        StateMachineCompileError,
        match=r"strategy\.py:8:20: STATE_MACHINE_UNSUPPORTED: stateful helper threshold",
    ):
        compile_state_machine_program(source, class_name="UnsafeHelper")


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
