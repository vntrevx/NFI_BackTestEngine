from __future__ import annotations

import ast

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.nfi_trade_manager import (
    _adjustment_literal_policy,
    _extract_rebuy_terminal_exit,
    _legacy_futures_fallback_loss_threshold,
    _method_ast_sha256,
)


def _method(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_rebuy_terminal_exit_extracts_source_policy_and_preserves_base_ast() -> None:
    base = _method(
        """
def long_exit_rebuy(self, enter_tags, current_time, trade, profit_init_ratio):
    if profit_init_ratio >= 0.5:
        return True, "ordinary_exit"
    return False, None
"""
    )
    extended = _method(
        """
def long_exit_rebuy(self, enter_tags, current_time, trade, profit_init_ratio):
    if profit_init_ratio >= 0.5:
        return True, "ordinary_exit"
    if (
        enter_tags == ["65"]
        and (current_time - trade.open_date_utc).total_seconds() >= 90 * 60
        and profit_init_ratio >= 0.0125
    ):
        return True, "exit_long_rebuy_signal65_early_recovery"
    return False, None
"""
    )

    policy, statement_index = _extract_rebuy_terminal_exit(extended)

    assert policy == {
        "entry_tags": ["65"],
        "minimum_age_ms": 5_400_000,
        "minimum_profit_ratio": 0.0125,
        "reason": "exit_long_rebuy_signal65_early_recovery",
    }
    assert statement_index is not None
    assert _method_ast_sha256(
        extended,
        remove_statement_index=statement_index,
    ) == _method_ast_sha256(base, remove_statement_index=None)


def test_rebuy_terminal_exit_rejects_a_different_comparison_contract() -> None:
    method = _method(
        """
def long_exit_rebuy(self, enter_tags, current_time, trade, profit_init_ratio):
    if (
        enter_tags == ["65"]
        and (current_time - trade.open_date_utc).total_seconds() > 90 * 60
        and profit_init_ratio >= 0.0125
    ):
        return True, "exit_long_rebuy_signal65_early_recovery"
    return False, None
"""
    )

    assert _extract_rebuy_terminal_exit(method) == (None, None)


def _legacy_grind_method(*, fallback_threshold: float = -0.65) -> ast.FunctionDef:
    return _method(
        f"""
def long_grind_adjust_trade_position(self):
    if (
        is_futures
        and has_order_tags
        and not partial_sell
        and slice_profit < ({fallback_threshold} / trade_leverage)
        and (is_derisk or is_derisk_calc or is_grind_mode)
        and grind_1_sub_grind_count < grind_1_max_sub_grinds
    ):
        order_tag = "gd1"
        return buy_amount, order_tag
"""
    )


def test_legacy_futures_fallback_threshold_is_compiled_from_source() -> None:
    assert _legacy_futures_fallback_loss_threshold(_legacy_grind_method()) == -0.65
    assert (
        _legacy_futures_fallback_loss_threshold(_legacy_grind_method(fallback_threshold=-0.72))
        == -0.72
    )


def test_legacy_futures_fallback_rejects_an_unscaled_threshold() -> None:
    method = _legacy_grind_method()
    branch = method.body[0]
    assert isinstance(branch, ast.If)
    comparison = next(
        node
        for node in ast.walk(branch.test)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "slice_profit"
    )
    comparison.comparators[0] = ast.Constant(value=-0.65)

    with pytest.raises(
        StrategyAnalysisError,
        match="legacy futures drawdown fallback changed",
    ):
        _legacy_futures_fallback_loss_threshold(method)


def _adjustment_method(*, late_entry_threshold: float = -0.06) -> ast.FunctionDef:
    """Build the structural subset consumed by the policy extractor.

    The production callback is intentionally not copied into the test. Keeping
    only the reachable policy shape makes this test describe the compiler
    contract instead of pinning an upstream file by path or release number.
    """

    return _method(
        f"""
def long_grind_adjust_trade_position_v3(self, current_time):
    grind_entry_retry_time = current_time - timedelta(minutes=5)
    is_long_extra_checks_entry = (
        grind_entry_retry_time > filled_entries[-1].order_filled_utc
        and (
            current_time - timedelta(hours=6) > filled_orders[-1].order_filled_utc
            or slice_profit < {late_entry_threshold}
            or is_derisk_3
        )
    )
    if self.system_v3_grind_1_enable and is_long_grind_entry:
        order_tag = "grind_1_entry"
    if self.system_v3_grind_2_enable and is_long_grind_entry:
        order_tag = "grind_2_entry"
    if self.system_v3_grind_3_enable and is_long_grind_entry:
        order_tag = "grind_3_entry"
    if (
        self.system_v3_grind_4_enable
        and (
            is_long_grind_entry
            or (
                slice_profit_entry < {late_entry_threshold}
                and last_candle["RSI_14"] < 35.0
                and last_candle["close"] < last_candle["EMA_20"] * 0.985
            )
            or (
                slice_profit_entry < {late_entry_threshold}
                and num_open_grinds_and_buybacks == 0
                and last_candle["RSI_14"] < 30.0
            )
        )
    ):
        order_tag = "grind_4_entry"
    if (
        self.system_v3_grind_5_enable
        and (
            is_long_grind_entry
            or (
                (is_derisk_1_found or is_derisk_2_found or is_derisk_3_found)
                and slice_profit_entry < {late_entry_threshold}
                and last_candle["RSI_3"] > 10.0
                and last_candle["AROONU_14"] < 50.0
            )
        )
    ):
        order_tag = "grind_5_entry"
"""
    )


def test_adjustment_policy_is_compiled_from_callback_literals() -> None:
    policy = _adjustment_literal_policy(_adjustment_method())

    assert policy["entry_retry_ms"] == 300_000
    assert policy["stale_order_ms"] == 21_600_000
    assert policy["extra_entry_profit_condition"] == {
        "left": {"kind": "variable", "name": "slice_profit"},
        "operator": "lt",
        "right": {"kind": "literal", "value": -0.06},
    }
    assert policy["extra_entry_derisk_levels"] == [3]
    assert policy["grind_entry_fallbacks"][:3] == [
        {"level": 1, "predicates": []},
        {"level": 2, "predicates": []},
        {"level": 3, "predicates": []},
    ]
    assert policy["grind_entry_fallbacks"][4]["predicates"][0] == {
        "any_derisk_levels": [1, 2, 3],
        "conditions": [
            {
                "left": {"kind": "variable", "name": "slice_profit_entry"},
                "operator": "lt",
                "right": {"kind": "literal", "value": -0.06},
            },
            {
                "left": {"kind": "feature", "name": "RSI_3", "multiplier": 1.0},
                "operator": "gt",
                "right": {"kind": "literal", "value": 10.0},
            },
            {
                "left": {
                    "kind": "feature",
                    "name": "AROONU_14",
                    "multiplier": 1.0,
                },
                "operator": "lt",
                "right": {"kind": "literal", "value": 50.0},
            },
        ],
    }


def test_adjustment_policy_threshold_changes_with_source() -> None:
    policy = _adjustment_literal_policy(_adjustment_method(late_entry_threshold=-0.07))

    assert policy["extra_entry_profit_condition"]["right"]["value"] == -0.07
    assert (
        policy["grind_entry_fallbacks"][3]["predicates"][0]["conditions"][0]["right"]["value"]
        == -0.07
    )


def test_short_adjustment_policy_preserves_mirrored_source_conditions() -> None:
    method = _method(
        """
def short_grind_adjust_trade_position_v3(self, current_time):
    grind_entry_retry_time = current_time - timedelta(minutes=5)
    is_short_extra_checks_entry = (
        grind_entry_retry_time > filled_entries[-1].order_filled_utc
        and (
            current_time - timedelta(hours=6) > filled_orders[-1].order_filled_utc
            or slice_profit > 0.06
            or is_derisk_3
        )
    )
    if self.system_v3_grind_1_enable and is_short_grind_entry:
        order_tag = "grind_1_entry"
    if self.system_v3_grind_2_enable and is_short_grind_entry:
        order_tag = "grind_2_entry"
    if self.system_v3_grind_3_enable and is_short_grind_entry:
        order_tag = "grind_3_entry"
    if (
        self.system_v3_grind_4_enable
        and (
            is_short_grind_entry
            or (
                slice_profit_entry > 0.04
                and last_candle["RSI_14"] > 65.0
                and last_candle["close"] > last_candle["EMA_20"] * 1.015
            )
        )
    ):
        order_tag = "grind_4_entry"
    if (
        self.system_v3_grind_5_enable
        and (
            is_short_grind_entry
            or (
                (is_derisk_1_found or is_derisk_2_found or is_derisk_3_found)
                and slice_profit_entry > 0.06
                and last_candle["RSI_3"] < 90.0
                and last_candle["AROOND_14"] < 50.0
            )
        )
    ):
        order_tag = "grind_5_entry"
"""
    )

    policy = _adjustment_literal_policy(method, side="short")

    assert policy["extra_entry_profit_condition"] == {
        "left": {"kind": "variable", "name": "slice_profit"},
        "operator": "gt",
        "right": {"kind": "literal", "value": 0.06},
    }
    assert policy["grind_entry_fallbacks"][4]["predicates"][0] == {
        "any_derisk_levels": [1, 2, 3],
        "conditions": [
            {
                "left": {"kind": "variable", "name": "slice_profit_entry"},
                "operator": "gt",
                "right": {"kind": "literal", "value": 0.06},
            },
            {
                "left": {"kind": "feature", "name": "RSI_3", "multiplier": 1.0},
                "operator": "lt",
                "right": {"kind": "literal", "value": 90.0},
            },
            {
                "left": {
                    "kind": "feature",
                    "name": "AROOND_14",
                    "multiplier": 1.0,
                },
                "operator": "lt",
                "right": {"kind": "literal", "value": 50.0},
            },
        ],
    }


def test_adjustment_policy_rejects_untyped_fallback_calls() -> None:
    method = _adjustment_method()
    grind_five = next(
        node
        for node in method.body
        if isinstance(node, ast.If)
        and any(
            isinstance(value, ast.Constant) and value.value == "grind_5_entry"
            for value in ast.walk(node)
        )
    )
    assert isinstance(grind_five.test, ast.BoolOp)
    signal = grind_five.test.values[1]
    assert isinstance(signal, ast.BoolOp)
    predicate = signal.values[1]
    assert isinstance(predicate, ast.BoolOp)
    predicate.values.append(ast.Call(func=ast.Name(id="unreviewed_gate"), args=[], keywords=[]))

    with pytest.raises(
        StrategyAnalysisError,
        match="grind 5 fallback condition changed",
    ):
        _adjustment_literal_policy(method)
