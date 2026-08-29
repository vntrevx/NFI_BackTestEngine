from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nfi_backtest_engine.compatibility_automation import classify_compatibility_automation
from nfi_backtest_engine.compatibility_status import (
    CompatibilityRunObservation,
    DiscoveryExecution,
    WorkflowExecution,
    classify_compatibility_status,
)
from test_compatibility_status import _documents, _exact_decisions


@pytest.mark.parametrize("payload", ["{not-json", "[]", "null"])
def test_cli_emits_typed_malformed_status_before_returning_failure(
    tmp_path: Path,
    payload: str,
) -> None:
    # Given
    identity, decisions = _exact_decisions()
    proof = tmp_path / "proof"
    proof.mkdir()
    (proof / "compatibility-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (proof / "automation-decision-spot.json").write_text(
        json.dumps(decisions["spot"]),
        encoding="utf-8",
    )
    (proof / "automation-decision-futures.json").write_text(payload, encoding="utf-8")
    output = tmp_path / "status.json"

    # When
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/compatibility_automation.py",
            "--identity",
            str(proof / "compatibility-identity.json"),
            "--decision-dir",
            str(proof),
            "--current-engine-sha",
            identity["engine_sha"],
            "--current-upstream-sha",
            identity["upstream_sha"],
            "--workflow-execution",
            "succeeded",
            "--spot-discovery-execution",
            "succeeded",
            "--futures-discovery-execution",
            "succeeded",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then
    status = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode != 0
    assert status["product"] == {"state": "inconclusive", "reason": "malformed_artifacts"}
    assert status["required_status_passed"] is False
    schema_path = (
        Path(__file__).parents[1]
        / "python/nfi_backtest_engine/schemas/compatibility-product-status-v1.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(status)


def test_cli_emits_typed_status_when_required_proof_file_is_missing(tmp_path: Path) -> None:
    # Given
    identity, decisions = _exact_decisions()
    proof = tmp_path / "proof"
    proof.mkdir()
    (proof / "compatibility-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (proof / "automation-decision-spot.json").write_text(
        json.dumps(decisions["spot"]),
        encoding="utf-8",
    )
    output = tmp_path / "status.json"

    # When
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/compatibility_automation.py",
            "--identity",
            str(proof / "compatibility-identity.json"),
            "--decision-dir",
            str(proof),
            "--proof-dir",
            str(proof),
            "--source-run-id",
            "7",
            "--seal-proof-manifest",
            "--current-engine-sha",
            identity["engine_sha"],
            "--current-upstream-sha",
            identity["upstream_sha"],
            "--workflow-execution",
            "succeeded",
            "--spot-discovery-execution",
            "succeeded",
            "--futures-discovery-execution",
            "succeeded",
            "--output",
            str(output),
        ],
        check=False,
    )

    # Then
    assert completed.returncode != 0
    status = json.loads(output.read_text(encoding="utf-8"))
    assert status["product"] == {"state": "inconclusive", "reason": "missing_artifacts"}
    assert status["required_status_passed"] is False


def test_fabricated_exact_decisions_without_authoritative_references_are_rejected() -> None:
    # Given
    identity, decisions = _exact_decisions()
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {"spot": DiscoveryExecution.NOT_REQUIRED, "futures": DiscoveryExecution.NOT_REQUIRED},
    )

    # When
    status = classify_compatibility_status(identity, decisions, observation)

    # Then
    assert status["product"] == {
        "state": "inconclusive",
        "reason": "missing_authoritative_proof",
    }
    assert status["required_status_passed"] is False


def test_external_data_deferred_has_explicit_blocked_product_reason() -> None:
    # Given
    identity, difference, compatibility, targeted = _documents()
    deferred = {
        "schema_version": "1.0.0",
        "trading_mode": "futures",
        "status": "external_data_deferred",
        "fingerprint": "1" * 64,
        "upstream_commit": identity["upstream_sha"],
        "engine_commit": identity["engine_sha"],
        "freqtrade_image_digest": identity["freqtrade_digest"],
        "strategy_sha256": identity["source_sha256"],
        "external_data": {"http_status": 451},
    }
    futures = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=deferred,
    )
    compatibility["trading_mode"] = "spot"
    targeted["trading_mode"] = "spot"
    targeted["qualification"]["trading_mode"] = "spot"
    deferred["trading_mode"] = "spot"
    spot = classify_compatibility_automation(
        identity,
        difference,
        compatibility,
        targeted,
        discovery=deferred,
    )
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {"spot": DiscoveryExecution.DEFERRED, "futures": DiscoveryExecution.DEFERRED},
    )

    # When
    status = classify_compatibility_status(
        identity,
        {"spot": spot, "futures": futures},
        observation,
    )

    # Then
    assert status["product"] == {"state": "blocked", "reason": "external_data_deferred"}
    assert status["required_status_passed"] is False


@pytest.mark.parametrize(
    ("execution", "reason"),
    [
        (WorkflowExecution.FAILED, "workflow_failed"),
        (WorkflowExecution.CANCELLED, "workflow_cancelled"),
        (WorkflowExecution.SKIPPED, "workflow_skipped"),
    ],
)
def test_non_success_workflow_conclusion_is_typed(
    execution: WorkflowExecution,
    reason: str,
) -> None:
    identity, decisions = _exact_decisions()
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        execution,
        {"spot": DiscoveryExecution.SKIPPED, "futures": DiscoveryExecution.SKIPPED},
    )

    status = classify_compatibility_status(identity, decisions, observation)

    assert status["product"] == {"state": "inconclusive", "reason": reason}
    assert status["required_status_passed"] is False
