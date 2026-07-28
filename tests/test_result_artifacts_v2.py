from __future__ import annotations

import csv
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.result_report import (
    format_terminal_summary,
    write_result_presentation,
)
from nfi_backtest_engine.specs import (
    RESULT_EVIDENCE_INDEX_SCHEMA,
    RESULT_VERIFICATION_SCHEMA,
    validate_schema,
)

ROOT = Path(__file__).parents[1]
SPOT_SURFACE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)
FUTURES_SURFACE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-liquidation-stoploss-guard-futures-v17.4.435-2022-04-29_05-02"
    / "artifacts"
    / "trade-surface.json"
)


def _complete_run(root: Path, source_surface: Path = SPOT_SURFACE) -> dict:
    root.mkdir()
    surface = root / "trade-surface.json"
    shutil.copyfile(source_surface, surface)
    document = read_json(surface)
    report = {
        "schema_version": "1.5.0",
        "run_id": "artifact-v2-run",
        "status": "complete",
        "complete": True,
        "prepared_only": False,
        "created_at": "2026-07-29T00:00:00Z",
        "inputs": {
            "pipeline": {"package_version": "1.1.0"},
            "strategy": {
                "class_name": "DynamicArtifactStrategy",
                "file_sha256": "a" * 64,
            },
            "timerange": document["context"]["timerange"],
        },
        "vectors": {"pair_count": 1},
        "execution": {},
        "timings": {"pipeline_wall_time_seconds": 12.5},
        "pipeline_evidence": {
            "cold": False,
            "vector_cache_hits": 1,
        },
        "resumed_stages": [],
        "result": {
            "trade_count": len(document["trades"]),
            "execution": {
                "build": {
                    "binary_sha256": "b" * 64,
                    "target": "test-platform",
                }
            },
            "simulation_result": {
                "path": str(root / "simulation-result.json"),
                "bytes": 1,
                "sha256": "c" * 64,
            },
            "trade_surface": {
                "path": str(surface),
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


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_v2_artifacts_are_complete_hash_indexed_and_source_immutable(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    _complete_run(run)
    source_before = {
        path.name: (path.stat().st_size, sha256_file(path))
        for path in (run / "run.json", run / "trade-surface.json")
    }

    summary = write_result_presentation(run)

    for name in (
        "orders.csv",
        "equity.csv",
        "verification.json",
        "evidence/index.json",
    ):
        assert (run / name).is_file()
    assert source_before == {
        path.name: (path.stat().st_size, sha256_file(path))
        for path in (run / "run.json", run / "trade-surface.json")
    }

    verification = read_json(run / "verification.json")
    index = read_json(run / "evidence/index.json")
    validate_schema(verification, RESULT_VERIFICATION_SCHEMA)
    validate_schema(index, RESULT_EVIDENCE_INDEX_SCHEMA)
    assert [stage["status"] for stage in verification["verification"]["stages"]] == [
        "passed",
        "passed",
        "not_run",
        "not_run",
    ]
    assert verification["verification"]["identities"] == {
        "strategy_sha256": "a" * 64,
        "certified_strategy_sha256": None,
        "package_version": "1.1.0",
        "package_sha256": None,
        "certified_package_sha256": None,
        "native_binary_sha256": "b" * 64,
        "native_target": "test-platform",
        "trade_surface_sha256": sha256_file(run / "trade-surface.json"),
        "simulation_result_sha256": "c" * 64,
    }
    assert summary["artifacts"]["evidence_index"] == "evidence/index.json"
    terminal = format_terminal_summary(summary, run)
    assert str(run / "orders.csv") in terminal
    assert str(run / "equity.csv") in terminal
    assert str(run / "verification.json") in terminal
    assert str(run / "evidence/index.json") in terminal
    assert index["source_evidence_immutable"] is True
    for entry in index["entries"]:
        artifact = run / entry["path"]
        assert artifact.stat().st_size == entry["bytes"]
        assert sha256_file(artifact) == entry["sha256"]


def test_orders_and_equity_exports_preserve_sealed_event_detail(
    tmp_path: Path,
) -> None:
    run = tmp_path / "futures"
    _complete_run(run, FUTURES_SURFACE)
    surface = read_json(run / "trade-surface.json")

    summary = write_result_presentation(run)
    orders = _csv_rows(run / "orders.csv")
    equity = _csv_rows(run / "equity.csv")
    html = (run / "report.html").read_text(encoding="utf-8")

    assert len(orders) == sum(len(trade["orders"]) for trade in surface["trades"])
    assert {row["schema_version"] for row in orders} == {"1.0.0"}
    assert any(row["position_action"] == "partial_exit" for row in orders)
    assert any(row["position_action"] == "exit" for row in orders)
    first_partial = next(row for row in orders if row["position_action"] == "partial_exit")
    assert first_partial["is_partial_exit"] == "True"
    assert first_partial["tag"] == "derisk_level_1"

    assert len(equity) == len(surface["trades"]) + 1
    assert equity[0]["event"] == "start"
    assert all(row["event"] in {"start", "trade_close"} for row in equity)
    assert equity[-1]["source_final_balance"] == surface["summary"]["final_balance"]
    assert (
        Decimal(equity[-1]["equity"])
        + Decimal(equity[-1]["reconciliation_delta"])
        == Decimal(surface["summary"]["final_balance"])
    )
    assert summary["equity_curve"]["source"] == "closed_trade_profit"
    assert summary["risk"]["closed_trade_annualized"] is False
    assert summary["risk"]["closed_trade_return_observations"] == len(
        surface["trades"]
    )

    assert "Orders and position changes" in html
    assert "partial exits" in html
    assert "Funding total" in html
    assert "Liquidation exits" in html
    assert "Candle-level equity" in html
    assert "not annualized" in html
    assert "orders.csv" in html
    assert "equity.csv" in html


def test_verification_binds_strategy_and_never_overwrites_confirmation(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    _complete_run(run)
    proof = tmp_path / "confirmation.json"
    write_json(
        proof,
        {
            "run_id": "artifact-v2-run",
            "equal": True,
            "release_certified": True,
            "inputs": {
                "strategy_sha256": "a" * 64,
                "package_sha256": "f" * 64,
                "engine_trade_surface": {
                    "sha256": sha256_file(run / "trade-surface.json"),
                },
            },
        },
    )
    proof_before = proof.read_bytes()

    summary = write_result_presentation(
        run,
        verification=read_json(proof),
        verification_path=proof,
    )

    assert proof.read_bytes() == proof_before
    assert summary["verification"]["status"] == "exact_match"
    assert summary["verification"]["identities"]["certified_strategy_sha256"] == "a" * 64
    assert summary["verification"]["identities"]["certified_package_sha256"] == "f" * 64
    assert summary["verification"]["stages"][-1]["status"] == "passed"
    assert read_json(run / "verification.json")["verification"]["source_sha256"] == sha256_file(
        proof
    )
    html = (run / "report.html").read_text(encoding="utf-8")
    assert "Certified strategy SHA" in html
    assert "Certified package SHA" in html
    assert ("f" * 64) in html

    changed = read_json(proof)
    changed["inputs"]["strategy_sha256"] = "d" * 64
    with pytest.raises(BenchmarkError, match="different strategy"):
        write_result_presentation(
            run,
            verification=changed,
            verification_path=proof,
        )
    assert proof.read_bytes() == proof_before


def test_confirmation_cannot_collide_with_a_derived_artifact(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _complete_run(run)
    write_result_presentation(run)

    with pytest.raises(BenchmarkError, match="collides with a derived"):
        write_result_presentation(
            run,
            verification=read_json(run / "verification.json"),
            verification_path=run / "verification.json",
        )


def test_prepared_run_gets_schema_only_csvs_and_verification_stages(
    tmp_path: Path,
) -> None:
    run = tmp_path / "prepared"
    run.mkdir()
    write_json(
        run / "run.json",
        {
            "run_id": "prepared-artifacts",
            "status": "prepared",
            "complete": False,
            "prepared_only": True,
            "created_at": "2026-07-29T00:00:00Z",
            "inputs": {
                "pipeline": {"package_version": "1.1.0"},
                "strategy": {
                    "class_name": "PreparedStrategy",
                    "file_sha256": "e" * 64,
                },
                "timerange": "20210101-20260101",
            },
            "vectors": {"pair_count": 80},
            "execution": {},
            "timings": {},
            "resumed_stages": [],
            "result": None,
            "official_confirmation": {"status": "not_run"},
            "capability": {"blockers": []},
        },
    )

    write_result_presentation(run)

    assert _csv_rows(run / "orders.csv") == []
    assert _csv_rows(run / "equity.csv") == []
    verification = read_json(run / "verification.json")
    assert [stage["status"] for stage in verification["verification"]["stages"]] == [
        "not_run",
        "not_run",
        "not_run",
        "not_run",
    ]
    validate_schema(
        read_json(run / "evidence/index.json"),
        RESULT_EVIDENCE_INDEX_SCHEMA,
    )
