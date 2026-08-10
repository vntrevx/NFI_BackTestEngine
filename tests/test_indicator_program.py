from __future__ import annotations

import copy
from pathlib import Path

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.indicator_program import (
    IndicatorProgramCompileError,
    compile_indicator_program,
    validate_indicator_program,
)
from nfi_backtest_engine.specs import INDICATOR_PROGRAM_SCHEMA, validate_schema

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "benchmarks" / "reference" / "strategies" / "IndicatorProgramContract.py"


def _write_program_strategy(path: Path, *, rsi_period: int = 14) -> None:
    path.write_text(
        "import numpy as np\n"
        "import pandas as pd\n"
        "import talib.abstract as ta\n"
        "from freqtrade.strategy import IStrategy\n"
        "class ProgramStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    @staticmethod\n"
        "    def rsi(values):\n"
        f"        return ta.RSI(values, timeperiod={rsi_period})\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        close = dataframe['close']\n"
        "        dataframe['rsi'] = self.rsi(close)\n"
        "        dataframe['mean4'] = pd.Series(close).rolling(4).mean()\n"
        "        dataframe['previous'] = close.shift(1)\n"
        "        dataframe['ewm8'] = pd.Series(close).ewm(\n"
        "            span=8, adjust=False\n"
        "        ).mean()\n"
        "        dataframe['selected'] = np.where(\n"
        "            dataframe['rsi'] > 50,\n"
        "            dataframe['mean4'],\n"
        "            dataframe['previous'],\n"
        "        )\n"
        "        return dataframe\n",
        encoding="utf-8",
    )


def test_indicator_program_compiles_typed_causal_dag_and_helpers(tmp_path: Path) -> None:
    source = tmp_path / "ProgramStrategy.py"
    _write_program_strategy(source)

    program = compile_indicator_program(source, class_name="ProgramStrategy")

    validate_indicator_program(program)
    validate_schema(program, INDICATOR_PROGRAM_SCHEMA)
    assert program["entrypoint"] == "f1"
    assert [(item["id"], item["source_name"], item["kind"]) for item in program["functions"]] == [
        ("f1", "populate_indicators", "entrypoint"),
        ("f2", "rsi", "helper"),
    ]
    assert program["required_input_columns"] == ["close"]
    assert program["produced_columns"] == [
        "ewm8",
        "mean4",
        "previous",
        "rsi",
        "selected",
    ]
    assert {
        "parameter",
        "column-read",
        "column-write",
        "function-call",
        "indicator-call",
        "window",
        "shift",
        "compare",
        "select",
        "return",
    } <= set(program["opcodes"])
    assert all(node["lookback"]["causal"] for node in program["nodes"])
    rolling = next(
        node
        for node in program["nodes"]
        if node["op"] == "window" and node["parameters"]["kind"] == "rolling"
    )
    assert rolling["parameters"] == {
        "kind": "rolling",
        "reducer": "mean",
        "window": 4,
        "center": False,
        "min_periods": None,
    }
    assert rolling["lookback"] == {
        "kind": "finite",
        "candles": 3,
        "expression": None,
        "causal": True,
    }
    shift = next(node for node in program["nodes"] if node["op"] == "shift")
    assert shift["parameters"] == {"periods": 1}
    assert shift["lookback"]["candles"] == 1
    ewm = next(
        node
        for node in program["nodes"]
        if node["op"] == "window" and node["parameters"]["kind"] == "ewm"
    )
    assert ewm["lookback"]["kind"] == "recursive"
    helper_call = next(node for node in program["nodes"] if node["op"] == "function-call")
    assert helper_call["lookback"]["kind"] == "mixed"
    assert "library-defined" in helper_call["lookback"]["expression"]
    final_return = next(
        node
        for node in program["nodes"]
        if node["function"] == program["entrypoint"] and node["op"] == "return"
    )
    assert final_return["lookback"]["kind"] == "mixed"
    assert set(program["source_map"]) == {node["id"] for node in program["nodes"]}
    assert len(program["fingerprint"]) == 64


def test_indicator_program_compiles_generic_talib_multi_outputs(tmp_path: Path) -> None:
    source = tmp_path / "MultiOutputStrategy.py"
    source.write_text(
        "import talib.abstract as ta\n"
        "from freqtrade.strategy import IStrategy\n"
        "class MultiOutputStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        high = dataframe['high']\n"
        "        low = dataframe['low']\n"
        "        close = dataframe['close']\n"
        "        aroon_down, aroon_up = ta.AROON(high, low, timeperiod=14)\n"
        "        fast_k = ta.STOCHF(high, low, close, fastk_period=9)[0]\n"
        "        dataframe['aroon_down'] = aroon_down\n"
        "        dataframe['aroon_up'] = aroon_up\n"
        "        dataframe['fast_k'] = fast_k\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="MultiOutputStrategy")

    calls = [node for node in program["nodes"] if node["op"] == "indicator-call"]
    assert [(node["parameters"]["name"], node["parameters"]["output"]) for node in calls] == [
        ("AROON", "aroondown"),
        ("AROON", "aroonup"),
        ("STOCHF", "fastk"),
    ]
    assert all(node["parameters"]["family"] == "ta" for node in calls)
    assert program["required_input_columns"] == ["close", "high", "low"]
    validate_indicator_program(program)


def test_committed_indicator_program_contract_remains_schema_valid() -> None:
    program = compile_indicator_program(
        CONTRACT,
        class_name="IndicatorProgramContract",
    )

    validate_schema(program, INDICATOR_PROGRAM_SCHEMA)
    assert program["required_input_columns"] == ["close"]
    assert program["informative_nodes"] == []
    assert all(node["lookback"]["causal"] for node in program["nodes"])


def test_indicator_program_semantic_validator_rejects_reference_and_identity_mutation() -> None:
    program = compile_indicator_program(
        CONTRACT,
        class_name="IndicatorProgramContract",
    )
    invalid_reference = copy.deepcopy(program)
    target = next(node for node in invalid_reference["nodes"] if node["inputs"])
    target["inputs"][0] = "n999"

    with pytest.raises(SpecValidationError, match="non-prior input"):
        validate_indicator_program(invalid_reference)

    invalid_opcode = copy.deepcopy(program)
    invalid_opcode["nodes"][0]["op"] = "signal-102"
    with pytest.raises(SpecValidationError, match="indicator-program-v1.schema.json"):
        validate_indicator_program(invalid_opcode)

    invalid_identity = copy.deepcopy(program)
    invalid_identity["source_map"]["n1"]["line"] += 1
    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        validate_indicator_program(invalid_identity)


def test_indicator_program_identity_is_path_independent_and_source_sensitive(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "ProgramStrategy.py"
    second = tmp_path / "second" / "Renamed.py"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_program_strategy(first)
    second.write_bytes(first.read_bytes())

    first_program = compile_indicator_program(first, class_name="ProgramStrategy")
    second_program = compile_indicator_program(second, class_name="ProgramStrategy")

    assert first_program["fingerprint"] == second_program["fingerprint"]
    _write_program_strategy(second, rsi_period=15)
    mutated = compile_indicator_program(second, class_name="ProgramStrategy")
    assert mutated["fingerprint"] != first_program["fingerprint"]


def test_indicator_program_records_informative_merge_before_forward_fill(tmp_path: Path) -> None:
    source = tmp_path / "Informative.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class Informative(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata, informative):\n"
        "        dataframe = merge_informative_pair(\n"
        "            dataframe, informative, self.timeframe, '1h', ffill=False\n"
        "        )\n"
        "        dataframe = dataframe.ffill()\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="Informative")
    merge = next(node for node in program["nodes"] if node["op"] == "informative-merge")
    fill = next(node for node in program["nodes"] if node["op"] == "fill")

    assert program["informative_nodes"] == [merge["id"]]
    assert merge["parameters"] == {
        "base_timeframe": "5m",
        "informative_timeframe": "1h",
        "ffill": False,
        "append_timeframe": True,
        "date_column": "date",
        "suffix": None,
    }
    assert fill["parameters"] == {"direction": "forward"}
    assert fill["inputs"] == [merge["id"]]
    assert fill["lookback"]["kind"] == "recursive"
    assert merge["source_order"] < fill["source_order"]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("dataframe['close'].shift(-1)", "LOOKAHEAD_NEGATIVE_SHIFT"),
        ("dataframe['close'].rolling(4, center=True).mean()", "LOOKAHEAD_CENTERED_WINDOW"),
        ("dataframe['close'].bfill()", "backward fill would look ahead"),
    ],
)
def test_indicator_program_rejects_lookahead_capable_source(
    tmp_path: Path,
    expression: str,
    message: str,
) -> None:
    source = tmp_path / "Lookahead.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Lookahead(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        f"        dataframe['bad'] = {expression}\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    with pytest.raises(IndicatorProgramCompileError, match=message):
        compile_indicator_program(source, class_name="Lookahead")


def test_indicator_program_preserves_informative_merge_defaults(tmp_path: Path) -> None:
    source = tmp_path / "Defaults.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class Defaults(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata, informative):\n"
        "        dataframe = merge_informative_pair(\n"
        "            dataframe, informative, self.timeframe, '1h'\n"
        "        )\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="Defaults")
    merge = next(node for node in program["nodes"] if node["op"] == "informative-merge")

    assert merge["parameters"] == {
        "base_timeframe": "5m",
        "informative_timeframe": "1h",
        "ffill": True,
        "append_timeframe": True,
        "date_column": "date",
        "suffix": None,
    }
    assert merge["lookback"]["kind"] == "recursive"


def test_indicator_program_binds_mixed_informative_merge_arguments(tmp_path: Path) -> None:
    source = tmp_path / "MixedArguments.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class MixedArguments(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata, informative):\n"
        "        dataframe = merge_informative_pair(\n"
        "            dataframe, informative=informative, timeframe=self.timeframe,\n"
        "            timeframe_inf='1h', ffill=False, append_timeframe=False,\n"
        "            date_column='opened_at', suffix='btc',\n"
        "        )\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="MixedArguments")
    merge = next(node for node in program["nodes"] if node["op"] == "informative-merge")

    assert merge["parameters"] == {
        "base_timeframe": "5m",
        "informative_timeframe": "1h",
        "ffill": False,
        "append_timeframe": False,
        "date_column": "opened_at",
        "suffix": "btc",
    }


def test_indicator_program_normalizes_falsy_suffix_like_freqtrade(tmp_path: Path) -> None:
    source = tmp_path / "FalsySuffix.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class FalsySuffix(IStrategy):\n"
        "    def populate_indicators(self, dataframe, metadata, informative):\n"
        "        return merge_informative_pair(\n"
        "            dataframe, informative, '5m', '1h', suffix=''\n"
        "        )\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="FalsySuffix")
    merge = next(node for node in program["nodes"] if node["op"] == "informative-merge")

    assert merge["parameters"]["suffix"] is None


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', False, ffill=False)",
            "duplicate informative merge argument ffill",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', unknown=False)",
            "unknown informative merge keyword unknown",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', "
            "ffill=metadata['ffill'])",
            "dynamic indicator parameter",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', "
            "append_timeframe=True, suffix='btc')",
            "suffix conflicts with append_timeframe",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', "
            "append_timeframe=False)",
            "informative merge without an output suffix",
        ),
        (
            "merge_informative_pair(dataframe, informative, '1h', '5m', ffill=False)",
            "faster informative timeframe would create rows",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', 'nonsense')",
            "invalid informative merge timeframe 'nonsense'",
        ),
        (
            "merge_informative_pair(dataframe, informative, '5m', '1h', "
            "False, True, 'date', None, 'extra')",
            "informative merge signature",
        ),
    ],
)
def test_indicator_program_rejects_ambiguous_informative_merge_calls(
    tmp_path: Path,
    call: str,
    message: str,
) -> None:
    source = tmp_path / "Rejected.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class Rejected(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata, informative):\n"
        f"        dataframe = {call}\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    with pytest.raises(
        IndicatorProgramCompileError,
        match=rf"strategy\.py:5:\d+:.*{message}",
    ):
        compile_indicator_program(source, class_name="Rejected")


def test_indicator_program_constant_folds_static_control_without_lookahead(tmp_path: Path) -> None:
    source = tmp_path / "StaticControl.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class StaticControl(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        debug = False\n"
        "        if debug:\n"
        "            dataframe['ignored'] = dataframe['close'] * 0\n"
        "        else:\n"
        "            dataframe['previous'] = dataframe['close'].shift(1)\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    program = compile_indicator_program(source, class_name="StaticControl")

    assert program["produced_columns"] == ["previous"]
    assert all(node["lookback"]["causal"] for node in program["nodes"])


def test_indicator_program_rejects_dynamic_window_and_helper_signature(tmp_path: Path) -> None:
    source = tmp_path / "DynamicWindow.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class DynamicWindow(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    @staticmethod\n"
        "    def helper(values):\n"
        "        return values\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        window = metadata['window']\n"
        "        dataframe['bad'] = dataframe['close'].rolling(window).mean()\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    with pytest.raises(IndicatorProgramCompileError, match="dynamic indicator parameter"):
        compile_indicator_program(source, class_name="DynamicWindow")

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "dataframe['close'].rolling(window).mean()",
            "self.helper(values=dataframe['close'])",
        ),
        encoding="utf-8",
    )
    with pytest.raises(IndicatorProgramCompileError, match="helper call signature"):
        compile_indicator_program(source, class_name="DynamicWindow")


def test_indicator_program_parser_requires_an_output_contract() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "indicator-program",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--output",
            ".nfi/indicator-program.json",
        ]
    )

    assert args.strategy_command == "indicator-program"
    assert args.output == Path(".nfi/indicator-program.json")
