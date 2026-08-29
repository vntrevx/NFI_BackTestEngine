from __future__ import annotations

import json
from pathlib import Path

import pandas as pd  # noqa: PANDAS_OK
from nfi_backtest_engine import _rust
from nfi_backtest_engine.parity import first_difference
from nfi_backtest_engine.signal_program import (
    compile_signal_program,
    execute_signal_program,
)
from nfi_backtest_engine.tag_program import compile_tag_program, execute_tag_program

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "benchmarks/reference/strategies/CurrentChangedPredicateContract.py"
CLASS_NAME = "CurrentChangedPredicateContract"
ATOMIC_COLUMNS = ("RSI_3_15m", "RSI_3_1h", "RSI_3_4h", "AROONU_14_1h")


def _boundary_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "RSI_3_15m": [15.0, 15.000000000000002, 15.0, 15.0, 15.0],
            "RSI_3_1h": [20.0, 20.0, 20.000000000000004, 20.0, 20.0],
            "RSI_3_4h": [25.0, 25.0, 25.0, 25.000000000000004, 25.0],
            "AROONU_14_1h": [0.0, 0.0, 0.0, 0.0, 5e-324],
        }
    )


def test_current_changed_predicate_lowers_all_atomic_boundaries_in_both_modes() -> None:
    frame = _boundary_frame()
    for mode in ("spot", "futures"):
        config = {"trading_mode": mode}
        signal = compile_signal_program(
            CONTRACT,
            class_name=CLASS_NAME,
            trading_mode=mode,
            config=config,
        )
        tag = compile_tag_program(
            CONTRACT,
            class_name=CLASS_NAME,
            trading_mode=mode,
            config=config,
        )
        signal_output = execute_signal_program(signal, frame)
        tag_output = execute_tag_program(tag, frame)
        bridge = _rust.execute_numeric_mutation_program(
            json.dumps(tag, separators=(",", ":")),
            {column: frame[column].tolist() for column in ATOMIC_COLUMNS},
            {},
            ["enter_long", "enter_short", "enter_tag", "exit_long", "exit_short", "exit_tag"],
        )

        assert signal["required_input_columns"] == sorted(ATOMIC_COLUMNS)
        assert tag["required_input_columns"] == sorted(ATOMIC_COLUMNS)
        assert signal_output["enter_short"].tolist() == [0, 1, 1, 1, 1]
        assert tag_output["enter_tag"].tolist() == ["", "562 ", "562 ", "562 ", "562 "]
        assert bridge["enter_long"]["values"] == [0, 0, 0, 0, 0]
        assert bridge["enter_short"]["values"] == [0, 1, 1, 1, 1]
        assert bridge["enter_tag"]["values"] == ["", "562 ", "562 ", "562 ", "562 "]
        assert bridge["exit_long"]["values"] == [0, 0, 0, 0, 0]
        assert bridge["exit_short"]["values"] == [0, 0, 0, 0, 0]
        assert bridge["exit_tag"]["values"] == ["", "", "", "", ""]


def test_each_current_source_comparator_mutation_has_first_difference(tmp_path: Path) -> None:
    official = execute_tag_program(
        compile_tag_program(CONTRACT, class_name=CLASS_NAME),
        _boundary_frame(),
    )
    source = CONTRACT.read_text(encoding="utf-8")
    needles = ('> 15.0)', '> 20.0)', '> 25.0)', '> 0.0)')
    for index, needle in enumerate(needles):
        mutant = tmp_path / f"mutant-{index}.py"
        mutant.write_text(source.replace(needle, needle.replace(">", ">="), 1), encoding="utf-8")
        native = execute_tag_program(
            compile_tag_program(mutant, class_name=CLASS_NAME),
            _boundary_frame(),
        )
        difference = first_difference(
            official["enter_short"].tolist(),
            native["enter_short"].tolist(),
        )
        assert difference is not None
        assert difference.path == "$[0]"
