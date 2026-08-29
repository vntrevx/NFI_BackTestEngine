from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, assert_never

import pytest
from changed_target_ledger_support import (
    HEAD,
    ORACLE,
    PROFILE,
    SOURCE_SHA,
)
from changed_target_ledger_support import (
    _documents as ledger_documents,
)
from jsonschema import Draft202012Validator
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.changed_target_ledger import _sha256_json, build_changed_target_ledger
from nfi_backtest_engine.compatibility_automation import classify_compatibility_automation
from nfi_backtest_engine.compatibility_proof import (
    CompatibilityProof,
    UnavailableCompatibilityProof,
    load_compatibility_proof,
    seal_compatibility_proof,
)
from nfi_backtest_engine.compatibility_status import (
    CompatibilityRunObservation,
    DiscoveryExecution,
    WorkflowExecution,
    classify_compatibility_status,
)


def _authoritative_proof(
    tmp_path: Path,
) -> tuple[dict, dict[str, dict], CompatibilityProof, Path]:
    root = tmp_path / "authoritative"
    root.mkdir()
    sources = ledger_documents(root)
    identity = {
        "schema_version": "1.1.0",
        "upstream_sha": HEAD,
        "engine_sha": "a" * 40,
        "freqtrade_digest": ORACLE,
        "semantic_profile_sha256": PROFILE,
        "source_sha256": SOURCE_SHA,
    }
    difference = read_json(sources.strategy_diff)
    decisions = {}
    for mode, path in sources.targeted_reports.items():
        targeted = read_json(path)
        targeted["qualification"].update(
            {"schema_version": "1.0.0", "strategy_sha256": SOURCE_SHA}
        )
        write_json(path, targeted)
        report = {
            "schema_version": "1.0.0",
            "trading_mode": mode,
            "source": {"sha256": SOURCE_SHA},
            "native_compatible": True,
            "blockers": [],
        }
        decision = classify_compatibility_automation(identity, difference, report, targeted)
        decisions[mode] = decision
        write_json(root / f"report-{mode}.json", report)
        write_json(root / f"targeted-report-{mode}.json", targeted)
        write_json(root / f"qualification-{mode}.json", targeted["qualification"])
        write_json(root / f"automation-decision-{mode}.json", decision)
    write_json(root / "changed-target-ledger.json", build_changed_target_ledger(sources))
    write_json(root / "compatibility-identity.json", identity)
    write_json(root / "strategy-diff.json", difference)
    write_json(root / "semantic-profile.json", {"fingerprint": PROFILE})
    canary = {
        "schema_version": "compatibility-hosted-canary-v1",
        "identity": {
            "nfi_upstream_sha": HEAD,
            "engine_sha": identity["engine_sha"],
            "freqtrade_digest": ORACLE,
            "semantic_profile_sha256": PROFILE,
        },
        "complete": True,
        "identity_advancement_allowed": True,
        "modes": [
            {
                "trading_mode": mode,
                "automation_route": decisions[mode]["automation_route"],
                "execution_route": decisions[mode]["execution_route"],
                "action_fingerprint": decisions[mode]["action_fingerprint"],
            }
            for mode in ("spot", "futures")
        ],
    }
    canary["fingerprint"] = hashlib.sha256(
        json.dumps(canary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    write_json(root / "hosted-canary.json", canary)
    source_run_id = "4242"
    write_json(
        root / "source-run.json",
        {
            "schema_version": "compatibility-source-run-v1",
            "source_run_id": source_run_id,
            "identity": {
                "upstream_sha": HEAD,
                "engine_sha": identity["engine_sha"],
                "freqtrade_digest": ORACLE,
                "semantic_profile_sha256": PROFILE,
                "source_sha256": SOURCE_SHA,
            },
        },
    )
    seal_compatibility_proof(root, source_run_id)
    return (
        identity,
        decisions,
        load_compatibility_proof(root, expected_source_run_id=source_run_id),
        root,
    )


@pytest.mark.parametrize(
    "reference",
    ["manifest", "qualification", "ledger", "source-run", "decision"],
)
def test_authoritative_recovery_rejects_crossed_or_incomplete_reference(
    tmp_path: Path,
    reference: Literal["manifest", "qualification", "ledger", "source-run", "decision"],
) -> None:
    # Given
    _identity, _decisions, _proof, root = _authoritative_proof(tmp_path)
    match reference:
        case "manifest":
            manifest = read_json(root / "compatibility-proof-manifest.json")
            manifest["artifacts"].pop()
            write_json(root / "compatibility-proof-manifest.json", manifest)
        case "qualification":
            qualification = read_json(root / "qualification-spot.json")
            qualification["verification_state"] = "latest_checked"
            write_json(root / "qualification-spot.json", qualification)
            seal_compatibility_proof(root, "4242")
        case "ledger":
            ledger = read_json(root / "changed-target-ledger.json")
            ledger["summary"]["native_promotion_allowed"] = False
            ledger["fingerprint"] = _sha256_json(
                {key: value for key, value in ledger.items() if key != "fingerprint"}
            )
            write_json(root / "changed-target-ledger.json", ledger)
            seal_compatibility_proof(root, "4242")
        case "source-run":
            source_run = read_json(root / "source-run.json")
            source_run["identity"]["engine_sha"] = "9" * 40
            write_json(root / "source-run.json", source_run)
            seal_compatibility_proof(root, "4242")
        case "decision":
            decision = read_json(root / "automation-decision-spot.json")
            decision["verification"]["exact"] = False
            write_json(root / "automation-decision-spot.json", decision)
            seal_compatibility_proof(root, "4242")
        case unreachable:
            assert_never(unreachable)

    # When
    proof = load_compatibility_proof(root, expected_source_run_id="4242")

    # Then
    assert isinstance(proof, UnavailableCompatibilityProof)


def test_status_recovers_when_same_engine_identity_is_fully_proven(tmp_path: Path) -> None:
    # Given
    identity, decisions, proof, _root = _authoritative_proof(tmp_path)
    observation = CompatibilityRunObservation(
        identity["engine_sha"],
        identity["upstream_sha"],
        WorkflowExecution.SUCCEEDED,
        {"spot": DiscoveryExecution.NOT_REQUIRED, "futures": DiscoveryExecution.NOT_REQUIRED},
    )

    # When
    status = classify_compatibility_status(
        identity,
        decisions,
        observation,
        authoritative_proof=proof,
    )

    # Then
    assert status["product"] == {
        "state": "compatible",
        "reason": "same_engine_proof_complete",
    }
    assert status["same_engine_proof"]["source_run_id"] == "4242"
    assert len(status["same_engine_proof"]["artifact_sha256"]) == 14
    assert status["required_status_passed"] is True
    schema_path = (
        Path(__file__).parents[1]
        / "python/nfi_backtest_engine/schemas/compatibility-product-status-v1.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(status)
