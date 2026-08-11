from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from nfi_backtest_engine.signal_fixture import decode_frame
from nfi_backtest_engine.tag_fixture import (
    CONTRACT_PATH,
    FIXTURE_PATH,
    PINNED_SOURCE,
    assert_fixture_identity,
    encode_tag_columns,
    generate_fixture,
)
from nfi_backtest_engine.tag_program import compile_tag_program, execute_tag_program

ROOT = Path(__file__).parents[1]


def test_committed_tag_fixture_has_pinned_identity_and_compound_rows() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert_fixture_identity(fixture)
    assert fixture["source"]["version"] == "2026.5.1"
    assert fixture["source"]["commit"] == "6fa470939cc74bf0672e0e348a4d9b293072e43c"
    assert fixture["source"]["interface_sha256"] == (
        "93ddb2f5579acd7a20d489174ffb68cd191428ff996d291b33be81d97fa9bf66"
    )
    contract = ROOT / CONTRACT_PATH
    assert fixture["source"]["strategy_sha256"] == hashlib.sha256(contract.read_bytes()).hexdigest()
    output = decode_frame(fixture["output"])
    assert output.loc[2, "enter_tag"] == "101 562 "
    assert output.loc[5, "enter_tag"] == "override final  "
    assert output.loc[5, "exit_tag"] == "profit signal "


def test_tag_program_matches_committed_freqtrade_fixture_exactly() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    frame = decode_frame(fixture["input"])
    program = compile_tag_program(ROOT / CONTRACT_PATH, class_name="TagProgramContract")

    output = execute_tag_program(program, frame, metadata={"pair": "ETH/USDT"})
    expected = decode_frame(fixture["output"])

    assert encode_tag_columns(output) == encode_tag_columns(expected)
    for column in ("enter_long", "enter_short", "exit_long", "exit_short"):
        assert output[column].tolist() == expected[column].tolist()


def test_tag_fixture_regeneration_is_deterministic() -> None:
    stored = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    source = ROOT / PINNED_SOURCE
    if not source.is_dir():
        pytest.skip("pinned Freqtrade source checkout is required for regeneration")

    assert generate_fixture(source) == stored
