from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nfi_backtest_engine.latest_signal47_fixture import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "benchmarks/evidence/m22/full-native-performance-storage.json"


def _load() -> dict[str, Any]:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_full_native_performance_evidence_is_hash_bound_and_repeatable() -> None:
    report = _load()

    assert report["schema_version"] == "full-native-performance-storage-v1"
    assert report["status"] == "certified"
    assert report["fingerprint"] == canonical_sha256(report)
    assert len(report["repetitions"]) == 3
    assert {item["name"] for item in report["repetitions"]} == {
        "cold-01",
        "warm-02",
        "warm-03",
    }
    assert report["result"]["byte_identical_across_repetitions"] is True
    assert report["result"]["trade_count"] > 0
    assert report["aggregate"]["wall_spread_ratio"] <= 0.05
    assert report["aggregate"]["five_repetitions_required"] is False


def test_full_native_storage_evidence_is_bounded_and_does_not_overclaim() -> None:
    report = _load()
    storage = report["storage"]
    claims = report["claim_boundary"]

    assert storage["actual_file_backed_bytes"] <= storage["required_upper_bound_bytes"]
    assert storage["bound_margin_bytes"] == (
        storage["required_upper_bound_bytes"] - storage["actual_file_backed_bytes"]
    )
    assert storage["named_orphan_file_count"] == 0
    assert storage["delete_pending_handle_count_after_exit"] == 0
    assert storage["unbounded_local_accumulation_observed"] is False
    assert claims["five_year_full_native_performance_and_storage_certified"] is True
    assert claims["runtime_strategy_pair_timerange_sha_or_result_hardcoding_added"] is False
    assert claims["official_freqtrade_five_year_parity_claimed"] is False
    assert claims["cross_platform_performance_claimed"] is False
    assert claims["futures_five_year_performance_claimed"] is False
    assert claims["v1_6_0_released"] is False


def test_full_native_dual_mode_exact_regression_is_zero_tolerance() -> None:
    regressions = _load()["exact_regression"]

    for mode in ("spot", "futures"):
        evidence = regressions[mode]
        assert evidence["trade_surface_exact"] is True
        assert evidence["full_state_exact"] is True
        assert evidence["state_event_count"] > 0
        assert len(evidence["state_stream_hash"]) == 64
