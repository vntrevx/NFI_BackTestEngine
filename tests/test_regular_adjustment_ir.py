from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.x7.regular_adjustment_ir import compile_regular_adjustment_ir

_SOURCE = Path(
    "benchmarks/fixtures/captured/"
    "x7-derisk-buyback-spot-v17.4.488-2023-01-01_16/inputs/strategy.py"
)


def _method() -> ast.FunctionDef:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "long_adjust_trade_position_no_derisk"
    )


def _constants() -> dict[str, list[float]]:
    return {
        f"regular_mode_grind_{level}_stakes_futures": [0.2]
        for level in range(1, 7)
    }


def test_regular_adjustment_ir_compiles_source_tags_order_and_continuation() -> None:
    program = compile_regular_adjustment_ir(_method(), _constants())

    assert program["schema_version"] == "regular-transition-program-v1"
    assert program["execution_mode"] == "primary-with-legacy-shadow"
    assert [transition["kind"] for transition in program["source_order"]] == [
        "rebuy",
        "grind",
        "grind",
        "grind",
        "grind",
        "grind",
        "grind",
        "derisk",
        "derisk",
    ]
    grinds = [
        transition
        for transition in program["source_order"]
        if transition["kind"] == "grind"
    ]
    assert [(item["entry_tag"], item["stop_tag"]) for item in grinds] == [
        (f"g{level}", f"sg{level}") for level in range(1, 7)
    ]
    assert [item["futures_fallback_loss_threshold"] for item in grinds] == [
        -0.65,
        None,
        None,
        None,
        None,
        None,
    ]
    assert program["continuation"]["amount_ratio"] == 0.95
    assert len(program["fingerprint"]) == 64


def test_regular_adjustment_ir_tracks_source_tag_and_threshold_mutations() -> None:
    original = compile_regular_adjustment_ir(_method(), _constants())
    method = copy.deepcopy(_method())
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and node.value == "g1":
            node.value = "source-lane-one"
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and node.operand.value == 0.65
        ):
            node.operand.value = 0.7

    changed = compile_regular_adjustment_ir(method, _constants())

    first_grind = next(
        transition
        for transition in changed["source_order"]
        if transition["kind"] == "grind"
    )
    assert first_grind["entry_tag"] == "source-lane-one"
    assert first_grind["futures_fallback_loss_threshold"] == -0.7
    assert changed["fingerprint"] != original["fingerprint"]


def test_regular_adjustment_ir_rejects_an_unrecognized_continuation_guard() -> None:
    method = _method()
    comparison = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "current_amount"
    )
    comparison.comparators[0] = ast.Name(id="start_amount", ctx=ast.Load())

    with pytest.raises(StrategyAnalysisError, match="amount guard changed"):
        compile_regular_adjustment_ir(method, _constants())


def test_regular_adjustment_ir_rejects_a_non_reversed_order_scan() -> None:
    method = _method()
    loop = next(
        node
        for node in method.body
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "order"
    )
    loop.iter = ast.Name(id="filled_orders", ctx=ast.Load())

    with pytest.raises(StrategyAnalysisError, match="reverse order scan changed"):
        compile_regular_adjustment_ir(method, _constants())
