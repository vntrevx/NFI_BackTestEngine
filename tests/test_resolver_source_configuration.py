from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.source_configuration import resolve_source_parameter_overrides
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.strategy_resolver_settings import INHERITED_SETTINGS

_EVIDENCE = Path("benchmarks/evidence/x8-settings-2026-09-08")
_SOURCE = Path(
    "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
)
_CASES = json.loads((_EVIDENCE / "resolver-cases.json").read_text())["cases"]


@lru_cache(maxsize=1)
def _source():
    analysis = analyze_strategy(_SOURCE)
    cls = next(
        node for node in ast.parse(_SOURCE.read_text()).body if isinstance(node, ast.ClassDef)
    )
    return cls, analysis["strategies"][0]["constants"]


@pytest.mark.parametrize("case", _CASES)
def test_resolver_precedence_matches_pinned_full_constructor(case):
    cls, constants = _source()
    if "expected_error" in case:
        with pytest.raises(StrategyAnalysisError, match=case["expected_error"]):
            resolve_source_parameter_overrides(cls, constants, case["config"])
        return
    effective = (
        INHERITED_SETTINGS
        | constants
        | resolve_source_parameter_overrides(
            cls,
            constants,
            case["config"],
        )
    )
    assert {key: effective[key] for key in case["expected"]} == case["expected"]


@pytest.mark.parametrize("attribute", ["target_profit_cache", "hold_trades_cache"])
def test_runtime_cache_objects_cannot_be_overridden_as_static_data(attribute):
    cls, constants = _source()
    with pytest.raises(StrategyAnalysisError, match="initialization|runtime state"):
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "nfi_advanced_mode": True,
                "nfi_parameters": {attribute: {}},
            },
        )


def test_active_system_flags_do_not_invent_a_version_from_missing_names():
    from nfi_backtest_engine.x7.adjustment_dispatch import active_system_flags

    flags = active_system_flags({})
    assert not any(
        flags[name] for name in ("is_system_v3", "is_system_v3_1", "is_system_v3_2", "is_system_v4")
    )


def test_derisk_switch_uses_the_selected_source_assignment():
    from nfi_backtest_engine.x7.adjustment_specialization import source_derisk_switches

    cls, constants = _source()
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "long_grind_adjust_trade_position_v4"
    )
    changed = constants | {"system_v4_derisk_level_4_enable": True}
    assert source_derisk_switches(method, changed)["derisk_4_enable"] is False


@pytest.mark.parametrize("side", ["long", "short"])
def test_legacy_system_operand_specialization_requires_source_proof(side):
    import copy

    from nfi_backtest_engine.x7.adjustment_dispatch import prove_adjustment_inputs

    cls, _ = _source()
    method = copy.deepcopy(
        next(
            node
            for node in cls.body
            if isinstance(node, ast.FunctionDef)
            and node.name == f"{side}_grind_adjust_trade_position_v3"
        )
    )
    prove_adjustment_inputs(method, side=side, family="v3")
    assignment = next(
        node
        for node in method.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "is_not_trade_max_stake_v3"
    )
    assignment.value = ast.Constant(value=True)
    with pytest.raises(StrategyAnalysisError, match="source operand changed"):
        prove_adjustment_inputs(method, side=side, family="v3")
