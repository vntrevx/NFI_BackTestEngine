from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.specs import TAG_PROGRAM_SCHEMA, validate_schema
from nfi_backtest_engine.tag_program import (
    TagProgramCompileError,
    TagProgramExecutionError,
    canonical_tag_route,
    compile_tag_program,
    execute_tag_program,
    validate_tag_program,
)

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "benchmarks" / "reference" / "strategies" / "TagProgramContract.py"


def _input_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "score": [-2.0, -0.5, 0.0, 0.5, 1.5, 2.0, 2.5, float("nan")],
            "exit_mask": pd.array(
                [False, True, False, True, False, True, pd.NA, False],
                dtype="boolean",
            ),
            "enter_tag": ["stale-entry"] * 8,
            "exit_tag": ["stale-exit"] * 8,
        }
    )


def test_tag_program_compiles_ordered_literal_and_compound_mutations() -> None:
    program = compile_tag_program(CONTRACT, class_name="TagProgramContract")

    validate_schema(program, TAG_PROGRAM_SCHEMA)
    validate_tag_program(program)
    assert program["entrypoints"] == [
        {"phase": "entry", "function": "f1"},
        {"phase": "exit", "function": "f2"},
    ]
    writes = [node for node in program["nodes"] if node["op"] == "frame-write"]
    tag_writes = [
        node
        for node in writes
        if any(column.endswith("_tag") for column in node["parameters"]["columns"])
    ]
    assert program["mutation_nodes"] == [node["id"] for node in writes]
    assert program["tag_mutation_nodes"] == [node["id"] for node in tag_writes]
    assert program["tag_outputs"] == [
        {
            "column": "enter_tag",
            "phase": "entry",
            "wrapper_initializer": "",
            "final_mutation": tag_writes[3]["id"],
        },
        {
            "column": "exit_tag",
            "phase": "exit",
            "wrapper_initializer": "",
            "final_mutation": tag_writes[-1]["id"],
        },
    ]
    assert [node["parameters"]["assignment"] for node in tag_writes] == [
        "string-append",
        "string-append",
        "column-values",
        "string-append",
        "column-values",
        "string-append",
    ]
    assert program["required_input_columns"] == ["exit_mask", "score"]
    formatted = [node for node in program["nodes"] if node["op"] == "format-string"]
    assert [node["parameters"]["segments"] for node in formatted] == [["", " "], ["", " "]]
    assert all(node["value_type"] == "string-scalar" for node in formatted)
    assert program["route_contract"]["original_storage"] == "preserve-exact"
    assert len(program["fingerprint"]) == 64


def test_tag_program_executes_priority_and_original_whitespace_exactly() -> None:
    frame = _input_frame()
    program = compile_tag_program(
        CONTRACT,
        class_name="TagProgramContract",
        trading_mode="futures",
    )

    actual = execute_tag_program(program, frame, metadata={"pair": "ETH/USDT"})

    assert actual["enter_tag"].tolist() == [
        "562 ",
        "562 ",
        "101 562 ",
        "101 ",
        "101 ",
        "override final  ",
        "override final  ",
        "",
    ]
    assert actual["exit_tag"].tolist() == [
        "",
        "signal ",
        "",
        "signal ",
        "profit ",
        "profit signal ",
        "profit ",
        "",
    ]
    assert actual.loc[2, ["enter_long", "enter_short"]].tolist() == [1, 1]
    assert actual.loc[2, "enter_tag"] == "101 562 "
    assert actual.loc[5, "exit_tag"] == "profit signal "
    assert frame["enter_tag"].eq("stale-entry").all()
    assert frame["exit_tag"].eq("stale-exit").all()


def test_canonical_route_does_not_change_original_tag() -> None:
    original = "101  562 \t"

    assert canonical_tag_route(original) == ("101", "562")
    assert original == "101  562 \t"
    assert canonical_tag_route("") == ()
    assert canonical_tag_route(None) == ()


def test_tag_program_identity_rejects_order_or_route_mutation(tmp_path: Path) -> None:
    copied = tmp_path / "Renamed.py"
    copied.write_bytes(CONTRACT.read_bytes())
    first = compile_tag_program(CONTRACT, class_name="TagProgramContract")
    second = compile_tag_program(copied, class_name="TagProgramContract")
    assert first["fingerprint"] == second["fingerprint"]

    reordered = copy.deepcopy(first)
    reordered["tag_mutation_nodes"][:2] = reversed(reordered["tag_mutation_nodes"][:2])
    with pytest.raises(SpecValidationError, match="tag mutation inventory differs"):
        validate_tag_program(reordered)

    trimmed_contract = copy.deepcopy(first)
    trimmed_contract["route_contract"]["trailing_whitespace"] = "trim"
    with pytest.raises(SpecValidationError, match="tag-program-v1.schema.json"):
        validate_tag_program(trimmed_contract)

    changed_location = copy.deepcopy(first)
    changed_location["source_map"]["n1"]["line"] += 1
    with pytest.raises(SpecValidationError, match="fingerprint differs"):
        validate_tag_program(changed_location)

    malformed_format = copy.deepcopy(first)
    format_node = next(node for node in malformed_format["nodes"] if node["op"] == "format-string")
    format_node["parameters"]["segments"] = [""]
    with pytest.raises(SpecValidationError, match="format-string contract is invalid"):
        validate_tag_program(malformed_format)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("dataframe.loc[:, 'enter_tag'] = 1", "non-string tag assignment"),
        ("dataframe.loc[:, 'enter_long'] = '1'", "non-numeric signal assignment"),
        ("dataframe.loc[:, 'exit_tag'] = 'wrong-phase'", "during the entry phase"),
        ("dataframe.loc[:, 'feature'] = 'tag'", "non-signal/tag dataframe output"),
        ("dataframe.loc[:, 'enter_tag'] -= 'tag'", "non-additive tag"),
    ],
)
def test_tag_program_fails_closed_outside_exact_surface(
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

    with pytest.raises(TagProgramCompileError, match=message):
        compile_tag_program(source, class_name="Unsupported")


def test_tag_program_runtime_fails_closed_for_numeric_mask(tmp_path: Path) -> None:
    source = tmp_path / "NumericMask.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NumericMask(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[dataframe['mask'], 'enter_tag'] += '101 '\n"
        "        return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata):\n"
        "        dataframe.loc[:, 'exit_long'] = 0\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    program = compile_tag_program(source, class_name="NumericMask")

    with pytest.raises(TagProgramExecutionError, match="mask dtype is not boolean"):
        execute_tag_program(program, pd.DataFrame({"mask": [0, 1]}))


def test_tag_program_parser_seals_mode_and_output() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "tag-program",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "futures",
            "--output",
            ".nfi/tag-program.json",
        ]
    )

    assert args.strategy_command == "tag-program"
    assert args.trading_mode == "futures"
    assert args.output == Path(".nfi/tag-program.json")
