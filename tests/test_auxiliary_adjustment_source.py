from __future__ import annotations

import ast
import copy
from functools import lru_cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.x7.auxiliary_adjustment import prove_counted_entry_groups
from nfi_backtest_engine.x7.auxiliary_entry import prove_auxiliary_entry_inputs


@lru_cache(maxsize=1)
def methods():
    source = Path(
        "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    )
    cls = next(
        node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef)
    )
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("family", ["v3", "v4"])
def test_source_counts_include_active_rebuys_and_long_buybacks(side, family):
    groups = prove_counted_entry_groups(
        methods()[f"{side}_grind_adjust_trade_position_{family}"], list(range(1, 6)), side
    )
    assert [group["entry_tag"] for group in groups] == (
        ["buyback_1_entry", "rebuy_entry"] if side == "long" else ["rebuy_entry"]
    )
    assert groups[-1]["exit_tags"] == ["rebuy_exit", "rebuy_derisk", "derisk_global"]


@pytest.mark.parametrize(
    "mutation", ["increment", "first-entry", "reset", "shadow", "extract", "initialization"]
)
def test_source_counter_changes_cannot_reuse_the_order_scan(mutation):
    method = copy.deepcopy(methods()["long_grind_adjust_trade_position_v3"])
    if mutation == "increment":
        addition = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "rebuy_sub_grind_count"
        )
        addition.value = ast.Constant(value=2)
    elif mutation == "first-entry":
        loop = next(
            node
            for node in method.body
            if isinstance(node, ast.For) and ast.unparse(node.iter) == "reversed(filled_orders)"
        )
        loop.body[0].test = ast.parse("order.ft_order_side == 'buy'", mode="eval").body
    elif mutation == "reset":
        token = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Constant) and node.value == "rebuy_exit"
        )
        token.value = "other_exit"
    elif mutation == "initialization":
        initial = next(
            node
            for node in method.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "rebuy_sub_grind_count"
        )
        method.body.remove(initial)
        method.body.append(initial)
    else:
        loop = next(
            node
            for node in method.body
            if isinstance(node, ast.For) and ast.unparse(node.iter) == "reversed(filled_orders)"
        )
        entry = loop.body[0]
        if mutation == "shadow":
            entry.body[-1].test = ast.Constant(value=True)
        else:
            entry.body[0].value = ast.Constant(value="rebuy_entry")
    with pytest.raises(StrategyAnalysisError, match="counted-group"):
        prove_counted_entry_groups(method, list(range(1, 6)), "long")


@pytest.mark.parametrize("side", ["long", "short"])
def test_auxiliary_entry_operands_preserve_mode_maximum_and_derisk_helper(side):
    aliases, helper = prove_auxiliary_entry_inputs(
        methods()[f"{side}_grind_adjust_trade_position_v3"], side
    )
    assert helper == f"{side}_rebuy_entry_v3"
    assert ast.unparse(aliases["is_not_trade_max_stake_v3_1"]) == (
        "current_stake_amount < slice_amount * self.system_v3_1_max_stake"
    )
    assert ast.unparse(aliases["rebuy_max_sub_grinds"]) == (
        "len(self.system_v3_1_rebuy_stakes_futures if is_futures_mode "
        "else self.system_v3_1_rebuy_stakes_spot)"
    )


@pytest.mark.parametrize("mutation", ["helper", "maximum", "length", "mode"])
@pytest.mark.parametrize("side", ["long", "short"])
def test_changed_auxiliary_entry_operands_fail_closed(side, mutation):
    method = copy.deepcopy(methods()[f"{side}_grind_adjust_trade_position_v3"])
    if mutation == "helper":
        call = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == f"{side}_rebuy_entry_v3"
        )
        call.args[-1] = ast.Constant(value=False)
    elif mutation == "mode":
        field = next(
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute) and node.attr == "system_v3_1_rebuy_stakes_futures"
        )
        field.attr = "system_v3_1_rebuy_stakes_spot"
    else:
        name = "is_not_trade_max_stake_v3_1" if mutation == "maximum" else "rebuy_max_sub_grinds"
        assignment = next(
            node
            for node in method.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        )
        assignment.value = ast.Constant(value=100)
    with pytest.raises(StrategyAnalysisError, match="auxiliary entry"):
        prove_auxiliary_entry_inputs(method, side)
