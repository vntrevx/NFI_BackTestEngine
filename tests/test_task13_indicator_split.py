from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine import _indicator_contract
from nfi_backtest_engine.indicator_program import (
    INDICATOR_PROGRAM_VERSION,
    IndicatorProgramCompileError,
    compile_indicator_program,
    validate_indicator_program,
)

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "benchmarks/reference/strategies/IndicatorProgramContract.py"
PACKAGE = ROOT / "python/nfi_backtest_engine"


def _canonical_bytes(program: dict[str, object]) -> bytes:
    portable = dict(program)
    raw_source = portable["source"]
    assert isinstance(raw_source, dict)
    source = dict(raw_source)
    source["path"] = CONTRACT.name
    portable["source"] = source
    return json.dumps(
        portable,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_task13_locks_public_facade_and_canonical_contract() -> None:
    assert INDICATOR_PROGRAM_VERSION == "indicator-program-v1"
    assert IndicatorProgramCompileError is _indicator_contract.IndicatorProgramCompileError
    assert compile_indicator_program.__module__ == "nfi_backtest_engine.indicator_program"
    assert validate_indicator_program.__module__ == "nfi_backtest_engine.indicator_program"

    first = compile_indicator_program(CONTRACT, class_name="IndicatorProgramContract")
    second = compile_indicator_program(CONTRACT, class_name="IndicatorProgramContract")

    assert _canonical_bytes(first) == _canonical_bytes(second)
    assert (
        first["fingerprint"] == "cb278c7294995c0a8c018d44485990612e8370ba4fff7e5d71df0c3c862fc8ca"
    )
    assert hashlib.sha256(_canonical_bytes(first)).hexdigest() == (
        "2b2d5220a54520d3ed3cd34177f9fa982181bc90ef484eff5df291daff0be0fe"
    )
    assert list(first["source_map"]) == [node["id"] for node in first["nodes"]]


def test_task13_locks_unsupported_ast_source_diagnostic(tmp_path: Path) -> None:
    source = tmp_path / "Unsupported.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Unsupported(IStrategy):\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['bad'] = {value for value in dataframe['close']}\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    with pytest.raises(IndicatorProgramCompileError) as captured:
        compile_indicator_program(source, class_name="Unsupported")

    assert str(captured.value) == (
        "strategy.py:4:27: indicator-program-v1 does not support indicator expression SetComp"
    )


def test_task13_compiler_is_decomposed_below_pure_loc_ceiling() -> None:
    modules = sorted(PACKAGE.glob("indicator_compiler_*.py"))
    assert PACKAGE.joinpath("indicator_compiler_expressions.py") in modules
    assert len(modules) >= 8
    for module in [PACKAGE / "indicator_program.py", *modules]:
        pure_lines = [
            line
            for line in module.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert len(pure_lines) <= 250, f"{module.name}: {len(pure_lines)} pure LOC"
