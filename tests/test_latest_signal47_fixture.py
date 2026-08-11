from __future__ import annotations

import json
from pathlib import Path

from nfi_backtest_engine.latest_signal47_fixture import (
    CHANGED_EXPRESSION,
    FIXTURE_PATH,
    SCHEMA_VERSION,
    canonical_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def test_latest_signal47_boundary_is_dual_mode_and_source_bound() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert fixture["schema_version"] == SCHEMA_VERSION
    assert fixture["fingerprint"] == canonical_sha256(fixture)
    assert fixture["source"]["commit"] == "1df961c07e5ce6b1a8cb459a2a46958aed258323"
    assert fixture["source"]["strategy_sha256"] == (
        "45a2bf611d6fc5e60c7e1f4c672ce7932f6573872c2a959f6338d079dac5e382"
    )
    assert fixture["changed_route"]["expression"] == CHANGED_EXPRESSION
    assert set(fixture["source_diff"]["changed_methods"]) == {
        "populate_entry_trend",
        "version",
    }
    assert fixture["source_diff"]["callback_and_stateful_methods_changed"] is False
    assert fixture["source_diff"]["unchanged_method_count"] > 100
    assert set(fixture["modes"]) == {"spot", "futures"}

    expected_pass = [0, 0, 0, 0, 0, 1, 1, 1]
    expected_reject = [0] * 8
    for mode in fixture["modes"].values():
        assert mode["required_input_column_count"] == 179
        reject = mode["cases"]["new_protection_rejects"]
        accepted = mode["cases"]["one_term_passes"]
        assert reject["baseline"]["enter_long"] == expected_pass
        assert reject["baseline"]["enter_tag"][-3:] == ["47 "] * 3
        assert reject["current"]["enter_long"] == expected_reject
        assert reject["current"]["enter_tag"] == [""] * 8
        assert accepted["baseline"]["enter_long"] == expected_pass
        assert accepted["current"]["enter_long"] == expected_pass
        assert accepted["current"]["enter_tag"][-3:] == ["47 "] * 3


def test_latest_signal47_boundary_records_independent_exact_lanes() -> None:
    fixture = json.loads((ROOT / FIXTURE_PATH).read_text(encoding="utf-8"))

    assert fixture["claims"] == {
        "source_wrapper_python_program_exact": True,
        "source_wrapper_rust_signal_exact": True,
        "source_wrapper_rust_tag_exact": True,
        "spot_and_futures_exact": True,
        "runtime_signal_number_branch_added": False,
    }
    for mode in fixture["modes"].values():
        for identity in ("current", "baseline"):
            programs = mode["programs"][identity]
            assert len(programs["signal_fingerprint"]) == 64
            assert len(programs["tag_fingerprint"]) == 64
            assert programs["signal_node_count"] > 9_000
            assert programs["tag_node_count"] > programs["signal_node_count"]
