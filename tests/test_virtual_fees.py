from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.x7.virtual_fees import compile_virtual_fee_policy

_ROOT = Path("benchmarks/fixtures/source-contracts")


def _methods() -> dict[str, ast.FunctionDef]:
    methods = {}
    for path in (_ROOT / "virtual-fees/profit.source", _ROOT / "x8-system-v4/strategy.source"):
        cls = next(
            node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef)
        )
        methods.update({node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)})
    return methods


def test_virtual_fee_policy_matches_donor_and_preserves_independent_fallbacks() -> None:
    methods = _methods()
    assert compile_virtual_fee_policy(methods, {}) is None
    assert compile_virtual_fee_policy(methods, {}, require_source_snapshot=True) == {
        "open_rate": None,
        "close_rate": None,
    }
    assert compile_virtual_fee_policy(methods, {"custom_fee_open_rate": 0}) == {
        "open_rate": 0,
        "close_rate": None,
    }
    assert compile_virtual_fee_policy(methods, {"custom_fee_close_rate": 0.002}) == {
        "open_rate": None,
        "close_rate": 0.002,
    }
    source = _ROOT / "virtual-fees/profit.source"
    provenance = json.loads(source.with_name("provenance.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance["contract_sha256"]
    method = methods["calc_total_profit"]
    segment = "".join(
        source.read_text().splitlines(keepends=True)[method.lineno - 1 : method.end_lineno]
    )
    assert hashlib.sha256(segment.encode()).hexdigest() == provenance["method_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        "arithmetic",
        "fallback",
        "all-fallbacks",
        "reassign",
        "reader",
        "writer",
        "cluster",
        "cluster-write",
    ],
)
def test_changed_virtual_fee_semantics_fail_closed(mutation: str) -> None:
    methods = _methods()
    if mutation == "arithmetic":
        methods["calc_total_profit"].body[-1] = ast.parse("return 0, 0, 0, 0").body[0]
    else:
        method = methods["long_grind_adjust_trade_position_v4"]
        if mutation == "fallback":
            assignment = next(
                node
                for node in method.body
                if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "fee_open_rate"
            )
            assignment.value = ast.parse("trade_fee_open", mode="eval").body
        elif mutation == "reassign":
            method.body.append(ast.parse("fee_close_rate = 0").body[0])
        elif mutation == "all-fallbacks":
            for node in method.body:
                if isinstance(node, ast.Assign):
                    target = ast.unparse(node.targets[0])
                    if target in {"fee_open_rate", "fee_close_rate"}:
                        node.value = ast.parse(
                            "trade_" + target.removesuffix("_rate"), mode="eval"
                        ).body
        elif mutation == "cluster-write":
            method.body.append(ast.parse("grind_1_current_grind_stake += 1").body[0])
        elif mutation == "reader":
            method.body.append(ast.parse("return self.custom_fee_open_rate").body[0])
        elif mutation == "cluster":
            for node in ast.walk(method):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and (
                        ast.unparse(node.targets[0]) == "grind_1_current_grind_stake"
                        and not isinstance(node.value, ast.Constant)
                    )
                ):
                    for value in ast.walk(node.value):
                        if isinstance(value, ast.Name) and value.id == "trade_fee_close":
                            value.id = "fee_close_rate"
        else:
            method.body.append(ast.parse("self.custom_fee_open_rate = 0").body[0])
    with pytest.raises(StrategyAnalysisError, match="virtual.fee"):
        compile_virtual_fee_policy(methods, {"custom_fee_open_rate": 0.001})


@pytest.mark.parametrize("value", [True, "0.001", -0.1, 1.0, float("nan"), float("inf")])
def test_invalid_source_fee_defaults_fail_closed(value) -> None:
    with pytest.raises(StrategyAnalysisError, match="virtual fee"):
        compile_virtual_fee_policy(_methods(), {"custom_fee_open_rate": value})


def test_complete_donor_binds_the_distinct_legacy_short_fee_rule() -> None:
    source = Path(
        "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    )
    provenance = json.loads((_ROOT / "virtual-fees/provenance.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance["donor_sha256"]
    cls = next(
        node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef)
    )
    methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
    assert compile_virtual_fee_policy(methods, {}, require_source_snapshot=True) == {
        "open_rate": None,
        "close_rate": None,
        "legacy_short_close_fee_additive": True,
    }
    method = methods["short_grind_adjust_trade_position"]
    assignment = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and ast.unparse(node.targets[0]) == "grind_1_current_grind_stake"
        and not isinstance(node.value, ast.Constant)
    )
    assignment.value = ast.parse(
        "grind_1_total_amount * exit_rate * (1 - trade_fee_close)", mode="eval"
    ).body
    with pytest.raises(StrategyAnalysisError, match="cluster accounting"):
        compile_virtual_fee_policy(methods, {}, require_source_snapshot=True)
