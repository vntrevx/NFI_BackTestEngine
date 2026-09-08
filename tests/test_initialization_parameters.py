from __future__ import annotations

import ast
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.initialization_parameters import initialization_parameters
from nfi_backtest_engine.source_configuration import (
    configured_analysis,
    resolve_source_parameter_overrides,
)
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.strategy_resolver_settings import INHERITED_SETTINGS

_EVIDENCE = Path("benchmarks/evidence/x8-initialization-2026-09-08")
_SOURCE = Path(
    "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
)
_OFFICIAL = json.loads((_EVIDENCE / "official-results.json").read_text())


@lru_cache(maxsize=1)
def source():
    cls = next(n for n in ast.parse(_SOURCE.read_text()).body if isinstance(n, ast.ClassDef))
    constants = INHERITED_SETTINGS | analyze_strategy(_SOURCE)["strategies"][0]["constants"]
    return cls, constants


def test_constructor_probe_is_authenticated():
    receipt = json.loads((_EVIDENCE / "official-run.json").read_text())
    assert receipt["exit_code"] == 0
    assert (
        hashlib.sha256((_EVIDENCE / "official-results.json").read_bytes()).hexdigest()
        == receipt["output_sha256"]
    )
    assert (
        hashlib.sha256((_EVIDENCE / "official-probe.source").read_bytes()).hexdigest()
        == receipt["script_sha256"]
    )
    assert hashlib.sha256(_SOURCE.read_bytes()).hexdigest() == _OFFICIAL["source_sha256"]


def test_initialization_configuration_can_be_reapplied_by_callback_compilers():
    config = {
        "trading_mode": "futures",
        "nfi_advanced_mode": True,
        "nfi_parameters": {
            "is_futures_mode": False,
            "can_short": False,
            "target_profit_cache": None,
            "hold_trades_cache": None,
        },
    }
    analysis = analyze_strategy(_SOURCE)
    once = configured_analysis(analysis, config)
    twice = configured_analysis(once, config)
    assert once == twice
    assert twice["strategies"][0]["constants"]["is_futures_mode"] is True
    assert twice["strategies"][0]["constants"]["can_short"] is True


@pytest.mark.parametrize("case", _OFFICIAL["cases"])
def test_initialization_overrides_preserve_official_constructor_outcomes(case):
    cls, constants = source()
    name = case["parameter"]
    config = {
        "trading_mode": case["trading_mode"],
        "nfi_advanced_mode": True,
        "nfi_parameters": {name: case["configured"]},
    }
    if case["trading_mode"] == "spot" and case["configured"] is True:
        with pytest.raises(StrategyAnalysisError, match="source mode differs"):
            resolve_source_parameter_overrides(cls, constants, config)
        return
    result = resolve_source_parameter_overrides(cls, constants, config)
    if name == "target_profit_cache":
        # Preserve fresh initialization; the scalar inventory cannot contain a Cache object.
        assert result[name] is None and case["effective"] == {"type": "Cache"}
    else:
        assert result[name] == case["effective"]


@pytest.mark.parametrize(
    "mutation", ["base", "prefix", "prefix-call", "default", "mode", "runtime-write"]
)
def test_changed_initialization_assumptions_fail_closed(mutation):
    _, constants = source()
    cls = next(n for n in ast.parse(_SOURCE.read_text()).body if isinstance(n, ast.ClassDef))
    initializer = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    if mutation == "base":
        cls.bases[0] = ast.Name(id="DifferentBase", ctx=ast.Load())
    elif mutation == "prefix":
        initializer.body.insert(0, ast.parse("self.target_profit_cache = None").body[0])
    elif mutation == "prefix-call":
        initializer.body.insert(0, ast.parse("mutate(self)").body[0])
    elif mutation == "default":
        declaration = next(
            n
            for n in cls.body
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "target_profit_cache"
        )
        declaration.value = ast.Constant(value=False)
    elif mutation == "mode":
        assignment = next(
            n
            for n in ast.walk(initializer)
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Attribute)
            and n.targets[0].attr == "is_futures_mode"
        )
        assignment.value = ast.Constant(value=False)
    else:
        method = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name != "__init__"
        )
        method.body.append(ast.parse("self.is_futures_mode = False").body[0])
    with pytest.raises(StrategyAnalysisError, match="initialization"):
        initialization_parameters(
            cls,
            initializer,
            constants,
            {"trading_mode": "futures"},
            {"target_profit_cache": None, "is_futures_mode": False},
        )
