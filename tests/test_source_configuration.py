from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.source_configuration import resolve_source_parameter_overrides

_SOURCE = Path("benchmarks/fixtures/source-contracts/x8-system-v4/configuration.source")


def _inputs():
    cls = next(
        node for node in ast.parse(_SOURCE.read_text()).body if isinstance(node, ast.ClassDef)
    )
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in cls.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    return cls, constants


def test_source_configuration_precedence_and_partial_signal_updates() -> None:
    cls, constants = _inputs()
    original = copy.deepcopy(constants)
    config = {
        "nfi_parameters": {
            "futures_mode_leverage": 2,
            "system_v4_bad_trade_exit_enable": False,
            "long_entry_signal_params": {"long_entry_condition_1_enable": False, "unknown": True},
        },
        "futures_mode_leverage": 4,
        "long_entry_signal_params": {"long_entry_condition_2_enable": False},
    }
    result = resolve_source_parameter_overrides(cls, constants, config)
    assert result["futures_mode_leverage"] == 4
    assert result["system_v4_bad_trade_exit_enable"] is False
    expected = constants["long_entry_signal_params"] | {
        "long_entry_condition_1_enable": False,
        "long_entry_condition_2_enable": False,
    }
    assert result["long_entry_signal_params"] == expected
    assert constants == original
    assert resolve_source_parameter_overrides(cls, constants, {"nfi_parameters": []}) == {}
    assert (
        resolve_source_parameter_overrides(cls, constants, {"nfi_parameters": {"unknown": 1}}) == {}
    )


@pytest.mark.parametrize(
    "mutation", ["precedence", "extra-write", "advanced-predicate", "signal-body"]
)
def test_changed_configuration_initialization_fails_closed(mutation: str) -> None:
    cls, constants = _inputs()
    init = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    if mutation == "precedence":
        index = next(i for i, node in enumerate(init.body) if isinstance(node, ast.For))
        init.body.insert(0, init.body.pop(index))
    elif mutation == "extra-write":
        init.body.append(ast.parse('setattr(self, "stops_enable", False)').body[0])
    elif mutation == "advanced-predicate":
        assignment = next(
            node
            for node in init.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "is_config_advanced_mode"
        )
        assignment.value = ast.Constant(True)
    else:
        helper = next(
            node
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "update_signals_from_config"
        )
        helper.body.reverse()
    with pytest.raises(StrategyAnalysisError):
        resolve_source_parameter_overrides(cls, constants, {"stops_enable": False})


@pytest.mark.parametrize(
    "key,value",
    [
        ("target_profit_cache", {}),
        ("custom_fee_open_rate", "0.01"),
        ("max_entry_position_adjustment", 0),
        ("unreviewed_inherited_attribute", 1),
    ],
)
def test_unrepresented_configuration_never_silently_uses_defaults(key, value) -> None:
    cls, constants = _inputs()
    with pytest.raises(StrategyAnalysisError):
        resolve_source_parameter_overrides(
            cls, constants, {"nfi_advanced_mode": True, "nfi_parameters": {key: value}}
        )


def test_configuration_contract_preserves_donor_methods() -> None:
    cls, _ = _inputs()
    provenance = json.loads(_SOURCE.with_name("configuration-provenance.json").read_text())
    assert hashlib.sha256(_SOURCE.read_bytes()).hexdigest() == provenance["contract_sha256"]
    lines = _SOURCE.read_text().splitlines(keepends=True)
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            segment = "".join(lines[node.lineno - 1 : node.end_lineno])
            assert hashlib.sha256(segment.encode()).hexdigest() == provenance["methods"][node.name]


def test_source_parameter_updates_match_pinned_constructor_capture() -> None:
    evidence = json.loads(
        Path(
            "benchmarks/evidence/x8-system-v4-2026-09-07/official-configuration-cases.json"
        ).read_text()
    )
    cls, constants = _inputs()
    assert hashlib.sha256(_SOURCE.read_bytes()).hexdigest() == evidence["source_sha256"]
    for case in evidence["cases"]:
        effective = constants | resolve_source_parameter_overrides(cls, constants, case["config"])
        assert {key: effective[key] for key in case["effective"]} == case["effective"]


def test_resolver_adjustment_settings_override_source_after_construction() -> None:
    cls, constants = _inputs()
    constants["position_adjustment_enable"] = True
    result = resolve_source_parameter_overrides(
        cls,
        constants,
        {
            "position_adjustment_enable": False,
            "max_entry_position_adjustment": 0,
        },
    )
    assert result == {"position_adjustment_enable": False, "max_entry_position_adjustment": 0}
    assert constants["position_adjustment_enable"] is True
    assert (
        resolve_source_parameter_overrides(
            cls,
            constants,
            {
                "position_adjustment_enable": True,
                "max_entry_position_adjustment": -1,
            },
        )
        == {}
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("position_adjustment_enable", "false"),
        ("position_adjustment_enable", 0),
        ("max_entry_position_adjustment", True),
        ("max_entry_position_adjustment", -2),
        ("max_entry_position_adjustment", 0.5),
    ],
)
def test_invalid_resolver_adjustment_values_fail_closed(key, value) -> None:
    cls, constants = _inputs()
    with pytest.raises(StrategyAnalysisError, match="resolver parameter"):
        resolve_source_parameter_overrides(cls, constants, {key: value})


def test_virtual_fee_overrides_preserve_zero_none_and_legacy_precedence() -> None:
    cls, constants = _inputs()
    result = resolve_source_parameter_overrides(cls, constants, {
        "nfi_parameters": {"custom_fee_open_rate": 0.002, "custom_fee_close_rate": 0.003},
        "custom_fee_open_rate": 0,
        "custom_fee_close_rate": None,
    })
    assert result == {"custom_fee_open_rate": 0, "custom_fee_close_rate": None}


@pytest.mark.parametrize("value", [True, "0.001", -0.001, 1, float("nan"), float("inf")])
def test_invalid_virtual_fee_overrides_fail_closed(value) -> None:
    cls, constants = _inputs()
    with pytest.raises(StrategyAnalysisError, match="virtual fee"):
        resolve_source_parameter_overrides(cls, constants, {"custom_fee_close_rate": value})
