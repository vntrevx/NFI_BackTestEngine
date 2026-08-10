from __future__ import annotations

import copy

import pytest
from nfi_backtest_engine.compatibility_automation import (
    classify_compatibility_automation,
)
from nfi_backtest_engine.errors import SpecValidationError


def _documents() -> tuple[dict, dict, dict, dict]:
    source = "e" * 64
    identity = {
        "schema_version": "1.1.0",
        "upstream_sha": "a" * 40,
        "engine_sha": "b" * 40,
        "freqtrade_digest": "sha256:" + "c" * 64,
        "semantic_profile_sha256": "d" * 64,
        "source_sha256": source,
    }
    difference = {
        "classification": "ir-compatible",
        "new": {"sha256": source},
        "changes": {"opcodes": {"added": [], "removed": []}},
        "behavior_targets": [
            {
                "id": "f" * 64,
                "kind": "callback",
                "change": "changed",
                "runtime_observable": True,
            }
        ],
    }
    compatibility = {
        "schema_version": "1.0.0",
        "trading_mode": "futures",
        "source": {"sha256": source},
        "native_compatible": True,
        "blockers": [],
    }
    qualification = {
        "schema_version": "1.0.0",
        "strategy_sha256": source,
        "verification_state": "latest_checked",
        "changed_branch_reached": False,
        "trade_surface_exact": None,
        "full_state_exact": None,
        "blockers": [{"code": "CHANGED_BRANCH_PROOF_REQUIRED", "message": "missing"}],
    }
    targeted = {
        "schema_version": "1.0.0",
        "trading_mode": "futures",
        "source_sha256": source,
        "complete": False,
        "verification_state": "latest_checked",
        "blockers": list(qualification["blockers"]),
        "qualification": qualification,
    }
    return identity, difference, compatibility, targeted


def _discovery(identity: dict, *, status: str) -> dict:
    candidate = (
        {"trade_surface_exact": True, "full_state_exact": True}
        if status == "candidate_found"
        else None
    )
    return {
        "schema_version": "1.0.0",
        "trading_mode": "futures",
        "status": status,
        "fingerprint": "1" * 64,
        "upstream_commit": identity["upstream_sha"],
        "engine_commit": identity["engine_sha"],
        "freqtrade_image_digest": identity["freqtrade_digest"],
        "strategy_sha256": identity["source_sha256"],
        "candidate": candidate,
    }


def test_ir_compatible_targeted_exact_is_the_only_native_promotion() -> None:
    identity, difference, compatibility, targeted = _documents()
    qualification = targeted["qualification"]
    qualification.update(
        {
            "verification_state": "quick_verified",
            "changed_branch_reached": True,
            "trade_surface_exact": True,
            "full_state_exact": True,
            "blockers": [],
        }
    )
    targeted.update(
        {"complete": True, "verification_state": "quick_verified", "blockers": []}
    )

    report = classify_compatibility_automation(
        identity, difference, compatibility, targeted
    )

    assert report["automation_route"] == "native_exact"
    assert report["execution_route"] == "native"
    assert report["action"]["native_promotion_allowed"] is True
    assert report["action"]["automatic_semantic_merge_allowed"] is False


def test_static_new_opcode_is_official_only_and_routes_to_review_draft() -> None:
    identity, difference, compatibility, targeted = _documents()
    difference["classification"] = "stateful-review"
    difference["changes"]["opcodes"]["added"] = ["call:new_behavior"]
    compatibility["native_compatible"] = False
    compatibility["blockers"] = [
        {"code": "EXACT_LOWERING_REVIEW_REQUIRED", "message": "review"}
    ]

    report = classify_compatibility_automation(
        identity, difference, compatibility, targeted
    )

    assert report["automation_route"] == "semantic_review_draft_pr"
    assert report["execution_route"] == "official_only"
    assert report["review_kind"] == "new_opcode"
    assert report["action"]["draft_pr_kind"] == "new_opcode"
    assert report["action"]["native_promotion_allowed"] is False


def test_static_lowering_change_routes_without_hardcoded_behavior_value() -> None:
    identity, difference, compatibility, targeted = _documents()
    difference["behavior_targets"] = []
    compatibility["native_compatible"] = False
    compatibility["blockers"] = [
        {"code": "EXACT_LOWERING_REVIEW_REQUIRED", "message": "source shape changed"}
    ]

    report = classify_compatibility_automation(
        identity, difference, compatibility, targeted
    )

    assert report["automation_route"] == "semantic_review_draft_pr"
    assert report["review_kind"] == "generic_lowering"
    assert report["action"]["bounded_discovery_required"] is False


def test_missing_exact_proof_routes_to_bounded_discovery_and_exact_candidate_pr() -> None:
    identity, difference, compatibility, targeted = _documents()
    initial = classify_compatibility_automation(
        identity, difference, compatibility, targeted
    )
    candidate = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=_discovery(identity, status="candidate_found"),
    )

    assert initial["automation_route"] == "bounded_discovery"
    assert initial["action"]["bounded_discovery_required"] is True
    assert candidate["automation_route"] == "exact_fixture_draft_pr"
    assert candidate["execution_route"] == "official_only"
    assert candidate["action"]["draft_pr_kind"] == "exact_fixture"


def test_external_deferral_is_stable_reusable_and_never_exact() -> None:
    identity, difference, compatibility, targeted = _documents()
    deferred = _discovery(identity, status="external_data_deferred")

    first = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=deferred,
    )
    second = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=copy.deepcopy(deferred),
    )

    assert first == second
    assert first["automation_route"] == "external_data_deferred"
    assert first["verification"]["exact"] is False
    assert first["action"]["external_data_deferred_is_exact"] is False


def test_terminal_discovery_gap_stays_official_only() -> None:
    identity, difference, compatibility, targeted = _documents()

    report = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=_discovery(identity, status="coverage_exhausted"),
    )

    assert report["automation_route"] == "official_only"
    assert report["execution_route"] == "official_only"


def test_inexact_candidate_and_cross_identity_reports_are_rejected() -> None:
    identity, difference, compatibility, targeted = _documents()
    candidate = _discovery(identity, status="candidate_found")
    candidate["candidate"]["full_state_exact"] = False
    with pytest.raises(SpecValidationError, match="independent exact proof"):
        classify_compatibility_automation(
            identity,
            difference,
            compatibility,
            targeted,
            discovery=candidate,
        )

    crossed = _discovery(identity, status="coverage_exhausted")
    crossed["engine_commit"] = "9" * 40
    with pytest.raises(SpecValidationError, match="identity differs"):
        classify_compatibility_automation(
            identity,
            difference,
            compatibility,
            targeted,
            discovery=crossed,
        )
