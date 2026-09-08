from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.x7.adjustment_dispatch import compile_adjustment_dispatch

_SOURCE = Path("benchmarks/fixtures/source-contracts/x8-system-v4/dispatch.source")


def _inputs() -> tuple[dict[str, ast.FunctionDef], dict]:
    strategy = next(
        node for node in ast.parse(_SOURCE.read_text()).body if isinstance(node, ast.ClassDef)
    )
    methods = {node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)}
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in strategy.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    return methods, constants


def test_source_dispatch_compiles_to_the_rust_test_contract() -> None:
    methods, constants = _inputs()
    compiled = compile_adjustment_dispatch(methods, constants)
    assert compiled == json.loads(_SOURCE.with_name("dispatch-program.json").read_text())
    provenance = json.loads(_SOURCE.with_name("dispatch-provenance.json").read_text())
    assert hashlib.sha256(_SOURCE.read_bytes()).hexdigest() == provenance["contract_sha256"]
    lines = _SOURCE.read_text().splitlines(keepends=True)
    for name, method in methods.items():
        segment = "".join(lines[method.lineno - 1 : method.end_lineno])
        assert hashlib.sha256(segment.encode()).hexdigest() == provenance["methods"][name]


@pytest.mark.parametrize("mutation", ["system-marker", "unguarded-entry", "arguments", "v4-branch"])
def test_changed_dispatch_assumptions_fail_closed(mutation: str) -> None:
    methods, constants = _inputs()
    if mutation == "system-marker":
        helper = methods["is_system_v4"]
        key = next(
            node
            for node in ast.walk(helper)
            if isinstance(node, ast.Constant) and node.value == "system_version"
        )
        key.value = "different_marker"
    elif mutation == "unguarded-entry":
        guard = next(
            node
            for node in methods["order_filled"].body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.BoolOp)
            and isinstance(node.test.op, ast.And)
        )
        guard.test = guard.test.values[0]
    elif mutation == "arguments":
        assignment = next(
            node
            for node in methods["adjust_trade_position"].body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "args"
        )
        assert isinstance(assignment.value, ast.Tuple)
        assignment.value.elts[3] = ast.Name(id="current_entry_rate", ctx=ast.Load())
    else:
        for node in ast.walk(methods["adjust_trade_position"]):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "is_system_v4"
            ):
                node.test = ast.Constant(value=False)
    with pytest.raises(StrategyAnalysisError):
        compile_adjustment_dispatch(methods, constants)


def test_tag_predicate_changes_recompile_without_strategy_or_hash_gate() -> None:
    methods, constants = _inputs()
    original = compile_adjustment_dispatch(methods, constants)
    changed = copy.deepcopy(constants)
    changed["long_rebuy_mode_tags"].append("new-rebuy-tag")
    changed["long_rebuy_grind_mode_tags"].append("new-rebuy-tag")
    changed["long_known_mode_tags"].append("new-rebuy-tag")
    compiled = compile_adjustment_dispatch(methods, changed)
    assert compiled["fingerprint"] != original["fingerprint"]
    assert any(
        "new-rebuy-tag" in record["entry_tags"] for record in compiled["predicates"].values()
    )


def test_disabled_adjustment_dispatch_returns_none_before_any_route() -> None:
    methods, constants = _inputs()
    compiled = compile_adjustment_dispatch(
        methods, constants | {"position_adjustment_enable": False}
    )
    first = compiled["program"]["statements"][0]
    assert first[0] == "return"
    assert compiled["program"]["expressions"][first[1]] == ["literal", None]
    assert compiled["predicates"]
