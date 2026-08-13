from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nfi_backtest_engine.latest_signal47_fixture import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "benchmarks/evidence/m22/latest-x7-full-native-qualification.json"
BOUNDARY = ROOT / "benchmarks/evidence/m22/latest-x7-signal47-boundary.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_latest_full_native_qualification_is_hash_bound_and_exact() -> None:
    report = _load(REPORT)
    boundary = _load(BOUNDARY)

    assert report["schema_version"] == "latest-x7-full-native-qualification-v1"
    assert report["status"] == "exact"
    assert report["fingerprint"] == canonical_sha256(report)
    assert report["source"] == {
        "upstream_commit": "1df961c07e5ce6b1a8cb459a2a46958aed258323",
        "strategy_sha256": "45a2bf611d6fc5e60c7e1f4c672ce7932f6573872c2a959f6338d079dac5e382",
        "strategy_version": "v17.4.528",
        "freqtrade_version": "2026.5.1",
    }
    assert report["changed_branch"]["evidence_fingerprint"] == boundary["fingerprint"]
    assert report["changed_branch"]["evidence_sha256"] == hashlib.sha256(
        BOUNDARY.read_bytes()
    ).hexdigest()

    for mode in ("spot", "futures"):
        evidence = report[mode]
        assert evidence["transport"] == "full-native-vector-manifest"
        assert evidence["trade_surface"]["exact"] is True
        assert evidence["trade_surface"]["native_sha256"] == (
            evidence["trade_surface"]["official_sha256"]
        )
        assert evidence["full_state"]["exact"] is True
        assert evidence["full_state"]["first_difference"] is None
        assert evidence["full_state"]["event_count"] > 0
        assert evidence["blockers"] == []


def test_latest_qualification_does_not_overclaim_later_m22_gates() -> None:
    claims = _load(REPORT)["claims"]

    assert claims["latest_upstream_compiles_without_blockers"] is True
    assert claims["indicator_signal_tag_and_stateful_runtime_is_native"] is True
    assert claims["python_strategy_execution_in_native_runtime"] is False
    assert claims["official_fallback_retained"] is True
    assert claims["runtime_strategy_pair_timerange_sha_or_result_branches_added"] is False
    assert claims["m22_01_latest_dual_mode_qualification_complete"] is True
    assert claims["five_year_performance_certified"] is False
    assert claims["v1_6_0_released"] is False
