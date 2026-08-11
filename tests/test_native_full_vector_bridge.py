from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import _rust
from nfi_backtest_engine.indicator_program import compile_indicator_program
from nfi_backtest_engine.signal_program import compile_signal_program
from nfi_backtest_engine.tag_program import compile_tag_program


def _write_strategy(path: Path) -> None:
    path.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class BridgeStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        informative = self.dp.get_pair_dataframe(\n"
        "            pair=metadata['pair'], timeframe='15m'\n"
        "        )\n"
        "        dataframe = merge_informative_pair(\n"
        "            dataframe, informative, self.timeframe, '15m', ffill=False\n"
        "        )\n"
        "        dataframe['score'] = dataframe['close'] - dataframe['open']\n"
        "        dataframe['exit_mask'] = dataframe['close_15m']\n"
        "        return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['enter_long', 'enter_short']] = 0\n"
        "        dataframe.loc[dataframe['score'] > 0, 'enter_long'] = 1\n"
        "        dataframe.loc[dataframe['score'] > 0, 'enter_tag'] = '101  '\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['exit_long', 'exit_short']] = 0\n"
        "        dataframe.loc[dataframe['exit_mask'] > 0, 'exit_long'] = 1\n"
        "        dataframe.loc[dataframe['exit_mask'] > 0, 'exit_tag'] = 'done  '\n"
        "        return dataframe\n",
        encoding="utf-8",
    )


def _programs(path: Path) -> tuple[str, str, str]:
    programs = (
        compile_indicator_program(path, class_name="BridgeStrategy"),
        compile_signal_program(path, class_name="BridgeStrategy"),
        compile_tag_program(path, class_name="BridgeStrategy"),
    )
    return tuple(json.dumps(program, separators=(",", ":")) for program in programs)  # type: ignore[return-value]


def _execute(programs: tuple[str, str, str]) -> Mapping[str, Any]:
    indicator, signal, tag = programs
    return _rust.execute_full_vector(
        indicator,
        signal,
        tag,
        "ETH/USDT",
        "5m",
        [0, 300_000, 600_000, 900_000],
        {
            "open": [2.0, 2.0, 2.0, 2.0],
            "close": [1.0, 3.0, 4.0, 1.0],
            "raw_nan": [None, float("nan"), -0.0, 4.0],
        },
        [
            (
                "ETH/USDT",
                "15m",
                [-600_000, 0],
                {"close": [None, float("nan")]},
            )
        ],
        {"pair": "ETH/USDT"},
        ["open", "close", "raw_nan", "close_15m"],
        1,
    )


def _reseal(program: dict[str, Any]) -> str:
    identity = copy.deepcopy(program)
    identity.pop("fingerprint")
    identity["source"].pop("path")
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    program["fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return json.dumps(program, separators=(",", ":"))


def _write_numeric_mutation_strategy(path: Path) -> None:
    path.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NumericMutationStrategy(IStrategy):\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['enter_long', 'enter_short']] = 0\n"
        "        dataframe.loc[dataframe['score'] > 0, 'enter_long'] = 1\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, ['exit_long', 'exit_short']] = 0\n"
        "        return dataframe\n",
        encoding="utf-8",
    )


def test_full_vector_bridge_runs_in_memory_with_independent_frame_lengths(
    tmp_path: Path,
) -> None:
    strategy = tmp_path / "strategy.py"
    _write_strategy(strategy)
    result = _execute(_programs(strategy))

    assert result["pair"] == "ETH/USDT"
    assert result["timeframe"] == "5m"
    assert result["execution_start_index"] == 1
    assert result["timestamps_ms"] == [0, 300_000, 600_000, 900_000]
    assert result["columns"]["date"]["value_type"] == "Timestamp(Millisecond)"
    raw_nan = result["columns"]["raw_nan"]["values"]
    assert raw_nan[0] is None
    assert math.isnan(raw_nan[1])
    assert math.copysign(1.0, raw_nan[2]) == -1.0
    informative = result["columns"]["close_15m"]["values"]
    assert informative[0] is None
    assert math.isnan(informative[1])
    assert math.isnan(informative[2])
    assert result["columns"]["nfi_exec_enter_tag"]["values"] == [
        None,
        "",
        "101  ",
        "101  ",
    ]
    assert result["enabled_indexes"]["enter_long"] == [2, 3]


def test_full_vector_bridge_rejects_signal_tag_surface_drift(tmp_path: Path) -> None:
    strategy = tmp_path / "strategy.py"
    _write_strategy(strategy)
    indicator, signal, encoded_tag = _programs(strategy)
    tag = json.loads(encoded_tag)
    enter_one = next(
        node
        for node in tag["nodes"]
        if node["op"] == "literal"
        and node["parameters"].get("value") == 1
        and node["function"] == "f1"
    )
    enter_one["parameters"]["value"] = 0

    with pytest.raises(ValueError, match="Signal and Tag programs disagree on enter_long"):
        _execute((indicator, signal, _reseal(tag)))


def test_numeric_mutation_bridge_executes_compiled_program_without_strategy_python(
    tmp_path: Path,
) -> None:
    strategy = tmp_path / "numeric_mutation.py"
    _write_numeric_mutation_strategy(strategy)
    program = compile_signal_program(strategy, class_name="NumericMutationStrategy")

    result = _rust.execute_numeric_mutation_program(
        json.dumps(program, separators=(",", ":")),
        {"score": [None, -0.0, 1.0]},
        {"pair": "ETH/USDT"},
        ["enter_long", "enter_short", "exit_long", "exit_short"],
    )

    assert result["enter_long"] == {
        "value_type": "Int64",
        "values": [0, 0, 1],
    }
    assert result["enter_short"]["values"] == [0, 0, 0]
    assert result["exit_long"]["values"] == [0, 0, 0]
    assert result["exit_short"]["values"] == [0, 0, 0]
