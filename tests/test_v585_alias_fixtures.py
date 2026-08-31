from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.fixture import sha256_file, validate_fixture
from nfi_backtest_engine.state_trace import trace_summary_bytes

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "benchmarks/evidence/future-nfi-v17.4.585-alias-compatibility.json"


def test_v585_alias_compatibility_is_dual_mode_source_bound_and_exact() -> None:
    evidence = read_json(EVIDENCE)

    assert evidence["upstream_commit"] == "47f3b66f4767fe228a74a98f0d4a7e51199e1488"
    assert evidence["strategy_version"] == "v17.4.585"
    assert evidence["strategy_sha256"] == (
        "ff061a8c113b29a599306044cbcc2112ac2eb901f458a55de82bf15f93875e22"
    )
    assert set(evidence["modes"]) == {"spot", "futures"}
    assert evidence["combined_full_x7_certified"] is False
    assert evidence["release_certified"] is False

    for trading_mode, proof in evidence["modes"].items():
        manifest_path = ROOT / proof["fixture"]
        assert sha256_file(manifest_path) == proof["manifest_sha256"]
        manifest = validate_fixture(manifest_path)

        assert manifest["fixture_id"] == proof["fixture_id"]
        assert manifest["freqtrade"]["trading_mode"] == trading_mode
        assert manifest["strategy_provenance"]["upstream_commit"] == evidence["upstream_commit"]
        assert (
            manifest["strategy_provenance"]["effective_source_sha256"]
            == evidence["strategy_sha256"]
        )
        assert manifest["artifacts"]["trade_surface"]["sha256"] == proof[
            "trade_surface_sha256"
        ]
        assert proof["trade_surface_exact"] is True
        assert proof["full_state_exact"] is True

        projection_path = manifest["artifacts"]["state_projection"]["path"]
        retained_payloads = cast(Any, manifest).payloads
        projection = trace_summary_bytes(
            retained_payloads[projection_path],
            label=projection_path,
        )
        assert projection["event_count"] == proof["state_event_count"]
        assert projection["stream_hash"] == proof["state_stream_hash"]
