from __future__ import annotations

import copy

import pytest
from nfi_backtest_engine.compatibility_automation import classify_compatibility_automation
from nfi_backtest_engine.compatibility_status import (
    CompatibilityRunObservation,
    DiscoveryExecution,
    WorkflowExecution,
    classify_compatibility_status,
)


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
        "trading_mode": "futures",
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


def _exact_decisions() -> tuple[dict, dict[str, dict]]:
    identity, difference, compatibility, targeted = _documents()
    targeted["qualification"].update(
        {
            "verification_state": "quick_verified",
            "changed_branch_reached": True,
            "trade_surface_exact": True,
            "full_state_exact": True,
            "blockers": [],
        }
    )
    targeted.update({"complete": True, "verification_state": "quick_verified", "blockers": []})
    futures = classify_compatibility_automation(identity, difference, compatibility, targeted)
    compatibility["trading_mode"] = "spot"
    targeted["trading_mode"] = "spot"
    targeted["qualification"]["trading_mode"] = "spot"
    spot = classify_compatibility_automation(identity, difference, compatibility, targeted)
    return identity, {"spot": spot, "futures": futures}


@pytest.mark.parametrize(
    ("observation", "expected_reason"),
    [
        (
            CompatibilityRunObservation(
                "9" * 40,
                "a" * 40,
                WorkflowExecution.SUCCEEDED,
                {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
            ),
            "stale_engine",
        ),
        (
            CompatibilityRunObservation(
                "b" * 40,
                "9" * 40,
                WorkflowExecution.SUCCEEDED,
                {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
            ),
            "stale_upstream",
        ),
        (
            CompatibilityRunObservation(
                "b" * 40,
                "a" * 40,
                WorkflowExecution.INFRASTRUCTURE_LIMITED,
                {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
            ),
            "infrastructure_limited",
        ),
        (
            CompatibilityRunObservation(
                "b" * 40,
                "a" * 40,
                WorkflowExecution.STALE,
                {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
            ),
            "stale_trigger",
        ),
    ],
)
def test_status_is_inconclusive_when_identity_or_infrastructure_is_untrusted(
    observation: CompatibilityRunObservation,
    expected_reason: str,
) -> None:
    # Given
    identity, decisions = _exact_decisions()

    # When
    status = classify_compatibility_status(identity, decisions, observation)

    # Then
    assert status["product"] == {"state": "inconclusive", "reason": expected_reason}
    assert status["required_status_passed"] is False


def test_status_is_blocked_when_required_discovery_was_skipped() -> None:
    # Given
    identity, difference, compatibility, targeted = _documents()
    futures = classify_compatibility_automation(identity, difference, compatibility, targeted)
    compatibility["trading_mode"] = "spot"
    targeted["trading_mode"] = "spot"
    targeted["qualification"]["trading_mode"] = "spot"
    decisions = {
        "spot": classify_compatibility_automation(identity, difference, compatibility, targeted),
        "futures": futures,
    }
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
    )

    # When
    status = classify_compatibility_status(identity, decisions, observation)

    # Then
    assert status["workflow"]["state"] == "succeeded"
    assert status["product"] == {"state": "blocked", "reason": "discovery_skipped"}
    assert status["same_engine_proof"]["complete"] is False


def test_semantic_review_is_a_blocked_product_observation_not_infrastructure_failure() -> None:
    identity, difference, compatibility, targeted = _documents()
    compatibility.update(
        {
            "native_compatible": False,
            "blockers": [
                {
                    "code": "EXACT_LOWERING_REVIEW_REQUIRED",
                    "message": "system adjustment keys changed",
                }
            ],
        }
    )
    futures = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
    )
    compatibility["trading_mode"] = "spot"
    targeted["trading_mode"] = "spot"
    targeted["qualification"]["trading_mode"] = "spot"
    decisions = {
        "spot": classify_compatibility_automation(
            identity,
            difference,
            compatibility,
            targeted,
        ),
        "futures": futures,
    }
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {
            "spot": DiscoveryExecution.NOT_REQUIRED,
            "futures": DiscoveryExecution.NOT_REQUIRED,
        },
    )

    status = classify_compatibility_status(identity, decisions, observation)

    assert status["workflow"]["state"] == "succeeded"
    assert status["product"] == {
        "state": "blocked",
        "reason": "semantic_review_required",
    }
    assert status["required_status_passed"] is False


def test_status_fails_closed_when_artifact_is_missing_or_self_asserted_green() -> None:
    # Given
    identity, decisions = _exact_decisions()
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {"spot": DiscoveryExecution.SUCCEEDED, "futures": DiscoveryExecution.SUCCEEDED},
    )
    asserted_green = copy.deepcopy(decisions["spot"])
    asserted_green["action"]["external_data_deferred_is_exact"] = True

    # When
    missing = classify_compatibility_status(
        identity,
        {"futures": decisions["futures"]},
        observation,
    )
    forged = classify_compatibility_status(
        identity,
        {"spot": asserted_green, "futures": decisions["futures"]},
        observation,
    )

    # Then
    assert missing["product"]["reason"] == "missing_artifacts"
    assert missing["required_status_passed"] is False
    assert forged["product"]["reason"] == "invalid_proof"
    assert forged["required_status_passed"] is False
