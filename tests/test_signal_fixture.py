from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine.signal_fixture import (
    CONTRACT_PATH,
    FIXTURE_PATH,
    PINNED_SOURCE,
    canonical_sha256,
    decode_frame,
    encode_signal_columns,
    generate_fixture,
)
from nfi_backtest_engine.signal_program import compile_signal_program, execute_signal_program

ROOT = Path(__file__).resolve().parents[1]


def test_committed_signal_fixture_has_pinned_official_identity() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert fixture["fingerprint"] == canonical_sha256(fixture)
    assert fixture["schema_version"] == "freqtrade-signal-fixture-v1"
    assert fixture["source"]["version"] == "2026.5.1"
    assert fixture["source"]["commit"] == "6fa470939cc74bf0672e0e348a4d9b293072e43c"
    assert fixture["call_order"] == ["advise_entry", "advise_exit"]
    assert set(fixture["source"]["method_sha256"]) == {"advise_entry", "advise_exit"}


def test_signal_program_matches_pinned_freqtrade_raw_signal_columns() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    frame = decode_frame(fixture["input"])
    program = compile_signal_program(CONTRACT_PATH, class_name="SignalProgramContract")

    output = execute_signal_program(program, frame, metadata={"pair": "ETH/USDT"})
    expected = decode_frame(fixture["output"])

    assert encode_signal_columns(output) == encode_signal_columns(expected)
    assert output.loc[4, ["enter_long", "exit_long"]].tolist() == [0, 1]
    assert pd.isna(frame.loc[6, "score"])


def test_signal_fixture_regeneration_is_deterministic() -> None:
    stored = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))
    source = ROOT / PINNED_SOURCE
    if not source.is_dir():
        pytest.skip("pinned Freqtrade source checkout is required for regeneration")

    assert generate_fixture(source) == stored
