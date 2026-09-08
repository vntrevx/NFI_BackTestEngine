from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.x7.managed_short_exit_ir import (
    compile_managed_short_exit_ir,
    managed_short_route_specs,
)
from nfi_backtest_engine.x7.route_contracts import MANAGED_SHORT_ROUTE_SPECS
from nfi_backtest_engine.x7.routes import _build_managed_short_routes

_SOURCE = Path(
    "benchmarks/fixtures/captured/explicit-short-top-coins-futures-r1/inputs/strategy.py"
)
type SourceContext = tuple[dict[str, ast.FunctionDef], dict[str, Any]]


@pytest.fixture(scope="module")
def source_context() -> SourceContext:
    analysis = analyze_strategy(_SOURCE)
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    selected = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShortTopCoinsProbe"
    )
    required = {"custom_exit", "exit_profit_target", "short_exit_top_coins"}
    required.update(spec.method for spec in MANAGED_SHORT_ROUTE_SPECS)
    methods = {
        node.name: node for node in selected.body
        if isinstance(node, ast.FunctionDef) and node.name in required
    }
    return methods, analysis["strategies"][0]["constants"]


def test_explicit_top_coins_compiles_its_own_mode_and_complete_state_policy(
    source_context: SourceContext,
) -> None:
    methods, constants = source_context
    compiled = compile_managed_short_exit_ir(
        methods, constants, managed_short_route_specs(methods),
    )

    assert compiled.short_route_order == (
        "short_normal", "short_pump", "short_quick", "short_rebuy", "short_high_profit",
        "short_rapid", "short_top_coins", "short_scalp",
    )
    route = compiled.program["routes"][-2]
    assert route["id"] == "short_top_coins"
    assert route["mode_name"] == "short_tc"
    assert route["match"] == {"operator": "any", "entry_tags": ["641", "642"]}
    assert route["profit_basis"] == "initial-stake"
    assert route["initial_profit_gate"] is None
    assert route["decision_program_order"] == [
        "short_exit_signals", "short_exit_main", "short_exit_williams_r", "short_exit_dec",
    ]
    assert route["state_program"]["target"]["u_e_raise_delta"] == 0.005
    assert route["state_program"]["target"]["max_target_floor"] == 0.005
    assert route["terminal_exit"] is None


def test_dormant_wrapper_does_not_select_explicit_dispatch(source_context: SourceContext) -> None:
    methods, _ = copy.deepcopy(source_context)
    custom_exit = methods["custom_exit"]
    custom_exit.body = [
        node for node in custom_exit.body
        if not isinstance(node, ast.If) or "short_exit_top_coins" not in ast.unparse(node)
    ]

    assert "short_exit_top_coins" in methods
    assert managed_short_route_specs(methods) == MANAGED_SHORT_ROUTE_SPECS


def test_descriptor_uses_top_coins_profile_without_duplicate_fallback_tags(
    source_context: SourceContext,
) -> None:
    methods, constants = source_context
    routes = _build_managed_short_routes(constants, managed_short_route_specs(methods))

    assert "short_top_coins_fallback" not in routes
    assert routes["short_top_coins"]["profile"] == "top-coins"
    assert routes["short_top_coins"]["mode_name"] == "short_tc"
    assert routes["short_top_coins"]["entry_tags"] == ["641", "642"]


@pytest.mark.parametrize("tag", ["641", "620", "501"])
def test_reachable_normal_fallback_cannot_be_silently_dropped(
    source_context: SourceContext, tag: str,
) -> None:
    methods, constants = copy.deepcopy(source_context)
    constants["short_exit_known_mode_tags"].remove(tag)

    with pytest.raises(StrategyAnalysisError, match="unclassified short routes: short_exit_normal"):
        compile_managed_short_exit_ir(methods, constants, managed_short_route_specs(methods))


def test_explicit_router_reordering_fails_closed(source_context: SourceContext) -> None:
    methods, constants = copy.deepcopy(source_context)
    body = methods["custom_exit"].body
    top_index = next(
        index for index, node in enumerate(body)
        if isinstance(node, ast.If) and "self.short_exit_top_coins(" in ast.unparse(node)
    )
    scalp_index = next(
        index for index, node in enumerate(body)
        if isinstance(node, ast.If) and "self.short_exit_scalp(" in ast.unparse(node)
    )
    body[top_index], body[scalp_index] = body[scalp_index], body[top_index]

    with pytest.raises(StrategyAnalysisError, match="short route inventory changed"):
        compile_managed_short_exit_ir(methods, constants, managed_short_route_specs(methods))


def test_explicit_router_rejects_an_unbound_tag_alias(source_context: SourceContext) -> None:
    methods, constants = copy.deepcopy(source_context)
    router = methods["custom_exit"]
    router.body = [
        node for node in router.body
        if not (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "short_top_coins_mode_tags"
                for target in node.targets
            )
        )
    ]

    with pytest.raises(StrategyAnalysisError, match="short_top_coins short tag/side matcher"):
        compile_managed_short_exit_ir(methods, constants, managed_short_route_specs(methods))


def test_explicit_wrapper_predicate_is_compiled_from_source(source_context: SourceContext) -> None:
    methods, constants = copy.deepcopy(source_context)
    wrapper = methods["short_exit_top_coins"]
    threshold = next(
        node for node in ast.walk(wrapper)
        if isinstance(node, ast.Compare) and ast.unparse(node) == "profit_init_ratio >= 0.005"
    )
    threshold.comparators[0] = ast.Constant(value=0.007)

    compiled = compile_managed_short_exit_ir(methods, constants, managed_short_route_specs(methods))

    assert compiled.program["routes"][-2]["state_program"]["target"]["max_target_floor"] == 0.007
