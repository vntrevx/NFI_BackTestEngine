from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.callback_execution_contract import (
    CALLBACK_EXECUTION_IR_VERSION,
    compile_callback_execution_ir,
)
from nfi_backtest_engine.callback_source_ir import compile_callback_source_ir
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import analyze_strategy


def _write_callbacks(path: Path, *, mutation: str = "") -> None:
    source = '''from freqtrade.strategy import IStrategy
class CallbackMatrix(IStrategy):
    timeframe = "5m"
    position_adjustment_enable = True
    use_custom_stoploss = True

    def bot_loop_start(self, current_time, **kwargs):
        self.loop_seen = current_time

    def custom_stake_amount(self, pair, current_time, current_rate,
                            proposed_stake, min_stake, max_stake, leverage,
                            entry_tag, side, **kwargs):
        return proposed_stake

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs):
        return proposed_leverage

    def confirm_trade_entry(self, pair, order_type, amount, rate,
                            time_in_force, current_time, entry_tag, side,
                            **kwargs):
        return amount > 0

    def order_filled(self, pair, trade, order, current_time, **kwargs):
        trade.set_custom_data("filled", True)

    def first_exit(self, trade, current_profit):
        return "profit" if current_profit > 0.1 else None

    def second_exit(self, trade, current_profit):
        return "loss" if current_profit < -0.2 else None

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        checks = (self.first_exit, self.second_exit)
        for check in checks:
            reason = check(trade, current_profit)
            if reason:
                trade.set_custom_data("exit_seen", reason)
                return reason
        return None

    def custom_stoploss(self, pair, trade, current_time, current_rate,
                        current_profit, after_fill, **kwargs):
        return -0.05 if current_profit < -0.1 else None

    def confirm_trade_exit(self, pair, trade, order_type, amount, rate,
                           time_in_force, exit_reason, current_time, **kwargs):
        return exit_reason != "blocked"

    def adjust_trade_position(self, trade, current_time, current_rate,
                              current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate,
                              current_entry_profit, current_exit_profit,
                              **kwargs):
        return (min_stake, "rebuy") if current_profit < -0.05 else None
'''
    path.write_text(source.replace("amount > 0", mutation or "amount > 0"), encoding="utf-8")


def test_existing_callback_source_v1_identity_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "Callbacks.py"
    _write_callbacks(source)

    document = compile_callback_source_ir(source, class_name="CallbackMatrix")
    canonical = json.dumps(
        {key: value for key, value in document.items() if key != "source"},
        sort_keys=True,
        separators=(",", ":"),
    )

    assert document["schema_version"] == "callback-source-ir-v1"
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "e27c5091e8a177cc3bab6e78836c8800051028e336ef3252c0c3f9a4753524e1"
    )


def test_callback_execution_ir_covers_order_returns_visibility_and_rollback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Callbacks.py"
    _write_callbacks(source)
    analysis = analyze_strategy(source, class_name="CallbackMatrix")

    contract = compile_callback_execution_ir(
        analysis,
        trading_mode="futures",
        run_mode="backtest",
    )
    callbacks = {item["name"]: item for item in contract["callbacks"]}

    assert CALLBACK_EXECUTION_IR_VERSION == "callback-execution-ir-v1"
    assert contract["freqtrade_contract"] == {
        "version": "2026.5.1",
        "fingerprint": "7c26cbaea6853a20b93932dbc0f3bc788cf0d43e58f243e9985029a727d6ec7f",
    }
    assert [item["name"] for item in contract["callbacks"]] == [
        "bot_loop_start",
        "custom_stake_amount",
        "leverage",
        "confirm_trade_entry",
        "order_filled",
        "custom_exit",
        "custom_stoploss",
        "confirm_trade_exit",
        "adjust_trade_position",
    ]
    assert callbacks["leverage"]["order"]["before"] == ["custom_stake_amount"]
    assert callbacks["custom_stake_amount"]["order"] == {
        "phase": 4,
        "after": ["leverage"],
        "before": ["confirm_trade_entry"],
    }
    assert callbacks["custom_stoploss"]["order"]["before"] == ["custom_exit"]
    assert callbacks["adjust_trade_position"]["order"]["before"] == ["custom_stoploss"]
    assert callbacks["adjust_trade_position"]["visibility"]["signal_row_offset"] == -1
    assert callbacks["adjust_trade_position"]["visibility"][
        "callback_dataframe_completed_candle_lag"
    ] == 2
    assert callbacks["bot_loop_start"]["visibility"]["startup_executable"] is False
    assert callbacks["custom_exit"]["accepted_returns"] == ["exit-reason", "true", "null"]
    assert callbacks["custom_stoploss"]["accepted_returns"] == ["ratio", "null"]
    assert callbacks["confirm_trade_entry"]["exception"]["fallback"] == "accept"
    assert callbacks["adjust_trade_position"]["exception"] == {
        "fallback": "null-with-empty-tag",
        "ordinary_trade_deltas": "rollback-deepcopy",
        "custom_state_deltas": "persist-shared-storage",
        "scheduler_deltas_before_callback": "preserve",
    }
    assert callbacks["order_filled"]["custom_state_deltas"] == [
        {
            "operation": "set",
            "key": "filled",
            "producer_method": "order_filled",
            "predicate_ids": [],
            "source_order": 0,
        }
    ]
    exit_delta = callbacks["custom_exit"]["custom_state_deltas"][0]
    assert exit_delta["key"] == "exit_seen"
    assert exit_delta["predicate_ids"]
    assert callbacks["custom_exit"]["reachable_methods"] == [
        "custom_exit",
        "first_exit",
        "second_exit",
    ]


def test_execution_contract_is_deterministic_and_predicate_bound(tmp_path: Path) -> None:
    original = tmp_path / "original.py"
    changed = tmp_path / "changed.py"
    _write_callbacks(original)
    _write_callbacks(changed, mutation="amount >= 0")

    first = compile_callback_execution_ir(
        analyze_strategy(original, class_name="CallbackMatrix"),
        trading_mode="futures",
        run_mode="backtest",
    )
    repeated = compile_callback_execution_ir(
        analyze_strategy(original, class_name="CallbackMatrix"),
        trading_mode="futures",
        run_mode="backtest",
    )
    mutation = compile_callback_execution_ir(
        analyze_strategy(changed, class_name="CallbackMatrix"),
        trading_mode="futures",
        run_mode="backtest",
    )

    assert first == repeated
    assert first["fingerprint"] != mutation["fingerprint"]
    assert first["callbacks"][3]["source_predicates"] != mutation["callbacks"][3][
        "source_predicates"
    ]


def test_execution_contract_rejects_stale_analysis_and_unknown_callback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Callbacks.py"
    _write_callbacks(source)
    analysis = analyze_strategy(source, class_name="CallbackMatrix")
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(StrategyAnalysisError, match="source hash differs"):
        compile_callback_execution_ir(
            analysis,
            trading_mode="futures",
            run_mode="backtest",
        )

    unknown = tmp_path / "Unknown.py"
    unknown.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Unknown(IStrategy):\n"
        "    def custom_roi(self, pair, trade, current_time, trade_duration, "
        "entry_tag, side, **kwargs):\n"
        "        return 0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(StrategyAnalysisError, match="no pinned execution contract"):
        compile_callback_execution_ir(
            analyze_strategy(unknown, class_name="Unknown"),
            trading_mode="spot",
            run_mode="backtest",
        )
