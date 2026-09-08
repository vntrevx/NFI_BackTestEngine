from __future__ import annotations

import ast
import copy

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.source_configuration import resolve_source_parameter_overrides
from test_source_configuration import _inputs


def test_advanced_static_values_preserve_instance_assignment_and_aliases() -> None:
    cls, constants = _inputs()
    constants.update(stake_steps=[0.1, 0.2], combined_steps=[0.1, 0.2, 0.3], mode="original")
    original = copy.deepcopy(constants)
    config = {
        "nfi_advanced_mode": 1,
        "nfi_parameters": {"stake_steps": [0.4, 0.6], "mode": "configured"},
    }
    updates = resolve_source_parameter_overrides(cls, constants, config)
    assert updates == {"stake_steps": [0.4, 0.6], "mode": "configured"}
    assert (constants | updates)["combined_steps"] == [0.1, 0.2, 0.3]
    assert constants == original
    config["nfi_parameters"]["stake_steps"].append(0.8)
    assert updates["stake_steps"] == [0.4, 0.6]


@pytest.mark.parametrize("enabled", [False, "true", 2, None])
def test_advanced_mode_uses_source_equality(enabled) -> None:
    cls, constants = _inputs()
    constants["stake_steps"] = [0.1]
    assert (
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "nfi_advanced_mode": enabled,
                "nfi_parameters": {"stake_steps": [0.3]},
            },
        )
        == {}
    )


@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf"), 10**1000])
def test_advanced_numeric_parameters_reject_invalid_values(value) -> None:
    cls, constants = _inputs()
    constants["stake_multiplier"] = 0.3
    with pytest.raises(StrategyAnalysisError, match="invalid value"):
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "nfi_advanced_mode": True,
                "nfi_parameters": {"stake_multiplier": value},
            },
        )


@pytest.mark.parametrize("statement", ["self.stake_multiplier = 0.8", "x = self.stake_multiplier"])
def test_advanced_initialization_dependencies_cannot_be_frozen_as_final_values(statement) -> None:
    cls, constants = _inputs()
    constants["stake_multiplier"] = 0.3
    init = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    init.body.extend(ast.parse(statement).body)
    with pytest.raises(StrategyAnalysisError, match="consumed during initialization"):
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "nfi_advanced_mode": True,
                "nfi_parameters": {"stake_multiplier": 0.5},
            },
        )


def test_advanced_runtime_mutable_resources_are_not_static_parameters() -> None:
    cls, constants = _inputs()
    constants["resource"] = {}
    cls.body.extend(ast.parse("def update(self):\n self.resource = {}\n").body)
    with pytest.raises(StrategyAnalysisError, match="mutable runtime state"):
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "nfi_advanced_mode": True,
                "nfi_parameters": {"resource": {}},
            },
        )
