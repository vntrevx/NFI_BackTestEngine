from __future__ import annotations

import ast
import copy
from pathlib import Path

import nfi_backtest_engine.nfi_trade_manager as trade_manager
import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.trade_manager import build_nfi_trade_manager_ir


def _method(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _current_method() -> ast.FunctionDef:
    source = Path(
        "benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    strategy = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    return next(
        node
        for node in strategy.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "long_grind_adjust_trade_position"
    )


def test_current_long_grind_liquidation_rescue_compiles_typed_policy() -> None:
    # Given: the current source shape for the one-shot GD5 liquidation rescue.
    method = _method(
        """
def long_grind_adjust_trade_position(
    self,
    trade,
    current_rate,
    slice_profit_entry,
    is_futures,
    is_long_grind_entry,
    is_not_trade_max_stake,
):
    gd5_liquidation_rescue_eligible = (
        is_futures
        and slice_profit_entry < -0.12
        and trade.liquidation_price is not None
        and current_rate < trade.liquidation_price * 1.2
        and trade.get_custom_data(key="gd5_liquidation_rescue_used") is None
    )
    if (
        is_long_grind_entry or gd5_liquidation_rescue_eligible
    ) and is_not_trade_max_stake:
        if gd5_liquidation_rescue_eligible:
            trade.set_custom_data(
                key="gd5_liquidation_rescue_used",
                value=True,
            )
        return 100.0, "gd5"
"""
    )

    # When: the source-bound compiler extracts the rescue policy.
    policy = trade_manager._legacy_liquidation_rescue_policy(method)

    # Then: runtime inputs and one-shot state are typed program data.
    assert policy == {
        "cluster_level": 5,
        "loss_threshold": -0.12,
        "liquidation_multiplier": 1.2,
        "used_state_key": "gd5_liquidation_rescue_used",
    }


def test_eebaf97c_source_exposes_the_same_liquidation_rescue_policy() -> None:
    # Given: the exact source materialized for the current changed-target ledger.
    method = _current_method()

    # When: the production compiler reads the current callback.
    policy = trade_manager._legacy_liquidation_rescue_policy(method)

    # Then: the current callback matches the typed one-shot contract.
    assert policy == {
        "cluster_level": 5,
        "loss_threshold": -0.12,
        "liquidation_multiplier": 1.2,
        "used_state_key": "gd5_liquidation_rescue_used",
    }


def test_current_trade_manager_route_publishes_liquidation_rescue_policy() -> None:
    # Given: the exact current source and its source-bound dependency graph.
    source = Path(
        "benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source"
    )
    analysis = analyze_strategy(source, class_name="NostalgiaForInfinityX7")

    # When: the production trade-manager compiler publishes the route.
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))

    # Then: the GD5 rescue is typed route data rather than a runtime tag branch.
    assert manager is not None
    route = manager["operation"]["supported_routes"]["long_grind"]
    transition = next(
        item
        for item in route["program"]["source_order"]
        if item["kind"] == "cluster" and item["entry_tag"] == "gd5"
    )
    assert transition["liquidation_rescue"] == {
        "cluster_level": 5,
        "loss_threshold": -0.12,
        "liquidation_multiplier": 1.2,
        "used_state_key": "gd5_liquidation_rescue_used",
    }


@pytest.mark.parametrize("mutation", ["extra", "reordered", "comparator"])
def test_liquidation_rescue_predicate_shape_mutations_fail_closed(mutation: str) -> None:
    # Given: one structural mutation of the exact current predicate.
    method = copy.deepcopy(_current_method())
    assignment = next(
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "gd5_liquidation_rescue_eligible"
    )
    assert isinstance(assignment.value, ast.BoolOp)
    if mutation == "extra":
        assignment.value.values.append(ast.Constant(value=True))
    elif mutation == "reordered":
        assignment.value.values[0], assignment.value.values[1] = (
            assignment.value.values[1],
            assignment.value.values[0],
        )
    else:
        comparison = assignment.value.values[1]
        assert isinstance(comparison, ast.Compare)
        comparison.ops = [ast.LtE()]

    # When/Then: source compilation rejects the changed contract.
    with pytest.raises(StrategyAnalysisError, match="liquidation rescue"):
        trade_manager._legacy_liquidation_rescue_policy(method)


def test_liquidation_rescue_misplaced_state_update_fails_closed() -> None:
    # Given: an unrelated statement moved ahead of the source one-shot update.
    method = copy.deepcopy(_current_method())
    branch = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.If)
        and {
            child.id
            for child in ast.walk(node.test)
            if isinstance(child, ast.Name)
        }
        >= {"is_long_grind_entry", "gd5_liquidation_rescue_eligible"}
    )
    branch.body.insert(0, ast.Pass())

    # When/Then: custom-data ordering changes fail before publication.
    with pytest.raises(StrategyAnalysisError, match="one-shot update"):
        trade_manager._legacy_liquidation_rescue_policy(method)
