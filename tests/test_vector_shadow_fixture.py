from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pandas as pd
from nfi_backtest_engine.signal_program import execute_signal_program, validate_signal_program
from nfi_backtest_engine.tag_program import execute_tag_program, validate_tag_program
from nfi_backtest_engine.vector_shadow_fixture import (
    FIXTURE_PATH,
    INDICATOR_PROGRAM_PATH,
    SIGNAL_PROGRAM_PATH,
    TAG_PROGRAM_PATH,
    _portable_path,
    canonical_sha256,
    decode_exact_frame,
    encode_frame,
    generate_bundle,
)
from nfi_backtest_engine.vector_worker import _materialize_execution_signals

ROOT = Path(__file__).parents[1]


def _load(path: Path) -> dict[str, object]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_vector_shadow_bundle_identity_and_regeneration_are_exact() -> None:
    fixture = _load(FIXTURE_PATH)
    generated, programs = generate_bundle()

    assert fixture["fingerprint"] == canonical_sha256(fixture)
    assert generated == fixture
    assert programs["indicator"] == _load(INDICATOR_PROGRAM_PATH)
    assert programs["signal"] == _load(SIGNAL_PROGRAM_PATH)
    assert programs["tag"] == _load(TAG_PROGRAM_PATH)
    validate_signal_program(programs["signal"])
    validate_tag_program(programs["tag"])


def test_python_signal_tag_and_execution_lanes_recompute_the_fixture() -> None:
    fixture = _load(FIXTURE_PATH)
    cases = fixture["cases"]
    assert isinstance(cases, dict)
    signal_case = cases["signal"]
    tag_case = cases["tag"]
    execution_case = cases["execution"]
    assert isinstance(signal_case, dict)
    assert isinstance(tag_case, dict)
    assert isinstance(execution_case, dict)

    signal_program = _load(SIGNAL_PROGRAM_PATH)
    tag_program = _load(TAG_PROGRAM_PATH)
    signal_input = decode_exact_frame(signal_case["input"])
    tag_input = decode_exact_frame(tag_case["input"])
    signal_output = execute_signal_program(signal_program, signal_input)
    tag_output = execute_tag_program(tag_program, tag_input)

    assert encode_frame(signal_output.loc[:, signal_case["outputs"]]) == signal_case["expected"]
    assert encode_frame(tag_output.loc[:, tag_case["outputs"]]) == tag_case["expected"]
    materialized = _materialize_execution_signals(tag_output)
    actual_execution = encode_frame(materialized.loc[:, execution_case["outputs"]])
    assert actual_execution == execution_case["expected"]
    start = execution_case["execution_start_index"]
    assert isinstance(start, int)
    for column, expected in execution_case["enabled_indexes"].items():
        enabled = materialized[f"nfi_exec_{column}"].eq(1).fillna(False)
        actual = [index for index in range(start, len(enabled)) if bool(enabled.iloc[index])]
        assert actual == expected


def test_exact_frame_codec_keeps_nan_null_and_tag_whitespace_distinct() -> None:
    fixture = _load(FIXTURE_PATH)
    cases = fixture["cases"]
    assert isinstance(cases, dict)
    indicator = cases["indicator"]
    tag = cases["tag"]
    assert isinstance(indicator, dict)
    assert isinstance(tag, dict)

    indicator_expected = decode_exact_frame(indicator["expected"])
    tag_expected = decode_exact_frame(tag["expected"])
    assert pd.isna(indicator_expected.loc[0, "previous_close"])
    assert tag_expected.loc[2, "enter_tag"] == "101 562 "
    assert tag_expected.loc[5, "enter_tag"] == "override final  "


def test_contract_paths_are_platform_independent() -> None:
    assert (
        _portable_path(PureWindowsPath("benchmarks/reference/vector-shadow/tag-program.json"))
        == "benchmarks/reference/vector-shadow/tag-program.json"
    )
