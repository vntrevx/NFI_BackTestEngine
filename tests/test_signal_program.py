from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.signal_program import (
    SignalProgramCompileError,
    SignalProgramExecutionError,
    compile_signal_program,
    execute_signal_program,
    validate_signal_program,
)
from nfi_backtest_engine.specs import SIGNAL_PROGRAM_SCHEMA, validate_schema

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "benchmarks" / "reference" / "strategies" / "SignalProgramContract.py"


def _source_result(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy(deep=True)
    result.loc[:, ["enter_long", "enter_short"]] = (0, 0)
    positive = result["score"] > 0
    result.loc[positive, "enter_long"] = 1
    result.loc[result["score"] >= 2, "enter_long"] = 0
    result["enter_short"] = (result["score"] < 0).astype(int)
    result.loc[:, ["exit_long", "exit_short"]] = 0
    result.loc[(result["enter_long"] == 0) & (result["score"] > 1), "exit_long"] = 1
    result.loc[result["exit_mask"], "exit_short"] = 1
    return result


def test_signal_program_compiles_ordered_entry_and_exit_mutations() -> None:
    program = compile_signal_program(CONTRACT, class_name="SignalProgramContract")

    validate_schema(program, SIGNAL_PROGRAM_SCHEMA)
    validate_signal_program(program)
    assert program["entrypoints"] == [
        {"phase": "entry", "function": "f1"},
        {"phase": "exit", "function": "f2"},
    ]
    assert [item["column"] for item in program["signal_outputs"]] == [
        "enter_long",
        "enter_short",
        "exit_long",
        "exit_short",
    ]
    writes = [node for node in program["nodes"] if node["op"] == "frame-write"]
    assert program["mutation_nodes"] == [node["id"] for node in writes]
    assert [node["parameters"]["columns"] for node in writes] == [
        ["enter_long", "enter_short"],
        ["enter_long"],
        ["enter_long"],
        ["enter_short"],
        ["exit_long", "exit_short"],
        ["exit_long"],
        ["exit_short"],
    ]
    assert [node["parameters"]["rows"] for node in writes] == [
        "all",
        "mask",
        "mask",
        "all",
        "all",
        "mask",
        "mask",
    ]
    assert program["required_input_columns"] == ["exit_mask", "score"]
    assert len(program["fingerprint"]) == 64


def test_signal_program_executes_nullable_mask_and_source_order_exactly() -> None:
    frame = pd.DataFrame(
        {
            "score": [-2.0, -0.5, 0.5, 1.5, 2.5],
            "exit_mask": pd.array([pd.NA, False, True, True, False], dtype="boolean"),
        }
    )
    program = compile_signal_program(
        CONTRACT,
        class_name="SignalProgramContract",
        trading_mode="futures",
    )

    actual = execute_signal_program(program, frame, metadata={"pair": "ETH/USDT"})
    expected = _source_result(frame)

    pd.testing.assert_frame_equal(actual, expected, check_dtype=True, check_exact=True)
    assert actual["enter_long"].tolist() == [0, 0, 1, 1, 0]
    assert actual["enter_short"].tolist() == [1, 1, 0, 0, 0]
    assert actual["exit_long"].tolist() == [0, 0, 0, 0, 1]
    assert actual["exit_short"].tolist() == [0, 0, 1, 1, 0]


def test_signal_program_identity_rejects_order_or_source_map_mutation(tmp_path: Path) -> None:
    copied = tmp_path / "Renamed.py"
    copied.write_bytes(CONTRACT.read_bytes())
    first = compile_signal_program(CONTRACT, class_name="SignalProgramContract")
    second = compile_signal_program(copied, class_name="SignalProgramContract")
    assert first["fingerprint"] == second["fingerprint"]

    reordered = copy.deepcopy(first)
    reordered["mutation_nodes"][:2] = reversed(reordered["mutation_nodes"][:2])
    with pytest.raises(SpecValidationError, match="mutation inventory differs"):
        validate_signal_program(reordered)

    changed_location = copy.deepcopy(first)
    changed_location["source_map"]["n1"]["line"] += 1
    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        validate_signal_program(changed_location)

    invalid_write = copy.deepcopy(first)
    write = next(node for node in invalid_write["nodes"] if node["op"] == "frame-write")
    write["parameters"]["rows"] = "unordered"
    with pytest.raises(SpecValidationError, match="frame-write contract is invalid"):
        validate_signal_program(invalid_write)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("dataframe.loc[:, 'enter_tag'] = '1 '", "tag mutation"),
        ("dataframe.loc[:, 'feature'] = 1", "non-signal dataframe output"),
        ("dataframe.loc[:, 'exit_long'] = 1", "during the entry phase"),
        ("dataframe.iloc[:, 0] = 1", "nested dataframe write"),
    ],
)
def test_signal_program_fails_closed_outside_m21_signal_surface(
    tmp_path: Path,
    statement: str,
    message: str,
) -> None:
    source = tmp_path / "Unsupported.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Unsupported(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        f"        {statement}\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_long'] = 0\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    with pytest.raises(SignalProgramCompileError, match=message):
        compile_signal_program(source, class_name="Unsupported")


def test_signal_program_runtime_fails_closed_for_numeric_mask(tmp_path: Path) -> None:
    source = tmp_path / "NumericMask.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NumericMask(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[dataframe['mask'], 'enter_long'] = 1\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_long'] = 0\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    program = compile_signal_program(source, class_name="NumericMask")

    with pytest.raises(SignalProgramExecutionError, match="mask dtype is not boolean"):
        execute_signal_program(program, pd.DataFrame({"mask": [0, 1]}))


def test_signal_program_parser_seals_mode_and_output() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "signal-program",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "futures",
            "--output",
            ".nfi/signal-program.json",
        ]
    )

    assert args.strategy_command == "signal-program"
    assert args.trading_mode == "futures"
    assert args.output == Path(".nfi/signal-program.json")
