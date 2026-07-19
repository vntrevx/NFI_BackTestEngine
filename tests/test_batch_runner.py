from __future__ import annotations

import os
from pathlib import Path

from nfi_backtest_engine import batch_runner
from nfi_backtest_engine.canonical import write_json


def test_single_batch_job_uses_hardware_worker_budget(monkeypatch, tmp_path: Path) -> None:
    strategy = tmp_path / "strategy.py"
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    strategy.write_text("# strategy", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    data.mkdir()
    manifest = tmp_path / "batch.json"
    write_json(
        manifest,
        {
            "schema_version": "1.0.0",
            "jobs": [
                {
                    "name": "candidate-a",
                    "strategy_path": "strategy.py",
                    "class_name": "Candidate",
                    "config_path": "config.json",
                    "data_directory": "data",
                    "timerange": "20250101-20250102",
                    "prepare_only": True,
                }
            ],
        },
    )
    captured = {}

    monkeypatch.setattr(
        batch_runner,
        "ensure_execution_profile",
        lambda *a, **k: {
            "schema_version": "2.0.0",
            "created_at": "2026-01-01T00:00:00Z",
            "hardware_fingerprint": "hardware",
            "hardware": {
                "platform": "test",
                "machine": "x86_64",
                "cpu_name": "test",
                "physical_cpu_count": 6,
                "logical_cpu_count": 6,
                "affinity_cpu_count": 6,
                "affinity_cpu_ids": list(range(6)),
                "memory": {
                    "total_bytes": 16 * 1024**3,
                    "available_bytes": 12 * 1024**3,
                },
            },
            "limits": {
                "memory_cap_bytes": 12 * 1024**3,
                "cpu_process_limit": 6,
            },
            "runtime": {
                "portfolio_simulator_threads": 1,
                "nested_numeric_threads": 1,
            },
            "environment": {"OMP_NUM_THREADS": "1"},
        },
    )
    monkeypatch.setattr(
        batch_runner,
        "current_resource_limits",
        lambda _profile: {
            "memory_cap_bytes": 12 * 1024**3,
            "working_memory_bytes": 12 * 1024**3,
            "cpu_process_limit": 6,
        },
    )

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "prepared",
            "complete": False,
            "prepared_only": True,
            "run_id": "run",
        }

    monkeypatch.setattr(batch_runner, "run_research_backtest", fake_run)

    report = batch_runner.run_batch(
        manifest,
        tmp_path / "output",
        profile_path=tmp_path / "profile.json",
        cache_directory=tmp_path / "cache",
        registry_path=tmp_path / "runs.sqlite",
    )

    assert report["complete"]
    assert report["schema_version"] == "1.1.0"
    assert report["parallel_jobs"] == 1
    assert report["coordinator_process_id"] == os.getpid()
    assert report["jobs"][0]["process_id"] == os.getpid()
    assert report["wall_time_seconds"] >= report["jobs"][0]["wall_time_seconds"]
    assert report["aggregate_job_seconds"] == report["jobs"][0]["wall_time_seconds"]
    assert 0 < report["effective_parallelism"] <= 1
    assert report["parallel_efficiency"] == report["effective_parallelism"]
    assert captured["workers"] == 6
    assert captured["strategy_path"] == str(strategy.resolve())


def test_uncalibrated_candidates_start_with_one_coordinator(monkeypatch) -> None:
    profile = {
        "schema_version": "2.0.0",
        "created_at": "2026-01-01T00:00:00Z",
        "hardware_fingerprint": "ignored-by-monkeypatch",
        "hardware": {},
        "limits": {"memory_cap_bytes": None, "cpu_process_limit": 8},
        "runtime": {
            "portfolio_simulator_threads": 1,
            "nested_numeric_threads": 1,
        },
        "environment": {"OMP_NUM_THREADS": "1"},
    }
    limits = {
        "memory_cap_bytes": None,
        "working_memory_bytes": 24 * 1024**3,
        "cpu_process_limit": 8,
    }
    monkeypatch.setattr(batch_runner, "current_resource_limits", lambda _profile: limits)
    plan = batch_runner._parallelism_plan(profile, job_count=10, max_jobs=4)

    assert plan == {
        "process_start_method": "spawn",
        "parallel_job_processes": 1,
        "indicator_processes_per_job": 8,
        "maximum_indicator_processes": 8,
        "nested_numeric_threads_per_process": 1,
        "working_memory_bytes": 24 * 1024**3,
        "admission": "serial-until-workload-calibrated",
    }
