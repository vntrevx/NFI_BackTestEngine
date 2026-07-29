from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-futures-lifecycle-short-v17.4.435-2022-04-01_04-20"
)
SPEC = importlib.util.spec_from_file_location(
    "futures_candidate_pr",
    ROOT / "scripts" / "futures_candidate_pr.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _report() -> dict:
    logical_bytes = _bytes(FIXTURE)
    manifest = MODULE.validate_fixture(FIXTURE / "manifest.json")
    return {
        "status": "candidate_found",
        "fingerprint": "a" * 64,
        "upstream_commit": manifest["strategy_provenance"]["upstream_commit"],
        "engine_commit": "c" * 40,
        "strategy_sha256": manifest["strategy_provenance"]["effective_source_sha256"],
        "candidate": {
            "fixture_id": manifest["fixture_id"],
            "manifest_sha256": MODULE.sha256_file(FIXTURE / "manifest.json"),
            "trade_surface_exact": True,
            "full_state_exact": True,
            "logical_bytes": logical_bytes,
            "target_ids": ["generic-target"],
            "pair": "BTC/USDT:USDT",
            "timerange": "20220401-20220420",
        },
    }


def test_candidate_plan_allows_only_exact_size_bounded_fixture(
    tmp_path: Path,
) -> None:
    plan = MODULE.build_candidate_plan(
        _report(),
        FIXTURE,
        tmp_path,
        max_bytes=30 * 1024 * 1024,
    )

    assert plan["branch"] == "automation/futures-fixture-" + "a" * 16
    assert plan["fixture_destination"] == (
        "benchmarks/fixtures/captured/x7-v17.4.435-futures-lifecycle-btc-short"
    )
    assert plan["evidence_destination"] == (
        "benchmarks/evidence/future-nfi-futures-" + "a" * 16 + ".json"
    )


def test_candidate_plan_rejects_non_exact_or_size_mismatched_evidence(
    tmp_path: Path,
) -> None:
    report = _report()
    report["candidate"]["full_state_exact"] = False
    with pytest.raises(ValueError, match="exact evidence"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )

    report = _report()
    report["candidate"]["logical_bytes"] += 1
    with pytest.raises(ValueError, match="sealed size"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )


def test_candidate_plan_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    report = _report()
    report["candidate"]["manifest_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="sealed manifest"):
        MODULE.build_candidate_plan(
            report,
            FIXTURE,
            tmp_path,
            max_bytes=30 * 1024 * 1024,
        )


def test_candidate_plan_rejects_symlinked_input(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.symlink_to(FIXTURE, target_is_directory=True)

    with pytest.raises(ValueError, match="root must not be a symlink"):
        MODULE.build_candidate_plan(
            _report(),
            candidate,
            tmp_path / "repo",
            max_bytes=30 * 1024 * 1024,
        )
