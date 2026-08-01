from __future__ import annotations

import ast
import copy
from functools import cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.x7.adjustment_ir import compile_system_adjustment_ir
from nfi_backtest_engine.x7.adjustments import _build_adjustment_constants
from nfi_backtest_engine.x7.trade_manager import _ADJUSTMENT_METHOD_SHA256

_SOURCE = Path(
    "benchmarks/fixtures/captured/"
    "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
)


@cache
def _inputs() -> tuple[dict[str, ast.FunctionDef], dict[str, object]]:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    strategy = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {
        node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)
    }
    return methods, analysis["strategies"][0]["constants"]


def _compile(
    method: ast.FunctionDef | None = None,
    exit_method: ast.FunctionDef | None = None,
) -> dict[str, object]:
    methods, constants = _inputs()
    method = method or methods["long_grind_adjust_trade_position_v3"]
    exit_method = exit_method or methods["long_grind_exit_v3"]
    descriptor = _build_adjustment_constants(constants, method, side="long")
    return compile_system_adjustment_ir(
        method,
        exit_method,
        constants,
        side="long",
        retry_policy=descriptor["policy"],
    )


def _compile_short(
    method: ast.FunctionDef | None = None,
    exit_method: ast.FunctionDef | None = None,
) -> dict[str, object]:
    methods, constants = _inputs()
    method = method or methods["short_grind_adjust_trade_position_v3"]
    exit_method = exit_method or methods["short_grind_exit_v3"]
    descriptor = _build_adjustment_constants(constants, method, side="short")
    return compile_system_adjustment_ir(
        method,
        exit_method,
        constants,
        side="short",
        retry_policy=descriptor["policy"],
    )


def test_long_adjustment_compiles_source_order_tags_and_dynamic_levels() -> None:
    program = _compile()
    actions = program["source_order"]

    assert program["schema_version"] == "system-adjustment-program-v1"
    assert program["side"] == "long"
    assert program["execution_mode"] == "primary-with-legacy-shadow"
    assert len(actions) == 19
    assert [record["level"] for record in program["order_scan"]["grind_levels"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [
        record["minimum_scale_leverage"]
        for record in program["order_scan"]["grind_levels"]
    ] == [
        "trade-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
        "market-mode-leverage",
    ]
    assert [(record["kind"], record["level"]) for record in actions[:7]] == [
        ("derisk", 1),
        ("derisk", 2),
        ("derisk", 3),
        ("derisk", 4),
        ("grind-entry", 1),
        ("grind-exit", 1),
        ("grind-derisk", 1),
    ]
    assert actions[4]["tag"] == "grind_1_entry"
    assert actions[5]["append_entry_ids"] is True
    assert ["literal", "return-none"] in actions[4]["decision_program"]["expressions"]
    assert ["literal", None] not in actions[4]["decision_program"]["expressions"]
    assert "RSI_3" in program["input_contract"]["indexed_fields"]["last_candle"]


def test_long_adjustment_stake_literal_mutation_recompiles_without_hash_gate() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    branch = next(
        node
        for node in changed.body
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Constant) and item.value == "grind_1_entry"
            for item in ast.walk(node)
        )
    )
    literals = [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Constant) and node.value == 1.5
    ]
    assert len(literals) == 2
    for literal in literals:
        literal.value = 1.65

    original = _compile()
    mutated = _compile(method=changed)
    assert mutated["fingerprint"] != original["fingerprint"]
    assert mutated["source_order"][4]["decision_program"] != (
        original["source_order"][4]["decision_program"]
    )


def test_long_adjustment_exit_condition_mutation_recompiles() -> None:
    methods, _constants = _inputs()
    changed_exit = copy.deepcopy(methods["long_grind_exit_v3"])
    threshold = next(
        node
        for node in ast.walk(changed_exit)
        if isinstance(node, ast.Constant) and node.value == 99.0
    )
    threshold.value = 98.0

    original = _compile()
    mutated = _compile(exit_method=changed_exit)
    assert mutated["fingerprint"] != original["fingerprint"]
    assert mutated["source_order"][5]["decision_program"] != (
        original["source_order"][5]["decision_program"]
    )


def test_long_adjustment_changed_source_order_fails_closed() -> None:
    methods, _constants = _inputs()
    changed = copy.deepcopy(methods["long_grind_adjust_trade_position_v3"])
    indexes = [
        index
        for index, node in enumerate(changed.body)
        if isinstance(node, ast.If)
        and any(
            isinstance(item, ast.Constant)
            and item.value in {"grind_1_entry", "grind_1_derisk"}
            for item in ast.walk(node)
        )
    ]
    assert len(indexes) == 2
    changed.body[indexes[0]], changed.body[indexes[1]] = (
        changed.body[indexes[1]],
        changed.body[indexes[0]],
    )

    with pytest.raises(StrategyAnalysisError, match="Grind source order changed"):
        _compile(method=changed)


def test_long_adjustment_runtime_hash_gates_are_retired() -> None:
    assert "long_grind_adjust_trade_position_v3" not in _ADJUSTMENT_METHOD_SHA256
    assert "long_grind_exit_v3" not in _ADJUSTMENT_METHOD_SHA256
    assert "short_grind_adjust_trade_position_v3" not in _ADJUSTMENT_METHOD_SHA256
    assert "short_grind_exit_v3" not in _ADJUSTMENT_METHOD_SHA256


def test_short_adjustment_is_compiled_from_its_independent_directional_ast() -> None:
    long_program = _compile()
    short_program = _compile_short()

    assert short_program["side"] == "short"
    assert len(short_program["source_order"]) == 18
    assert short_program["order_scan"]["entry_order_side"] == "sell"
    assert short_program["order_scan"]["exit_order_side"] == "buy"
    assert [
        record["level"] for record in short_program["order_scan"]["derisk_tags"]
    ] == [1, 2, 3]
    assert [
        record["level"] for record in short_program["order_scan"]["grind_levels"]
    ] == [1, 2, 3, 4, 5]
    assert short_program["fingerprint"] != long_program["fingerprint"]
    assert "BBL_20_2.0" in short_program["input_contract"]["indexed_fields"]["last_candle"]
    assert "BBU_20_2.0" not in short_program["input_contract"]["indexed_fields"]["last_candle"]


def test_short_exit_mutation_recompiles_without_changing_long_program() -> None:
    methods, _constants = _inputs()
    long_fingerprint = _compile()["fingerprint"]
    changed_exit = copy.deepcopy(methods["short_grind_exit_v3"])
    threshold = next(
        node
        for node in ast.walk(changed_exit)
        if isinstance(node, ast.Constant) and node.value == 1.0
    )
    threshold.value = 2.0

    original_short = _compile_short()
    mutated_short = _compile_short(exit_method=changed_exit)
    assert mutated_short["fingerprint"] != original_short["fingerprint"]
    assert _compile()["fingerprint"] == long_fingerprint
