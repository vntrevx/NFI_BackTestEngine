from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.result_report import (
    format_run_list,
    format_terminal_summary,
    write_result_presentation,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)


def _complete_run(root: Path) -> dict:
    surface = root / "trade-surface.json"
    shutil.copyfile(FIXTURE, surface)
    report = {
        "schema_version": "1.4.0",
        "run_id": "fixture-run",
        "status": "complete",
        "complete": True,
        "prepared_only": False,
        "created_at": "2026-07-24T00:00:00Z",
        "inputs": {
            "strategy": {
                "class_name": "A <safe> strategy",
                "file_sha256": "a" * 64,
            },
            "timerange": "20250101-20250104",
        },
        "vectors": {"pair_count": 1},
        "execution": {
            "indicator_workers": 2,
            "cpu_process_limit": 4,
            "portfolio_simulator_threads": 2,
            "working_memory_bytes": 8 * 1024**3,
        },
        "timings": {"pipeline_wall_time_seconds": 12.5},
        "resumed_stages": [],
        "result": {
            "trade_count": 6,
            "trade_surface": {
                # A foreign absolute path proves the portable sibling fallback.
                "path": "C:/old-machine/result/trade-surface.json",
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            },
        },
        "official_confirmation": {
            "required_for_finalist": True,
            "status": "not_run",
        },
        "capability": {"blockers": []},
    }
    write_json(root / "run.json", report)
    return report


def test_report_writes_html_json_and_csv_without_mutating_evidence(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)
    evidence_before = (run / "run.json").read_bytes()

    summary = write_result_presentation(run)

    assert (run / "run.json").read_bytes() == evidence_before
    assert read_json(run / "summary.json") == summary
    assert summary["verification"]["status"] == "not_run"
    assert "<A <safe>" not in (run / "report.html").read_text(encoding="utf-8")
    html = (run / "report.html").read_text(encoding="utf-8")
    assert "A &lt;safe&gt; strategy" in html
    assert "Official verification" in html
    assert "Closed-trade cumulative balance" in html
    with (run / "trades.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert rows[0]["pair"] == "BTC/USDT"
    assert rows[0]["profit_abs"] == "-0.49538895"

    terminal = format_terminal_summary(summary, run)
    assert "NFI BACKTEST — COMPLETE ✓" in terminal
    assert "Pairs / trades        1 / 6" in terminal
    assert "Official parity       NOT RUN" in terminal
    assert str(run / "report.html") in terminal


def test_confirmation_refreshes_only_derived_presentation(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)
    evidence_before = (run / "run.json").read_bytes()
    confirmation = {
        "run_id": "fixture-run",
        "equal": True,
        "difference": None,
    }

    summary = write_result_presentation(
        run,
        verification=confirmation,
        verification_path=tmp_path / "confirmation.json",
    )

    assert (run / "run.json").read_bytes() == evidence_before
    assert summary["verification"]["status"] == "exact_match"
    assert "EXACT MATCH" in (run / "report.html").read_text(encoding="utf-8")
    assert "Official parity       EXACT MATCH ✓" in format_terminal_summary(
        summary,
        run,
    )


def test_report_uses_an_adjacent_certification_peak_rss_measurement(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)
    write_json(
        run / "certification-measurement.json",
        {
            "schema_version": "1.0.0",
            "wall_time_seconds": 12.5,
            "peak_rss_bytes": 3 * 1024**3,
            "exit_code": 0,
            "timed_out": False,
        },
    )

    summary = write_result_presentation(run)

    assert summary["execution"]["peak_rss_bytes"] == 3 * 1024**3
    assert "3.0 GiB peak RSS" in format_terminal_summary(summary, run)


def test_hash_valid_confirmation_survives_report_regeneration(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)
    proof = tmp_path / "confirmation.json"
    write_json(
        proof,
        {
            "run_id": "fixture-run",
            "equal": True,
            "engine": {
                "sha256": sha256_file(run / "trade-surface.json"),
            },
            "difference": None,
        },
    )
    write_result_presentation(
        run,
        verification=read_json(proof),
        verification_path=proof,
    )

    preserved = write_result_presentation(run)
    assert preserved["verification"]["status"] == "exact_match"

    write_json(proof, {"run_id": "fixture-run", "equal": False})
    invalidated = write_result_presentation(run)
    assert invalidated["verification"]["status"] == "not_run"


def test_confirmation_must_belong_to_the_same_research_run(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)

    with pytest.raises(BenchmarkError, match="different research run"):
        write_result_presentation(
            run,
            verification={"run_id": "other-run", "equal": True},
        )


def test_reference_report_binds_by_the_engine_surface_hash(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _complete_run(run)
    surface_sha256 = sha256_file(run / "trade-surface.json")

    summary = write_result_presentation(
        run,
        verification={
            "run_id": "independent-reference-run",
            "exact_parity": True,
            "complete": True,
            "inputs": {
                "engine_trade_surface": {
                    "sha256": surface_sha256,
                }
            },
        },
    )

    assert summary["verification"]["status"] == "exact_match"


def test_prepared_run_still_gets_a_clear_empty_report(tmp_path: Path) -> None:
    run = tmp_path / "prepared"
    run.mkdir()
    write_json(
        run / "run.json",
        {
            "run_id": "prepared-run",
            "status": "prepared",
            "complete": False,
            "prepared_only": True,
            "created_at": "2026-07-24T00:00:00Z",
            "inputs": {
                "strategy": {"class_name": "Prepared", "file_sha256": "b" * 64},
                "timerange": "20250101-20260101",
            },
            "vectors": {"pair_count": 80},
            "execution": {},
            "timings": {},
            "resumed_stages": [],
            "result": None,
            "capability": {"blockers": []},
            "official_confirmation": {"status": "not_run"},
        },
    )

    summary = write_result_presentation(run)

    assert summary["run"]["status"] == "prepared"
    assert summary["performance"] is None
    assert (run / "trades.csv").read_text(encoding="utf-8").count("\n") == 1
    assert "PREPARED" in (run / "report.html").read_text(encoding="utf-8")


def test_run_list_defaults_to_a_readable_table() -> None:
    rendered = format_run_list(
        [
            {
                "run_id": "1234567890abcdef",
                "status": "complete",
                "strategy_class": "NostalgiaForInfinityX7",
                "pair_count": 80,
                "trade_count": 927,
                "updated_at": "2026-07-24T12:34:56Z",
                "output_directory": "/home/user/results/x7-five-year",
            }
        ]
    )

    assert rendered.startswith("UPDATED")
    assert "NostalgiaForInfinityX7" in rendered
    assert "927" in rendered
    assert not rendered.lstrip().startswith("[")
