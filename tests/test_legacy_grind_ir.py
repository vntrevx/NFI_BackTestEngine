from __future__ import annotations

import ast
from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import build_trade_dependency_ir
from nfi_backtest_engine.x7.legacy import _route_exit_profit_threshold
from nfi_backtest_engine.x7.legacy_grind_ir import compile_legacy_grind_ir
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
    if first_entry_distance < first_entry_stop:
        order_tag = "recover-stop"
    if should_open_post:
        order_tag = "post-a"
    if should_close_post:
        order_tag = "post-a"
    if should_stop_post:
        order_tag = "post-a-stop"
    if should_open_first:
        buy_amount = min_stake * 1.5
        order_tag = "lane-a"
    if (
        is_futures and has_order_tags and not partial_sell
        and (is_derisk or is_derisk_calc or is_grind_mode)
        and grind_1_sub_grind_count < grind_1_max_sub_grinds
        and slice_profit < -0.65 / trade_leverage
    ):
        order_tag = "lane-a"
    if should_close_first:
        order_tag = "lane-a"
    if should_stop_first:
        order_tag = "lane-a-stop"
    if should_open_second:
        order_tag = "lane-b"
    if should_close_second:
        order_tag = "lane-b"
    if should_stop_second:
        order_tag = "lane-b-stop"
    if should_open_third:
        order_tag = "lane-c"
    if should_close_third:
        order_tag = "lane-c"
    if should_stop_third:
        order_tag = "lane-c-stop"
    if (
        is_derisk_1 and not derisk_1_reentry_found and derisk_1_order is not None
        and distance < (
            self.regular_mode_derisk_1_reentry_futures
            if is_futures else self.regular_mode_derisk_1_reentry_spot
        )
    ):
        if (
            grind_entry_retry_time > last_filled_entry.order_filled_utc
            and grind_force_order_age_time > last_filled_order.order_filled_utc
            and grind_order_age_time > last_filled_order.order_filled_utc
            and last_candle["guard-b"] == True
            and last_candle["guard-a"] == True
            and is_long_grind_entry
        ):
            buy_amount = derisk_1_order.safe_filled * derisk_1_order.safe_price
            if buy_amount > max_stake:
                return None
            order_tag = "risk-one"
    if (
        derisk_1_reentry_found and derisk_1_reentry_order is not None
        and distance < (
            (self.regular_mode_derisk_1_reentry_futures
             if is_futures else self.regular_mode_derisk_1_reentry_spot)
            / stake_scale_leverage
        )
    ):
        sell_amount = derisk_1_reentry_order.safe_filled * exit_rate / trade_leverage
        return -sell_amount, "risk-one"
"""
    ).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _constants() -> dict[str, object]:
    return {
        "first_entry_profit_threshold_spot": 0.018,
        "first_entry_stop_threshold_spot": -0.2,
        "derisk_1_reentry_futures": -0.08,
        "derisk_1_reentry_spot": -0.07,
        "clusters": [
            {"entry_tag": "lane-a", "stop_tag": "lane-a-stop", "post_derisk": False},
            {"entry_tag": "lane-b", "stop_tag": "lane-b-stop", "post_derisk": False},
            {"entry_tag": "lane-c", "stop_tag": "lane-c-stop", "post_derisk": False},
            {"entry_tag": "post-a", "stop_tag": "post-a-stop", "post_derisk": True},
        ],
    }


def _exit_wrapper(*, threshold: float = 0.25) -> ast.FunctionDef:
    node = ast.parse(
        f"""
def long_exit_grind(self, profit_init_ratio):
    if profit_init_ratio > {threshold!r}:
        return True, f"exit_{{self.long_grind_mode_name}}_g"
    return False, None
"""
    ).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_route_exit_profit_threshold_is_compiled_from_source() -> None:
    assert (
        _route_exit_profit_threshold(
            _exit_wrapper(),
            mode_constant="long_grind_mode_name",
        )
        == 0.25
    )
    assert (
        _route_exit_profit_threshold(
            _exit_wrapper(threshold=0.31),
            mode_constant="long_grind_mode_name",
        )
        == 0.31
    )


def test_route_exit_profit_threshold_rejects_changed_wrapper_control_flow() -> None:
    method = _exit_wrapper()
    method.body.insert(0, ast.parse("record_exit_attempt()").body[0])

    with pytest.raises(StrategyAnalysisError, match="exit wrapper changed"):
        _route_exit_profit_threshold(method, mode_constant="long_grind_mode_name")


def test_legacy_grind_ir_uses_source_tags_and_policy_as_data() -> None:
    program = compile_legacy_grind_ir(_method(), _constants())

    assert program["schema_version"] == "grind-transition-program-v3"
    assert [transition["kind"] for transition in program["source_order"]] == [
        "first-entry",
        "cluster",
        "cluster",
        "cluster",
        "cluster",
        "derisk-buyback",
    ]
    assert program["source_order"][0]["profit_tag"] == "recover"
    assert program["source_order"][0]["stop_tag"] == "recover-stop"
    assert [transition["entry_tag"] for transition in program["source_order"][1:-1]] == [
        "post-a",
        "lane-a",
        "lane-b",
        "lane-c",
    ]
    assert [
        transition["futures_fallback_loss_threshold"]
        for transition in program["source_order"][1:-1]
    ] == [None, -0.65, None, None]
    assert program["source_order"][-1] == {
        "kind": "derisk-buyback",
        "tag": "risk-one",
        "entry_threshold_futures": -0.08,
        "entry_threshold_spot": -0.07,
        "entry_feature_columns": ["guard-a", "guard-b"],
        "entry_retry_policy": "bounded-grind-policy",
        "entry_stake_basis": "derisk-exit-cost",
        "entry_minimum_multiplier": 1.5,
        "entry_wallet_guard": "return-none",
        "exit_threshold_divisor": "mode-leverage",
        "exit_stake_basis": "reentry-amount-at-current-rate",
        "exit_minimum_remaining_multiplier": 1.55,
        "location": program["source_order"][-1]["location"],
    }
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


def test_legacy_grind_ir_fingerprint_tracks_source_policy_changes() -> None:
    base = compile_legacy_grind_ir(_method(), _constants())
    changed = compile_legacy_grind_ir(_method(retry_minutes=12), _constants())

    assert changed["policy"]["entry_retry_ms"] == 720_000
    assert changed["fingerprint"] != base["fingerprint"]


def test_legacy_grind_ir_rejects_an_unbounded_derisk_buyback_retry() -> None:
    method = _method()
    retry_guard = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "grind_entry_retry_time"
    )
    retry_guard.ops = [ast.Lt()]

    with pytest.raises(StrategyAnalysisError, match="retry guard changed"):
        compile_legacy_grind_ir(method, _constants())


@pytest.mark.parametrize(
    ("target", "source_owner", "message"),
    [
        ("buy_amount", "derisk_1_order", "entry stake expression changed"),
        ("sell_amount", "derisk_1_reentry_order", "exit stake expression changed"),
    ],
)
def test_legacy_grind_ir_rejects_changed_derisk_buyback_stake_expressions(
    target: str,
    source_owner: str,
    message: str,
) -> None:
    method = _method()
    assignment = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == target
        and any(
            isinstance(child, ast.Name) and child.id == source_owner
            for child in ast.walk(node.value)
        )
    )
    assignment.value = ast.Constant(value=42.0)

    with pytest.raises(StrategyAnalysisError, match=message):
        compile_legacy_grind_ir(method, _constants())


def test_legacy_grind_ir_compiles_an_additional_source_defined_level() -> None:
    method = _method()
    reverse_loop = next(
        node
        for node in method.body
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "reversed"
    )
    for container in (
        node
        for node in ast.walk(reverse_loop)
        if isinstance(node, ast.List)
        and any(
            isinstance(item, ast.Constant) and item.value == "lane-c"
            for item in node.elts
        )
    ):
        container.elts.extend(
            [ast.Constant(value="future-lane"), ast.Constant(value="future-lane-stop")]
        )
    extra_actions = ast.parse(
        """
if should_open_future:
    order_tag = "future-lane"
if should_close_future:
    order_tag = "future-lane"
if should_stop_future:
    order_tag = "future-lane-stop"
"""
    ).body
    for statement in extra_actions:
        ast.increment_lineno(statement, 1_000)
    method.body.extend(extra_actions)
    constants = _constants()
    clusters = constants["clusters"]
    assert isinstance(clusters, list)
    clusters.append(
        {
            "entry_tag": "future-lane",
            "stop_tag": "future-lane-stop",
            "post_derisk": False,
        }
    )

    program = compile_legacy_grind_ir(method, constants)

    cluster_tags = [
        transition["entry_tag"]
        for transition in program["source_order"]
        if transition["kind"] == "cluster"
    ]
    assert cluster_tags[-1] == "future-lane"
    assert len(cluster_tags) == len(clusters)


def test_legacy_grind_ir_rejects_a_non_reversed_order_scan() -> None:
    method = _method()
    loop = next(node for node in method.body if isinstance(node, ast.For))
    loop.iter = ast.Name(id="filled_orders", ctx=ast.Load())

    with pytest.raises(StrategyAnalysisError, match="reverse filled-order scan changed"):
        compile_legacy_grind_ir(method, _constants())


def test_trade_manager_publishes_the_source_compiled_legacy_grind_prefix() -> None:
    analysis = analyze_strategy(_SOURCE, class_name="NostalgiaForInfinityX7")
    manager = build_nfi_trade_manager_ir(analysis, build_trade_dependency_ir(analysis))

    assert manager is not None
    operation = manager["operation"]
    program = operation["supported_routes"]["long_grind"]["program"]
    assert operation["schema_version"] == "0.29.0"
    assert manager["remaining_steps"] == []
    assert manager["backtest_exclusions"] == [
        {
            "code": "filled-order-partial-remainder",
            "runtime_scope": "live-only",
            "policy": "filled-orders-have-zero-remaining",
        }
    ]
    assert program["schema_version"] == "grind-transition-program-v3"
    transition_tags = [
        transition.get("profit_tag", transition.get("entry_tag", transition.get("tag")))
        for transition in program["source_order"]
    ]
    assert transition_tags == [
        "gm0",
        "dl1",
        "dl2",
        "gd1",
        "gd2",
        "gd3",
        "gd4",
        "gd5",
        "gd6",
        "d1",
    ]
    assert program["source_order"][0]["stop_tag"] == "gmd0"
    regular_program = operation["supported_routes"]["long_btc"]["regular_program"]
    assert regular_program["schema_version"] == "regular-transition-program-v1"
    regular_grinds = [
        transition
        for transition in regular_program["source_order"]
        if transition["kind"] == "grind"
    ]
    assert [transition["entry_tag"] for transition in regular_grinds] == [
        f"g{level}" for level in range(1, 7)
    ]
    assert regular_grinds[0]["futures_fallback_loss_threshold"] == -0.65
    assert regular_program["continuation"]["amount_ratio"] == 0.95
    fallback = next(
        transition
        for transition in program["source_order"]
        if transition.get("futures_fallback_loss_threshold") is not None
    )
    assert fallback["entry_tag"] == "gd1"
    assert fallback["futures_fallback_loss_threshold"] == -0.65
    assert program["order_scan"]["known_clusters"][0]["post_derisk"] is False
    assert program["order_scan"]["known_clusters"][-1]["post_derisk"] is True
    assert manager["proof"]["legacy_grind_ir_fingerprint"] == program["fingerprint"]
    assert operation["supported_routes"]["long_btc"]["program"]["schema_version"] == (
        "grind-transition-program-v3"
    )
