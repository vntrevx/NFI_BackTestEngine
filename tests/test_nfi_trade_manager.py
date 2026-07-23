from __future__ import annotations

import ast

from nfi_backtest_engine.nfi_trade_manager import (
    _extract_rebuy_terminal_exit,
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
