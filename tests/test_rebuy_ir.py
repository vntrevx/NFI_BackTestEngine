from __future__ import annotations

import ast
import copy
from functools import cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.rebuy_ir import compile_rebuy_transition_ir
from nfi_backtest_engine.x7.trade_manager import build_nfi_trade_manager_ir

_SOURCE = Path("benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source")


@cache
def _source_inputs() -> tuple[str, dict[str, object]]:
    text = _SOURCE.read_text(encoding="utf-8")
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    return text, analysis["strategies"][0]["constants"]


def _method(text: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(text)
    strategy = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    return next(
        node for node in strategy.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_rebuy_program_compiles_directional_order_scans_and_features() -> None:
    text, constants = _source_inputs()
    long = compile_rebuy_transition_ir(
        _method(text, "long_rebuy_adjust_trade_position_v3"),
        constants,
        delegate_retry_ms=300_000,
    )
    short = compile_rebuy_transition_ir(
        _method(text, "short_rebuy_adjust_trade_position_v3"),
        constants,
        delegate_retry_ms=300_000,
    )

    assert long["order_scan"]["cluster_order_side"] == "buy"
    assert long["order_scan"]["boundary_order_side"] == "sell"
    assert short["order_scan"]["cluster_order_side"] == "sell"
    assert short["order_scan"]["boundary_order_side"] == "buy"
    assert long["delegate"] == {
        "selector": "first-exit",
        "tag_operator": "equal",
        "tag": "derisk_level_3",
        "target": "position-adjustment",
        "source_target": "long_grind_adjust_trade_position_v3",
        "target_entry_retry_ms": 300_000,
        "location": long["delegate"]["location"],
    }
    assert long["input_contract"]["indexed_fields"]["last_candle"] == [
        "AROONU_14",
        "AROONU_14_15m",
        "EMA_26",
        "RSI_3",
        "RSI_3_15m",
        "close",
        "protections_long_global",
    ]
    assert short["input_contract"]["indexed_fields"]["last_candle"][0] == "AROOND_14"


def test_rebuy_stake_literal_mutation_recompiles_without_a_method_hash_gate() -> None:
    text, constants = _source_inputs()
    original_method = _method(text, "long_rebuy_adjust_trade_position_v3")
    rendered = ast.unparse(original_method)
    marker = "if buy_amount < min_stake * 1.5:"
    assert rendered.count(marker) == 1
    changed = ast.parse(rendered.replace(marker, "if buy_amount < min_stake * 1.65:"))
    changed_method = changed.body[0]
    assert isinstance(changed_method, ast.FunctionDef)
    original = compile_rebuy_transition_ir(
        original_method,
        constants,
        delegate_retry_ms=300_000,
    )
    mutated = compile_rebuy_transition_ir(
        changed_method,
        constants,
        delegate_retry_ms=300_000,
    )

    assert mutated["fingerprint"] != original["fingerprint"]
    assert mutated["decision_program"] != original["decision_program"]


def test_rebuy_compiles_conditional_ladders_and_local_candle_loading() -> None:
    text, constants = _source_inputs()
    original_method = _method(text, "long_rebuy_adjust_trade_position_v3")
    changed = copy.deepcopy(original_method)
    assignments = {
        statement.targets[0].id: statement
        for statement in changed.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id in {"rebuy_mode_stakes", "rebuy_mode_sub_thresholds"}
    }
    stakes = assignments["rebuy_mode_stakes"]
    thresholds = assignments["rebuy_mode_sub_thresholds"]
    assert isinstance(stakes.value, ast.IfExp)
    assert isinstance(thresholds.value, ast.IfExp)
    assert ast.dump(stakes.value.test) == ast.dump(thresholds.value.test)
    insertion = min(changed.body.index(stakes), changed.body.index(thresholds))
    changed.body = [
        statement for statement in changed.body if statement not in {stakes, thresholds}
    ]
    changed.body.insert(
        insertion,
        ast.If(
            test=copy.deepcopy(stakes.value.test),
            body=[
                ast.Assign(
                    targets=[copy.deepcopy(stakes.targets[0])],
                    value=copy.deepcopy(stakes.value.body),
                ),
                ast.Assign(
                    targets=[copy.deepcopy(thresholds.targets[0])],
                    value=copy.deepcopy(thresholds.value.body),
                ),
            ],
            orelse=[
                ast.Assign(
                    targets=[copy.deepcopy(stakes.targets[0])],
                    value=copy.deepcopy(stakes.value.orelse),
                ),
                ast.Assign(
                    targets=[copy.deepcopy(thresholds.targets[0])],
                    value=copy.deepcopy(thresholds.value.orelse),
                ),
            ],
        ),
    )
    entry = next(
        statement
        for statement in changed.body
        if isinstance(statement, ast.If)
        and any(
            isinstance(node, ast.Name) and node.id == "sub_grind_count"
            for node in ast.walk(statement.test)
        )
        and any(
            isinstance(node, ast.Name) and node.id == "buy_amount" for node in ast.walk(statement)
        )
    )
    entry.body[:0] = ast.parse(
        "df, _ = dp.get_analyzed_dataframe(trade_pair, self.timeframe)\n"
        "if len(df) < 1:\n"
        "    return None\n"
        "last_candle = df.iloc[-1]\n"
    ).body
    ast.fix_missing_locations(changed)

    original = compile_rebuy_transition_ir(
        original_method,
        constants,
        delegate_retry_ms=300_000,
    )
    compiled = compile_rebuy_transition_ir(
        changed,
        constants,
        delegate_retry_ms=300_000,
    )

    assert compiled["decision_program"] == original["decision_program"]
    assert compiled["input_contract"] == original["input_contract"]


def test_rebuy_unknown_order_walk_fails_closed() -> None:
    text, constants = _source_inputs()
    method = _method(text, "long_rebuy_adjust_trade_position_v3")
    loop = next(
        node
        for node in method.body
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "order"
    )
    loop.iter = ast.Name(id="filled_orders", ctx=ast.Load())

    with pytest.raises(StrategyAnalysisError, match="filled-order scan changed"):
        compile_rebuy_transition_ir(method, constants, delegate_retry_ms=300_000)


def test_trade_manager_binds_rebuy_delegates_to_compiled_adjustment_callbacks() -> None:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))
    assert manager is not None
    operation = manager["operation"]

    assert (
        operation["rebuy_adjustment"]["program"]["delegate"]["source_target"]
        == (operation["position_adjustment"]["source_callback"])
    )
    assert (
        operation["short_rebuy_adjustment"]["program"]["delegate"]["source_target"]
        == (operation["short_position_adjustment"]["source_callback"])
    )
    stateful_methods = manager["proof"]["stateful_methods"]
    assert "long_rebuy_adjust_trade_position_v3" not in stateful_methods
    assert "short_rebuy_adjust_trade_position_v3" not in stateful_methods


def test_trade_manager_binds_independent_long_and_short_adjustment_programs() -> None:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))
    assert manager is not None
    operation = manager["operation"]
    long_adjustment = operation["position_adjustment"]
    short_adjustment = operation["short_position_adjustment"]

    assert operation["schema_version"] == "0.31.0"
    assert long_adjustment["program"]["side"] == "long"
    assert short_adjustment["program"]["side"] == "short"
    assert long_adjustment["program"]["order_scan"]["entry_order_side"] == "buy"
    assert short_adjustment["program"]["order_scan"]["entry_order_side"] == "sell"
    assert [row["level"] for row in long_adjustment["constants"]["derisk_levels"]] == [
        1,
        2,
        3,
        4,
    ]
    assert [row["level"] for row in short_adjustment["constants"]["derisk_levels"]] == [
        1,
        2,
        3,
    ]
    assert (
        manager["proof"]["system_adjustment_ir_fingerprint"]
        != manager["proof"]["short_system_adjustment_ir_fingerprint"]
    )
