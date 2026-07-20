from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.performance_gate import (
    _certification_verdict,
    _determinism_assessment,
    _relative_spread,
    _representative_scope,
)


def _manifest(tmp_path: Path, timerange: str, pair_count: int) -> tuple[Path, dict]:
    manifest_path = tmp_path / "manifest.json"
    config_path = tmp_path / "config.json"
    write_json(
        config_path,
        {
            "exchange": {
                "pair_whitelist": [f"PAIR-{index}/USDT" for index in range(pair_count)]
            }
        },
    )
    return manifest_path, {
        "inputs": [{"role": "config", "path": config_path.name}],
        "freqtrade": {"timerange": timerange},
    }


def test_performance_claim_requires_at_least_five_years(tmp_path: Path) -> None:
    manifest_path, manifest = _manifest(tmp_path, "20210101-20260101", 80)

    scope = _representative_scope(manifest_path, manifest)

    assert scope["eligible"] is True
    assert scope["required_days"] == 1825
    assert scope["actual_days"] == 1826


def test_three_year_fixture_remains_diagnostic_only(tmp_path: Path) -> None:
    manifest_path, manifest = _manifest(tmp_path, "20220101-20250101", 80)

    scope = _representative_scope(manifest_path, manifest)

    assert scope["eligible"] is False
    assert scope["label"] == "fixture-diagnostic-only"


def test_representative_gate_cannot_pass_on_parity_alone() -> None:
    complete, certified = _certification_verdict(
        representative=True,
        parity=True,
        speed=False,
        memory=True,
    )

    assert complete is False
    assert certified is False


def test_short_diagnostic_can_complete_without_becoming_release_evidence() -> None:
    complete, certified = _certification_verdict(
        representative=False,
        parity=True,
        speed=False,
        memory=True,
    )

    assert complete is True
    assert certified is False


def test_release_gate_rejects_nondeterministic_results() -> None:
    complete, certified = _certification_verdict(
        representative=True,
        parity=True,
        speed=True,
        memory=True,
        determinism=False,
    )

    assert complete is False
    assert certified is False


def test_result_determinism_requires_one_shared_hash_across_both_lanes() -> None:
    engine = [{"result_sha256": "a" * 64}, {"result_sha256": "a" * 64}]
    reference = [{"result_sha256": "a" * 64}, {"result_sha256": "a" * 64}]
    assert _determinism_assessment(engine, reference)["met"] is True

    reference[1]["result_sha256"] = "b" * 64
    assert _determinism_assessment(engine, reference)["met"] is False


def test_relative_spread_uses_median_and_is_order_independent() -> None:
    runs = [
        {"wall_time_seconds": 100.0},
        {"wall_time_seconds": 104.0},
        {"wall_time_seconds": 102.0},
    ]
    assert _relative_spread(runs) == pytest.approx(4.0 / 102.0)
