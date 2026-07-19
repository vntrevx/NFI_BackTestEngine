from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine.strategy_ir import analyze_strategy
from nfi_backtest_engine.trade_ir import (
    build_trade_dependency_ir,
    summarize_trade_dependency_ir,
)


def test_scalar_exit_decision_is_compiled_into_compact_arena(tmp_path: Path) -> None:
    source = tmp_path / "Scalar.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "import numpy as np\n"
        "class Scalar(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.exit_dec('normal', current_profit, kwargs['last'])\n"
        "    def exit_dec(self, mode, current_profit, last_candle):\n"
        "        last_rsi = last_candle['RSI_14']\n"
        "        if 0.01 > current_profit >= 0.001:\n"
        "            if isinstance(last_rsi, np.float64) and last_rsi > 80.0:\n"
        "                return True, f'exit_{mode}_0_1'\n"
        "        return False, None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Scalar"))

    assert report["roots"]["custom_exit"]["methods"] == ["custom_exit", "exit_dec"]
    compiled = report["compiled_scalar_methods"]["exit_dec"]["program"]
    assert compiled["opcode"] == "scalar-decision-program-v1"
    assert compiled["parameters"] == ["mode", "current_profit", "last_candle"]
    assert any(expression[0] == "is-instance" for expression in compiled["expressions"])
    contract = report["compiled_scalar_methods"]["exit_dec"]["input_contract"]
    assert contract["indexed_fields"] == {"last_candle": ["RSI_14"]}
    assert contract["numeric_thresholds"]["current_profit"] == [0.001, 0.01]
    assert report["compiled_scalar_methods"]["custom_exit"]["called_methods"] == ["exit_dec"]
    summary = summarize_trade_dependency_ir(report)
    assert "program" not in summary["compiled_scalar_methods"]["exit_dec"]
    assert len(summary["compiled_scalar_methods"]["exit_dec"]["program_sha256"]) == 64


def test_large_elif_table_is_flattened_below_json_recursion_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ElifTable.py"
    branches = [
        (
            "        if score < 0:\n            return False, None\n"
            if index == 0
            else (f"        elif score < {index}:\n            return True, 'reason_{index}'\n")
        )
        for index in range(151)
    ]
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class ElifTable(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.decide(current_profit)\n"
        "    def decide(self, score):\n" + "".join(branches) + "        return False, None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="ElifTable"))
    program = report["compiled_scalar_methods"]["decide"]["program"]
    chain = program["statements"][0]

    assert chain[0] == "if-chain"
    assert len(chain[1]) == 151
    # The standard encoder succeeds without any unbounded-depth option.
    assert json.loads(json.dumps(program))["statements"][0][0] == "if-chain"


def test_stateful_or_looping_method_fails_closed_with_location(tmp_path: Path) -> None:
    source = tmp_path / "Stateful.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Stateful(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def adjust_trade_position(self, trade, current_time, current_rate, "
        "current_profit, min_stake, max_stake, **kwargs):\n"
        "        for order in trade.orders:\n"
        "            trade.set_custom_data(key='seen', value=order.id)\n"
        "        return None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Stateful"))
    failure = report["stateful_methods"]["adjust_trade_position"]

    assert report["compiled_scalar_methods"] == {}
    assert failure["node"] == "For"
    assert failure["line"] == 5
    assert "not scalar-pure" in failure["message"]


def test_self_constants_are_frozen_inside_scalar_program(tmp_path: Path) -> None:
    source = tmp_path / "Constants.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Constants(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    threshold = 0.2\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        if current_profit > self.threshold:\n"
        "            return 'done'\n"
        "        return None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Constants"))
    expressions = report["compiled_scalar_methods"]["custom_exit"]["program"]["expressions"]

    assert ["literal", 0.2] in expressions


def test_observability_only_grind_tag_write_is_lowered_as_ephemeral(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Ephemeral.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Ephemeral(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.long_grind_entry_v3(current_profit)\n"
        "    def long_grind_entry_v3(self, current_profit):\n"
        "        if current_profit > 0:\n"
        "            self._grind_entry_tag = 'g1'\n"
        "            return True\n"
        "        self._grind_entry_tag = ''\n"
        "        return False\n"
        "    def report(self):\n"
        "        log.info(f'{self._grind_entry_tag}')\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Ephemeral"))
    compiled = report["compiled_scalar_methods"]["long_grind_entry_v3"]

    assert compiled["elided_observability_writes"] == ["_grind_entry_tag"]
    assert "ephemeral-set" in json.dumps(compiled["program"]["statements"])


def test_grind_tag_write_is_not_elided_when_read_semantically(tmp_path: Path) -> None:
    source = tmp_path / "Semantic.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Semantic(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.long_grind_entry_v3(current_profit)\n"
        "    def long_grind_entry_v3(self, current_profit):\n"
        "        self._grind_entry_tag = 'g1'\n"
        "        return current_profit > 0\n"
        "    def semantic_read(self):\n"
        "        return self._grind_entry_tag == 'g1'\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Semantic"))

    assert "long_grind_entry_v3" not in report["compiled_scalar_methods"]
    assert report["stateful_methods"]["long_grind_entry_v3"]["node"] == "Assign"


def test_scalar_method_aliases_compile_as_a_transitive_program_bundle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Bundle.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Bundle(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        decide = self.decide\n"
        "        return decide('normal', current_profit)\n"
        "    def decide(self, mode, current_profit):\n"
        "        if current_profit > 0.1:\n"
        "            return True, f'exit_{mode}'\n"
        "        return False, None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="Bundle"))
    compiled = report["compiled_scalar_methods"]

    assert set(compiled) == {"custom_exit", "decide"}
    assert compiled["custom_exit"]["called_methods"] == ["decide"]
    assert ["pass"] in compiled["custom_exit"]["program"]["statements"]
    assert any(
        expression[:2] == ["call-program", "decide"]
        for expression in compiled["custom_exit"]["program"]["expressions"]
    )


def test_scalar_bundle_fails_closed_when_a_called_method_is_stateful(
    tmp_path: Path,
) -> None:
    source = tmp_path / "BundleState.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class BundleState(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        return self.decide(trade)\n"
        "    def decide(self, trade):\n"
        "        for order in trade.orders:\n"
        "            return order.id\n"
        "        return None\n",
        encoding="utf-8",
    )

    report = build_trade_dependency_ir(analyze_strategy(source, class_name="BundleState"))

    assert report["compiled_scalar_methods"] == {}
    failure = report["stateful_methods"]["custom_exit"]
    assert failure["node"] == "Call"
    assert "decide" in failure["message"]
