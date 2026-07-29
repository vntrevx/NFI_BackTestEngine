from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.state_machine_shadow import (
    evaluate_state_machine_shadow_gate,
)
from nfi_backtest_engine.state_trace import StateTraceWriter

SURFACE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)
HASH = "a" * 64


def test_shadow_gate_requires_separate_exact_full_state_executions(
    tmp_path: Path,
) -> None:
    legacy = _run(tmp_path / "legacy", lane="x7-legacy", run_id="legacy")
    candidate = _run(
        tmp_path / "candidate",
        lane="generic-state-machine",
        run_id="candidate",
    )
    legacy_trace = _trace(tmp_path / "legacy.trace", input_sha="1" * 64)
    candidate_trace = _trace(tmp_path / "candidate.trace", input_sha="2" * 64)
    proof = _proof(tmp_path / "proof.json")

    report = evaluate_state_machine_shadow_gate(
        legacy,
        candidate,
        legacy_trace=legacy_trace,
        candidate_trace=candidate_trace,
        branch_proof=proof,
    )

    assert report["separate_executions"] is True
    assert report["changed_branch_reached"] is True
    assert report["trade_surface_exact"] is True
    assert report["full_state_exact"] is True
    assert report["promoted"] is True


def test_shadow_gate_reports_state_difference_without_promotion(
    tmp_path: Path,
) -> None:
    legacy = _run(tmp_path / "legacy", lane="x7-legacy", run_id="legacy")
    candidate = _run(
        tmp_path / "candidate",
        lane="generic-state-machine",
        run_id="candidate",
    )
    legacy_trace = _trace(tmp_path / "legacy.trace", input_sha="1" * 64)
    candidate_trace = _trace(
        tmp_path / "candidate.trace",
        input_sha="2" * 64,
        balance="999",
    )

    report = evaluate_state_machine_shadow_gate(
        legacy,
        candidate,
        legacy_trace=legacy_trace,
        candidate_trace=candidate_trace,
        branch_proof=_proof(tmp_path / "proof.json"),
    )

    assert report["trade_surface_exact"] is True
    assert report["full_state_exact"] is False
    assert report["promoted"] is False
    assert report["differences"]["full_state"]["path"] == "$.state.wallet.balance"


def test_shadow_gate_rejects_a_shared_run_directory(tmp_path: Path) -> None:
    run = _run(tmp_path / "run", lane="generic-state-machine", run_id="run")

    with pytest.raises(SpecValidationError, match="two separate run directories"):
        evaluate_state_machine_shadow_gate(
            run,
            run,
            legacy_trace=tmp_path / "missing-legacy.trace",
            candidate_trace=tmp_path / "missing-candidate.trace",
            branch_proof=tmp_path / "missing-proof.json",
        )


def _run(path: Path, *, lane: str, run_id: str) -> Path:
    path.mkdir()
    surface = path / "trade-surface.json"
    shutil.copyfile(SURFACE, surface)
    report = {
        "run_id": run_id,
        "status": "complete",
        "complete": True,
        "inputs": {
            "strategy": {"file_sha256": HASH},
            "config": {"run_effective_sha256": "b" * 64},
            "pairlist_sha256": "c" * 64,
            "timerange": "20250101-20250102",
        },
        "data": {"aggregate_sha256": "d" * 64},
        "capability": {"adapter_lane": lane},
        "result": {
            "trade_surface": {
                "path": str(surface),
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            }
        },
    }
    write_json(path / "run.json", report)
    return path


def _trace(
    path: Path,
    *,
    input_sha: str,
    balance: str = "1000",
) -> Path:
    with StateTraceWriter(
        path,
        source="engine",
        run_id=path.stem,
        input_sha256=input_sha,
        strategy_sha256=HASH,
        profile_sha256="e" * 64,
        trading_mode="spot",
        include_state=True,
    ) as trace:
        trace.append(
            timestamp_ms=1,
            phase="callback",
            callback="adjust_trade_position",
            state={"wallet": {"balance": balance}},
        )
    return path


def _proof(path: Path) -> Path:
    write_json(
        path,
        {
            "schema_version": "1.1.0",
            "verification_level": "full",
            "complete": True,
            "parity": {
                "trade_surface": {"equal": True},
                "state_trace": {"equal": True},
            },
            "branch_coverage": {"met": True},
        },
    )
    return path
