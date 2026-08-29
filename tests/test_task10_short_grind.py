from __future__ import annotations

import ast
import copy
from pathlib import Path

import nfi_backtest_engine.nfi_trade_manager as trade_manager
import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.legacy import _build_short_grind_route
from nfi_backtest_engine.x7.trade_manager import build_nfi_trade_manager_ir

_SOURCE = Path(
    "benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source"
)
_HISTORICAL_SOURCE = Path(
    "benchmarks/fixtures/release-candidate/"
    "x7-tag-121-futures-v17.4.473-2023-01-01_02/inputs/strategy.py"
)


def _current_short_method() -> ast.FunctionDef:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    strategy = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    return next(
        node
        for node in strategy.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "short_grind_adjust_trade_position"
    )


def test_eebaf97c_short_rescue_compiles_side_specific_policy() -> None:
    # Given: the exact current short callback.
    method = _current_short_method()

    # When: the generic rescue compiler reads its ordered predicate.
    policy = trade_manager._legacy_liquidation_rescue_policy(method)

    # Then: short sign and liquidation direction remain explicit typed data.
    assert policy == {
        "side": "short",
        "cluster_level": 5,
        "loss_threshold": 0.12,
        "profit_comparison": "greater-than",
        "liquidation_multiplier": 0.8,
        "liquidation_comparison": "greater-than",
        "used_state_key": "gd5_liquidation_rescue_used",
    }


def test_current_manager_publishes_a_source_bound_short_legacy_route() -> None:
    # Given: current source and its dependency graph.
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")

    # When: the manager is compiled.
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))

    # Then: tag 620 routes to the independent short legacy program.
    assert manager is not None
    route = manager["operation"]["short_grind"]
    assert route["entry_tags"] == ["620"]
    assert route["program"]["source_callback"] == "short_grind_adjust_trade_position"
    rescue = next(
        transition["liquidation_rescue"]
        for transition in route["program"]["source_order"]
        if transition.get("liquidation_rescue") is not None
    )
    assert rescue["side"] == "short"


def test_v174473_short_route_without_liquidation_rescue_compiles() -> None:
    analysis = analyze_strategy(
        _HISTORICAL_SOURCE,
        class_name="NostalgiaForInfinityX7",
    )

    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))

    assert manager is not None
    source_order = manager["operation"]["short_grind"]["program"]["source_order"]
    assert all(transition.get("liquidation_rescue") is None for transition in source_order)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("term-order", "predicate"),
        ("profit-operator", "profit predicate"),
        ("profit-sign", "profit predicate"),
        ("liquidation-operator", "proximity predicate"),
        ("multiplier", "proximity predicate"),
        ("key", "one-shot update"),
        ("update-order", "one-shot update"),
    ],
)
def test_short_rescue_source_mutations_fail_closed(mutation: str, message: str) -> None:
    # Given: one semantic mutation of the current short rescue contract.
    method = copy.deepcopy(_current_short_method())
    assignment = next(
        statement
        for statement in method.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "gd5_liquidation_rescue_eligible"
    )
    assert isinstance(assignment.value, ast.BoolOp)
    terms = assignment.value.values
    if mutation == "term-order":
        terms[0], terms[1] = terms[1], terms[0]
    elif mutation == "profit-operator":
        assert isinstance(terms[1], ast.Compare)
        terms[1].ops = [ast.GtE()]
    elif mutation == "profit-sign":
        assert isinstance(terms[1], ast.Compare)
        terms[1].comparators = [ast.UnaryOp(op=ast.USub(), operand=ast.Constant(0.12))]
    elif mutation == "liquidation-operator":
        assert isinstance(terms[3], ast.Compare)
        terms[3].ops = [ast.Lt()]
    elif mutation == "multiplier":
        assert isinstance(terms[3], ast.Compare)
        product = terms[3].comparators[0]
        assert isinstance(product, ast.BinOp)
        product.right = ast.Constant(1.2)
    elif mutation == "key":
        assert isinstance(terms[4], ast.Compare)
        call = terms[4].left
        assert isinstance(call, ast.Call)
        call.keywords[0].value = ast.Constant("other_key")
    else:
        branch = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.If)
            and any(
                isinstance(child, ast.Name)
                and child.id == "gd5_liquidation_rescue_eligible"
                for child in ast.walk(node.test)
            )
            and node.body
            and isinstance(node.body[0], ast.If)
        )
        branch.body.insert(0, ast.Pass())

    # When/Then: changed source cannot inherit the sealed behavior.
    with pytest.raises(StrategyAnalysisError, match=message):
        trade_manager._legacy_liquidation_rescue_policy(method)


def _short_route_for_method(method: ast.FunctionDef) -> dict[str, object]:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    strategy = analysis["strategies"][0]
    assert isinstance(strategy, dict)
    constants = strategy["constants"]
    assert isinstance(constants, dict)
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    strategy_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NostalgiaForInfinityX7"
    )
    methods = {
        node.name: node for node in strategy_node.body if isinstance(node, ast.FunctionDef)
    }
    methods[method.name] = method
    route, _identity = _build_short_grind_route(constants, methods, strategy["methods"])
    assert route is not None
    return route


def test_short_route_rejects_a_dangling_rescue_reference() -> None:
    method = copy.deepcopy(_current_short_method())
    method.body = [
        statement
        for statement in method.body
        if not (
            isinstance(statement, ast.Assign)
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "gd5_liquidation_rescue_eligible"
        )
    ]

    with pytest.raises(StrategyAnalysisError, match="assignment changed"):
        _short_route_for_method(method)


def test_short_route_compiles_a_source_defined_rescue_cluster() -> None:
    method = copy.deepcopy(_current_short_method())
    for node in ast.walk(method):
        if isinstance(node, ast.Name) and node.id == "gd5_liquidation_rescue_eligible":
            node.id = "gd4_liquidation_rescue_eligible"

    route = _short_route_for_method(method)
    program = route["program"]
    assert isinstance(program, dict)
    rescue = next(
        transition["liquidation_rescue"]
        for transition in program["source_order"]
        if transition.get("liquidation_rescue") is not None
    )
    assert rescue["cluster_level"] == 4
