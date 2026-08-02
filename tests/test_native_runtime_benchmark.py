from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.native_runtime_benchmark import (
    _performance_gates,
    _workload_argument,
    run_native_runtime_benchmark,
)


def test_native_runtime_benchmark_uses_argument_supplied_workloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline-bin"
    candidate = tmp_path / "candidate-bin"
    spot = tmp_path / "spot.manifest.json"
    futures = tmp_path / "futures.manifest.json"
    for path in (baseline, candidate, spot, futures):
        path.write_bytes(path.name.encode())

    def fake_run_once(
        binary: Path,
        manifest: Path,
        output: Path,
        profile: Path,
        *,
        poll_interval_ms: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        del output, profile, poll_interval_ms, timeout_seconds
        is_candidate = binary == candidate
        return {
            "wall_time_seconds": 0.9 if is_candidate else 1.0,
            "event_loop_seconds": 0.45 if is_candidate else 0.5,
            "peak_rss_bytes": 90 if is_candidate else 100,
            "result_sha256": "a" * 64 if manifest == spot else "b" * 64,
        }

    monkeypatch.setattr(
        "nfi_backtest_engine.native_runtime_benchmark._run_once",
        fake_run_once,
    )

    report = run_native_runtime_benchmark(
        baseline_binary=baseline,
        candidate_binary=candidate,
        workloads={"spot": spot, "futures": futures},
        output_path=tmp_path / "report.json",
        baseline_identity="before",
        candidate_identity="after",
        inner_iterations=2,
    )

    assert report["status"] == "passed"
    assert report["measurement_contract"]["measured_repetitions"] == 3
    assert report["result_identity"]["exact"] is True
    assert report["gates"]["fresh_process_wall"]["met"] is True
    assert set(report["workloads"]) == {"spot", "futures"}


def test_native_runtime_gates_reject_result_or_resource_regression() -> None:
    summaries = {
        "baseline": {
            "wall_time_seconds_per_workload": {"median": 1.0, "relative_spread": 0.01},
            "event_loop_seconds_per_workload": {"median": 0.5, "relative_spread": 0.01},
            "peak_rss_bytes": {"maximum": 100},
        },
        "candidate": {
            "wall_time_seconds_per_workload": {"median": 1.2, "relative_spread": 0.01},
            "event_loop_seconds_per_workload": {"median": 0.6, "relative_spread": 0.01},
            "peak_rss_bytes": {"maximum": 120},
        },
    }

    gates = _performance_gates(
        summaries,
        result_identity_exact=False,
        spread_threshold=0.05,
        regression_tolerance=0.05,
    )

    assert gates["result_identity"]["met"] is False
    assert gates["fresh_process_wall"]["met"] is False
    assert gates["event_loop"]["met"] is False
    assert gates["peak_rss"]["met"] is False
    assert gates["wall_spread"]["met"] is True


def test_native_runtime_benchmark_rejects_missing_or_duplicate_inputs(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="at least one workload"):
        run_native_runtime_benchmark(
            baseline_binary=tmp_path / "missing-baseline",
            candidate_binary=tmp_path / "missing-candidate",
            workloads={},
            output_path=tmp_path / "report.json",
            baseline_identity="before",
            candidate_identity="after",
        )
    with pytest.raises(argparse.ArgumentTypeError):
        _workload_argument("missing-separator")
