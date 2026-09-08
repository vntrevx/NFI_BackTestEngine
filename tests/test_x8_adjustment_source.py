from __future__ import annotations

import ast
import copy
import hashlib
import json
from functools import cache
from pathlib import Path

import pytest
from nfi_backtest_engine.callback_order_state import _lower_x7_order_filled
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.x7.adjustment_ir import (
    _StaticInputLowerer,
    compile_system_adjustment_ir,
)
from nfi_backtest_engine.x7.adjustments import (
    _build_adjustment_constants,
    _build_rebuy_adjustment_constants,
)
from nfi_backtest_engine.x7.rebuy_ir import compile_rebuy_transition_ir

_SOURCE = Path("benchmarks/fixtures/source-contracts/x8-system-v4/strategy.source")


@cache
def _inputs() -> tuple[dict[str, ast.FunctionDef], dict]:
    source = _SOURCE.read_text(encoding="utf-8")
    strategy = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef))
    methods = {node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)}
    analysis = analyze_strategy(_SOURCE, class_name="SystemSourceContract")
    return methods, analysis["strategies"][0]["constants"]


def test_source_contract_retains_exact_donor_methods() -> None:
    provenance = json.loads(_SOURCE.with_name("provenance.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(_SOURCE.read_bytes()).hexdigest() == provenance["contract_sha256"]
    lines = _SOURCE.read_text(encoding="utf-8").splitlines(keepends=True)
    methods, _ = _inputs()
    assert set(methods) == set(provenance["methods"])
    for name, method in methods.items():
        segment = "".join(lines[method.lineno - 1 : method.end_lineno])
        assert hashlib.sha256(segment.encode()).hexdigest() == provenance["methods"][name]


@pytest.mark.parametrize("side", ["long", "short"])
def test_v4_adjustment_actions_compile_under_explicit_active_system_assumption(side: str) -> None:
    # This exercises compilation under an explicit assumption, not manager routing.
    # Production integration must prove the active system before supplying this flag.
    methods, constants = _inputs()
    method = methods[f"{side}_grind_adjust_trade_position_v4"]
    descriptor = _build_adjustment_constants(
        constants, method, side=side, system_prefix="system_v4", derisk_prefix="system_v4"
    )
    program = compile_system_adjustment_ir(
        method,
        methods[f"{side}_grind_exit_v4"],
        constants,
        side=side,
        retry_policy=descriptor["policy"],
        static_inputs={"is_system_v4": True},
        constant_prefix="system_v4_",
    )
    assert program["side"] == side
    assert program["execution_mode"] == "primary"
    assert descriptor["max_stake_multiplier"] == constants["system_v4_max_stake"]
    levels = {record["level"] for record in descriptor["grinds"]}
    actions = program["source_order"]
    assert {action["level"] for action in actions if action["kind"] == "grind-entry"} == levels
    assert {action["level"] for action in actions if action["kind"] == "grind-exit"} == levels
    assert any(
        binding["kind"] == "below-maximum-stake"
        for action in actions
        for binding in action["bindings"]
    )
    assert not any(
        binding["name"] == "is_system_v4" for action in actions for binding in action["bindings"]
    )


@pytest.mark.parametrize("side", ["long", "short"])
def test_v4_rebuy_transition_compiles_from_donor_source(side: str) -> None:
    methods, constants = _inputs()
    program = compile_rebuy_transition_ir(
        methods[f"{side}_rebuy_adjust_trade_position_v4"], constants, delegate_retry_ms=300_000
    )
    assert program["source_order"] == ["delegate", "decision"]
    assert program["execution_mode"] == "primary"
    assert program["decision_program"]["expressions"]
    assert _build_rebuy_adjustment_constants(constants, system_prefix="system_v4")


def test_v4_constant_selection_does_not_fall_back_to_legacy_values() -> None:
    methods, constants = _inputs()
    changed = copy.deepcopy(constants)
    changed["system_v4_max_stake"] = 13.0
    changed["system_v3_max_stake"] = 99.0
    method = methods["long_grind_adjust_trade_position_v4"]
    descriptor = _build_adjustment_constants(
        changed, method, side="long", system_prefix="system_v4", derisk_prefix="system_v4"
    )
    assert descriptor["max_stake_multiplier"] == 13.0
    del changed["system_v4_max_stake"]
    with pytest.raises(StrategyAnalysisError, match="system_v4_max_stake"):
        _build_adjustment_constants(
            changed, method, side="long", system_prefix="system_v4", derisk_prefix="system_v4"
        )


def test_donor_order_filled_preserves_no_exits_guard() -> None:
    methods, constants = _inputs()
    lowered = _lower_x7_order_filled(methods["order_filled"], constants=constants)
    assert lowered is not None
    assert lowered["operation"]["initial_entry_requires_no_exits"] is True
    assert {"key": "system_version", "value": constants["system_v4_name"]} in (
        lowered["operation"]["initial_successful_entry_writes"]
    )


@pytest.mark.parametrize("selected", [True, False])
def test_static_input_selects_only_the_proven_conditional_arm(selected: bool) -> None:
    expression = ast.parse("self.active if enabled else self.inactive", mode="eval")
    lowered = _StaticInputLowerer({"enabled": selected}).visit(expression)
    assert ast.unparse(lowered) == ("self.active" if selected else "self.inactive")
    with pytest.raises(StrategyAnalysisError, match="reassigned"):
        _StaticInputLowerer({"enabled": selected}).visit(ast.parse("enabled = False"))


@pytest.mark.parametrize("side", ["long", "short"])
def test_rebuy_delegate_preserves_corrected_minimum_and_rejects_changed_transfer(side: str) -> None:
    source = _SOURCE.with_name("minimum-stake.source")
    provenance = json.loads(source.with_name("minimum-stake-provenance.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance["contract_sha256"]
    helper = next(
        n for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.FunctionDef)
    )
    methods, constants = _inputs()
    method = methods[f"{side}_rebuy_adjust_trade_position_v4"]
    program = compile_rebuy_transition_ir(
        method, constants, delegate_retry_ms=300_000, corrected_minimum_method=helper
    )
    assert program["delegate"]["preserve_corrected_minimum"] is True
    changed = copy.deepcopy(method)
    assignment = next(
        node
        for node in changed.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "min_stake"
    )
    assignment.value = ast.BinOp(
        assignment.value, ast.Div(), ast.Name("trade_leverage", ast.Load())
    )
    with pytest.raises(StrategyAnalysisError, match="stake transfer"):
        compile_rebuy_transition_ir(
            changed, constants, delegate_retry_ms=300_000, corrected_minimum_method=helper
        )
    changed_helper = copy.deepcopy(helper)
    changed_helper.args.args[1:3] = reversed(changed_helper.args.args[1:3])
    with pytest.raises(StrategyAnalysisError, match="correction signature"):
        compile_rebuy_transition_ir(
            method,
            constants,
            delegate_retry_ms=300_000,
            corrected_minimum_method=changed_helper,
        )
    helper.body[-1] = ast.Return(ast.Constant(0.0))
    with pytest.raises(StrategyAnalysisError, match="minimum correction"):
        compile_rebuy_transition_ir(
            method, constants, delegate_retry_ms=300_000, corrected_minimum_method=helper
        )


def test_disabled_adjustment_can_retain_its_exit_manager_contract_explicitly() -> None:
    methods, constants = _inputs()
    constants = constants | {"position_adjustment_enable": False}
    kwargs = {"side": "long", "system_prefix": "system_v4", "derisk_prefix": "system_v4"}
    with pytest.raises(StrategyAnalysisError, match="adjustment is disabled"):
        _build_adjustment_constants(
            constants, methods["long_grind_adjust_trade_position_v4"], **kwargs
        )
    contract = _build_adjustment_constants(
        constants, methods["long_grind_adjust_trade_position_v4"], allow_disabled=True, **kwargs
    )
    assert len(contract["grinds"]) == 5


@pytest.mark.parametrize("side", ["long", "short"])
def test_source_cluster_distance_preserves_the_source_sign(side: str) -> None:
    import ast

    from nfi_backtest_engine.errors import StrategyAnalysisError
    from nfi_backtest_engine.x7.adjustment_ir import _prove_raw_cluster_distances

    methods, _ = _inputs()
    method = copy.deepcopy(methods[f"{side}_grind_adjust_trade_position_v4"])
    _prove_raw_cluster_distances(method, [1, 2, 3, 4, 5])
    assignment = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "grind_5_distance_ratio"
        and not isinstance(node.value, ast.Constant)
    )
    assignment.value = ast.UnaryOp(op=ast.USub(), operand=assignment.value)
    with pytest.raises(StrategyAnalysisError, match="cluster distance changed"):
        _prove_raw_cluster_distances(method, [1, 2, 3, 4, 5])
