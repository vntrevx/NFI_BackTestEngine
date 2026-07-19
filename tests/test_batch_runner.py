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
            "hardware_fingerprint": "hardware",
            "tuning": {
                "independent_research_jobs": 3,
                "independent_engine_jobs": 3,
                "indicator_processes": 6,
                "nested_numeric_threads": 1,
                "working_memory_bytes": 12 * 1024**3,
                "assumed_indicator_worker_peak_bytes": 2 * 1024**3,
            },
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


def test_parallel_plan_splits_one_physical_budget_across_candidate_processes() -> None:
    profile = {
        "tuning": {
            "independent_research_jobs": 4,
            "indicator_processes": 8,
            "nested_numeric_threads": 1,
            "working_memory_bytes": 24 * 1024**3,
            "assumed_indicator_worker_peak_bytes": 3 * 1024**3,
        }
    }

    plan = batch_runner._parallelism_plan(profile, job_count=10, max_jobs=4)

    assert plan == {
        "process_start_method": "spawn",
        "parallel_job_processes": 4,
        "indicator_processes_per_job": 2,
        "maximum_indicator_processes": 8,
        "nested_numeric_threads_per_process": 1,
        "working_memory_bytes": 24 * 1024**3,
        "assumed_indicator_worker_peak_bytes": 3 * 1024**3,
    }
