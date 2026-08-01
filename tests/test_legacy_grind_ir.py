from __future__ import annotations

import ast
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.legacy_grind_ir import compile_legacy_grind_base_ir
from nfi_backtest_engine.x7.trade_manager import build_nfi_trade_manager_ir

_SOURCE = Path(
    "benchmarks/fixtures/captured/"
    "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20/inputs/strategy.py"
)


def _method(*, retry_minutes: int = 10) -> ast.FunctionDef:
    node = ast.parse(
        f"""
def route(self, current_time):
    grind_entry_retry_time = current_time - timedelta(minutes={retry_minutes})
    grind_order_age_time = current_time - timedelta(hours=6)
    grind_force_order_age_time = current_time - timedelta(hours=24)
    is_derisk = trade_amount < first_filled_entry.safe_filled * 0.95
    slice_profit_lt_neg_gate = slice_profit < -0.06
    for order in reversed(filled_orders):
        order_side = order.ft_order_side
        if order_side == "buy" and order is not filled_orders[0]:
            if order_tag == "lane-b":
                second_count += 1
            elif order_tag not in [
                "post-a", "post-a-stop", "lane-b", "lane-b-stop",
                "lane-c", "lane-c-stop", "recover", "recover-stop",
            ]:
                first_count += 1
        elif order_side == "sell":
            if order_tag == "risk-one":
                is_derisk_1 = True
            elif order_tag in ["partial_exit", "force_exit", ""]:
                first_closed = True
            elif order_tag not in [
                "post-a", "post-a-stop", "lane-b", "lane-b-stop",
                "lane-c", "lane-c-stop", "recover", "recover-stop",
            ]:
                first_closed = True
    for order in filled_orders:
        if order.ft_order_side == "sell" and order_tag in ["recover", "recover-stop"]:
            first_entry_closed = True
    if first_entry_distance > first_entry_profit:
        sell_amount = min_stake * 1.55
        order_tag = "recover"
    if should_open_first:
        buy_amount = min_stake * 1.5
        order_tag = "lane-a"
    if should_open_second:
        order_tag = "lane-b"
"""
    ).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _constants() -> dict[str, object]:
    return {
        "first_entry_profit_threshold_spot": 0.018,
        "clusters": [
            {"entry_tag": "lane-a", "stop_tag": "lane-a-stop", "post_derisk": False},
            {"entry_tag": "lane-b", "stop_tag": "lane-b-stop", "post_derisk": False},
            {"entry_tag": "lane-c", "stop_tag": "lane-c-stop", "post_derisk": False},
            {"entry_tag": "post-a", "stop_tag": "post-a-stop", "post_derisk": True},
        ],
    }


def test_legacy_grind_base_ir_uses_source_tags_and_policy_as_data() -> None:
    program = compile_legacy_grind_base_ir(_method(), _constants())

    assert program["schema_version"] == "grind-transition-program-v1"
    assert [transition["kind"] for transition in program["source_order"]] == [
        "first-entry-profit",
        "cluster",
        "cluster",
    ]
    assert program["source_order"][0]["tag"] == "recover"
    assert [transition["entry_tag"] for transition in program["source_order"][1:]] == [
        "lane-a",
        "lane-b",
    ]
    assert program["order_scan"]["entry_order_side"] == "buy"
    assert program["order_scan"]["exit_order_side"] == "sell"
    assert program["order_scan"]["derisk_entry_tag"] == "risk-one"
    assert program["policy"] == {
        "entry_retry_ms": 600_000,
        "order_age_ms": 21_600_000,
        "force_order_age_ms": 86_400_000,
        "forced_entry_loss_gate": -0.06,
        "minimum_entry_multiplier": 1.5,
        "minimum_remaining_multiplier": 1.55,
        "derisk_amount_ratio": 0.95,
    }


def test_legacy_grind_base_ir_fingerprint_tracks_source_policy_changes() -> None:
    base = compile_legacy_grind_base_ir(_method(), _constants())
    changed = compile_legacy_grind_base_ir(_method(retry_minutes=12), _constants())

    assert changed["policy"]["entry_retry_ms"] == 720_000
    assert changed["fingerprint"] != base["fingerprint"]


def test_legacy_grind_base_ir_rejects_a_non_reversed_order_scan() -> None:
    method = _method()
    loop = next(node for node in method.body if isinstance(node, ast.For))
    loop.iter = ast.Name(id="filled_orders", ctx=ast.Load())

    with pytest.raises(StrategyAnalysisError, match="reverse filled-order scan changed"):
        compile_legacy_grind_base_ir(method, _constants())


def test_trade_manager_publishes_the_source_compiled_legacy_grind_prefix() -> None:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))

    assert manager is not None
    operation = manager["operation"]
    program = operation["supported_routes"]["long_grind"]["program"]
    assert operation["schema_version"] == "0.25.0"
    assert program["schema_version"] == "grind-transition-program-v1"
    transition_tags = [
        transition.get("tag", transition.get("entry_tag"))
        for transition in program["source_order"]
    ]
    assert transition_tags == [
        "gm0",
        "gd1",
        "gd2",
    ]
    assert program["order_scan"]["known_clusters"][0]["post_derisk"] is False
    assert program["order_scan"]["known_clusters"][-1]["post_derisk"] is True
    assert manager["proof"]["legacy_grind_ir_fingerprint"] == program["fingerprint"]
