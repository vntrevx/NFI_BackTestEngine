from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.x7.adjustment_ir import compile_system_adjustment_ir
from nfi_backtest_engine.x7.adjustments import _build_adjustment_constants
from nfi_backtest_engine.x7.auxiliary_prices import prove_group_exit_ids, prove_group_prices


@cache
def inputs():
    source = Path(
        "benchmarks/fixtures/captured/x8-original-futures-20220401-20220420-r1/inputs/strategy.py"
    )
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef))
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}, analyze_strategy(
        source
    )["strategies"][0]["constants"]


def prove(method):
    assert prove_group_prices(method, "buyback_1") == "derisk_level_4"
    exits = [
        node
        for node in method.body
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
            and child.targets[0].id == "order_tag"
            and isinstance(child.value, ast.Constant)
            and child.value.value == "buyback_1_derisk"
            for child in ast.walk(node)
        )
    ]
    assert len(exits) == 1
    prove_group_exit_ids(method, "buyback_1", exits[0])


@pytest.mark.parametrize("family", ["v3", "v4"])
def test_donor_buyback_group_prices_and_exit_ids_are_proved(family):
    methods, _ = inputs()
    prove(methods[f"long_grind_adjust_trade_position_{family}"])


@pytest.mark.parametrize("family", ["v3", "v4"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "buyback_1_total_cost += order.safe_filled * order.safe_price",
            "buyback_1_total_cost += order.safe_filled",
        ),
        (
            "buyback_1_total_amount += order.safe_filled",
            "buyback_1_total_amount += order.safe_filled * 2",
        ),
        ("buyback_1_current_open_rate = 0.0", "buyback_1_current_open_rate = 1.0"),
        (
            "buyback_1_current_open_rate = buyback_1_total_cost / buyback_1_total_amount",
            "buyback_1_current_open_rate = buyback_1_total_cost * buyback_1_total_amount",
        ),
        ("elif is_derisk_4_found:", "elif is_derisk_3_found:"),
        (
            "(exit_rate - derisk_4_order.safe_price) / derisk_4_order.safe_price",
            "(exit_rate - derisk_3_order.safe_price) / derisk_3_order.safe_price",
        ),
        ("buyback_1_buy_orders.append(order.id)", "buyback_1_buy_orders.append(order.id + 1)"),
        (
            "for grind_entry_id in buyback_1_buy_orders:",
            "for grind_entry_id in reversed(buyback_1_buy_orders):",
        ),
        (
            "buyback_1_current_grind_stake - buyback_1_total_cost",
            "buyback_1_current_grind_stake + buyback_1_total_cost",
        ),
        ("trade_fee_close = trade.fee_close", "trade_fee_close = 0.0"),
    ],
)
def test_changed_buyback_accounting_cannot_reuse_source_group_contract(family, old, new):
    methods, _ = inputs()
    source = ast.unparse(methods[f"long_grind_adjust_trade_position_{family}"])
    assert old in source
    changed = ast.parse(source.replace(old, new)).body[0]
    with pytest.raises(StrategyAnalysisError):
        prove(changed)


@pytest.mark.parametrize("system", ["v3", "v3_1", "v3_2", "v4"])
def test_enabled_buyback_retains_source_mode_guards_and_both_action_positions(system):
    methods, original = inputs()
    family = "v4" if system == "v4" else "v3"
    constants = original | {f"system_{family}_buyback_1_enable": True}
    method = methods[f"long_grind_adjust_trade_position_{family}"]
    flags = {f"is_system_{name}": name == system for name in ("v3", "v3_1", "v3_2", "v4")}
    options = {
        "side": "long",
        "system_prefix": f"system_{family}",
        "derisk_prefix": "system_v4" if family == "v4" else "system_v3_2",
        "source_switches": {"derisk_4_enable": False} if family == "v4" else None,
    }
    with pytest.raises(StrategyAnalysisError, match="buyback route"):
        _build_adjustment_constants(constants, method, **options)
    descriptor = _build_adjustment_constants(constants, method, allow_buyback=True, **options)
    program = compile_system_adjustment_ir(
        method,
        methods[f"long_grind_exit_{family}"],
        constants,
        side="long",
        retry_policy=descriptor["policy"],
        static_inputs=flags,
        constant_prefix=f"system_{family}_",
    )
    actions = [
        action for action in program["source_order"] if action["kind"].startswith("auxiliary-")
    ]
    assert [action["tag"] for action in actions] == ["buyback_1_entry", "buyback_1_derisk"] + (
        ["rebuy_entry"] if system == "v3_1" else []
    )
    assert actions[1]["append_entry_ids"] is True
    group = program["order_scan"]["counted_entry_groups"][0]
    assert group["fallback_exit_tag"] == "derisk_level_4"
    assert group["exit_action_tag"] == "buyback_1_derisk"
