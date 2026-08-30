from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.compatibility_automation import classify_compatibility_automation
from nfi_backtest_engine.discovery_publication import (
    authorize_discovery_publication,
    validate_discovery_authorization,
)
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.futures_discovery import discover_targets
from nfi_backtest_engine.reference.contracts import REFERENCE_INDEX_DIGEST

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures" / "captured"


def _documents(
    tmp_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    Path,
    Path,
]:
    source = tmp_path / "strategy.py"
    source.write_text("class Demo: pass\n", encoding="utf-8")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    profile = tmp_path / "profile.json"
    write_json(profile, {"profile": "test"})
    identity = {
        "schema_version": "1.1.0",
        "upstream_sha": "a" * 40,
        "engine_sha": "b" * 40,
        "freqtrade_digest": REFERENCE_INDEX_DIGEST,
        "semantic_profile_sha256": "d" * 64,
        "source_sha256": source_sha256,
    }
    target = {
        "id": "f" * 64,
        "kind": "tag",
        "change": "added",
        "value": "unregistered-route",
        "methods": ["populate_entry_trend"],
        "tags": ["unregistered-route"],
        "runtime_observable": True,
    }
    difference = {
        "schema_version": "1.2.0",
        "classification": "ir-compatible",
        "new": {"sha256": source_sha256},
        "changes": {"opcodes": {"added": [], "removed": []}},
        "behavior_targets": [target],
    }
    compatibility: dict[str, dict[str, Any]] = {}
    targeted: dict[str, dict[str, Any]] = {}
    for mode in ("spot", "futures"):
        compatibility[mode] = {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source": {"sha256": source_sha256},
            "native_compatible": True,
            "blockers": [],
        }
        qualification = {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "strategy_sha256": source_sha256,
            "verification_state": "latest_checked",
            "changed_branch_reached": False,
            "trade_surface_exact": None,
            "full_state_exact": None,
            "blockers": [
                {"code": "CHANGED_BRANCH_PROOF_REQUIRED", "message": "missing"}
            ],
        }
        targeted[mode] = {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source_sha256": source_sha256,
            "complete": False,
            "verification_state": "latest_checked",
            "blockers": list(qualification["blockers"]),
            "qualification": qualification,
        }
    return identity, difference, compatibility, targeted, source, profile


def _budget_clock():
    values = iter((0.0, 8000.0, 8000.0))
    return lambda: next(values, 8000.0)


def _bundles(
    tmp_path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Path],
    dict[str, Path],
]:
    identity, difference, compatibility, targeted, source, profile = _documents(tmp_path)
    bundles: dict[str, Path] = {}
    policies: dict[str, Path] = {}
    for mode in ("spot", "futures"):
        output = tmp_path / f"nfi-branch-discovery-{mode}"
        report = discover_targets(
            source,
            difference,
            compatibility[mode],
            FIXTURES,
            ROOT / "planning" / f"{mode}-discovery-policy.json",
            output,
            class_name="Demo",
            upstream_repository="iterativv/NostalgiaForInfinity",
            upstream_commit=identity["upstream_sha"],
            engine_commit=identity["engine_sha"],
            profile_path=profile,
            as_of=date(2026, 7, 30),
            scout_service=lambda _shard, _context: {
                "outcome": "miss",
                "message": "not reached",
                "target_ids": [],
            },
            clock=_budget_clock(),
        )
        assert report["status"] == "budget_exhausted"
        decision = classify_compatibility_automation(
            identity,
            difference,
            compatibility[mode],
            targeted[mode],
            discovery=report,
        )
        write_json(output / "automation-decision.json", decision)
        bundles[mode] = output
        policies[mode] = ROOT / "planning" / f"{mode}-discovery-policy.json"
    return identity, difference, compatibility, targeted, bundles, policies


def _authorize(tmp_path: Path) -> dict[str, Any]:
    identity, difference, compatibility, targeted, bundles, policies = _bundles(tmp_path)
    return authorize_discovery_publication(
        identity,
        difference,
        compatibility,
        targeted,
        bundles,
        policies,
        FIXTURES,
        source_run_id="12345",
    )


def test_blocked_product_can_authorize_paired_budget_progress(tmp_path: Path) -> None:
    authorization = _authorize(tmp_path)

    assert authorization["source_run_id"] == "12345"
    assert authorization["modes"]["spot"]["status"] == "budget_exhausted"
    assert authorization["modes"]["futures"]["automation_route"] == "bounded_discovery"
    assert len(authorization["authorization_fingerprint"]) == 64


def test_mutation_boundary_rejects_bundle_changed_after_authorization(
    tmp_path: Path,
) -> None:
    identity, difference, compatibility, targeted, bundles, policies = _bundles(tmp_path)
    authorization = authorize_discovery_publication(
        identity,
        difference,
        compatibility,
        targeted,
        bundles,
        policies,
        FIXTURES,
        source_run_id="12345",
    )
    report_path = bundles["spot"] / "discovery-report.json"
    report = read_json(report_path)
    report["message"] = "changed after authorization"
    write_json(report_path, report)

    with pytest.raises(SpecValidationError, match="differs from authorization"):
        validate_discovery_authorization(
            authorization,
            bundles,
            expected_identity=authorization["identity"],
            expected_source_run_id="12345",
        )


def test_cursor_tampering_rejects_publication(tmp_path: Path) -> None:
    identity, difference, compatibility, targeted, bundles, policies = _bundles(tmp_path)
    cursor_path = bundles["spot"] / "cursor.json"
    cursor = read_json(cursor_path)
    cursor["next_shard"] = 1
    write_json(cursor_path, cursor)

    with pytest.raises(SpecValidationError, match="cursor differs"):
        authorize_discovery_publication(
            identity,
            difference,
            compatibility,
            targeted,
            bundles,
            policies,
            FIXTURES,
            source_run_id="12345",
        )


def test_decision_tampering_rejects_publication(tmp_path: Path) -> None:
    identity, difference, compatibility, targeted, bundles, policies = _bundles(tmp_path)
    decision_path = bundles["futures"] / "automation-decision.json"
    decision = read_json(decision_path)
    decision["automation_route"] = "native_exact"
    write_json(decision_path, decision)

    with pytest.raises(SpecValidationError, match="decision differs"):
        authorize_discovery_publication(
            identity,
            difference,
            compatibility,
            targeted,
            bundles,
            policies,
            FIXTURES,
            source_run_id="12345",
        )


def test_cross_mode_bundle_rejects_publication(tmp_path: Path) -> None:
    identity, difference, compatibility, targeted, bundles, policies = _bundles(tmp_path)
    bundles["spot"] = bundles["futures"]

    with pytest.raises(SpecValidationError, match="request differs"):
        authorize_discovery_publication(
            identity,
            difference,
            compatibility,
            targeted,
            bundles,
            policies,
            FIXTURES,
            source_run_id="12345",
        )
