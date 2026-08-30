"""Identity-bound required status for NFI product compatibility."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, assert_never

from .canonical import read_json, write_json
from .compatibility_decision_proof import is_valid_exact_decision
from .compatibility_proof import (
    CompatibilityProof,
    UnavailableCompatibilityProof,
    VerifiedCompatibilityProof,
)
from .errors import SpecValidationError

COMPATIBILITY_STATUS_VERSION: Final = "compatibility-product-status-v1"
_REQUIRED_MODES: Final = ("spot", "futures")
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class WorkflowExecution(StrEnum):
    """Infrastructure execution outcome, independent of product compatibility."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INFRASTRUCTURE_LIMITED = "infrastructure_limited"
    STALE = "stale"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class DiscoveryExecution(StrEnum):
    """Observed execution of one mode's discovery job."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEFERRED = "external_data_deferred"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True, slots=True)
class CompatibilityRunObservation:
    """Current refs and workflow state observed outside proof artifacts."""

    current_engine_sha: str
    current_upstream_sha: str
    workflow_execution: WorkflowExecution
    discovery_execution: Mapping[str, DiscoveryExecution]


def classify_compatibility_status(
    identity: Mapping[str, Any] | str | Path,
    decisions: Mapping[str, Mapping[str, Any]],
    observation: CompatibilityRunObservation,
    *,
    authoritative_proof: CompatibilityProof | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Emit an identity-bound required status that fails closed without proof."""

    checked_identity = _identity(_document(identity))
    current_engine_sha = _token(observation.current_engine_sha, _SHA, "current engine SHA")
    current_upstream_sha = _token(
        observation.current_upstream_sha,
        _SHA,
        "current upstream SHA",
    )
    product_state = "inconclusive"
    reason = "missing_artifacts"
    proof_complete = False
    match authoritative_proof:
        case None:
            verified_proof = None
            proof_failure_reason = "missing_authoritative_proof"
        case UnavailableCompatibilityProof(reason=proof_reason):
            verified_proof = None
            proof_failure_reason = proof_reason.value
        case VerifiedCompatibilityProof() as proof:
            verified_proof = proof
            proof_failure_reason = None
        case unreachable:
            assert_never(unreachable)
    match observation.workflow_execution:
        case WorkflowExecution.SUCCEEDED:
            execution_reason = None
        case WorkflowExecution.FAILED:
            execution_reason = "workflow_failed"
        case WorkflowExecution.INFRASTRUCTURE_LIMITED:
            execution_reason = "infrastructure_limited"
        case WorkflowExecution.STALE:
            execution_reason = "stale_trigger"
        case WorkflowExecution.CANCELLED:
            execution_reason = "workflow_cancelled"
        case WorkflowExecution.SKIPPED:
            execution_reason = "workflow_skipped"
        case unreachable:
            assert_never(unreachable)
    automation_routes = {
        str(decision.get("automation_route"))
        for decision in decisions.values()
        if isinstance(decision, Mapping)
    }

    if checked_identity["engine_sha"] != current_engine_sha:
        reason = "stale_engine"
    elif checked_identity["upstream_sha"] != current_upstream_sha:
        reason = "stale_upstream"
    elif execution_reason is not None:
        reason = execution_reason
    elif DiscoveryExecution.DEFERRED in observation.discovery_execution.values():
        product_state = "blocked"
        reason = "external_data_deferred"
    elif DiscoveryExecution.CANCELLED in observation.discovery_execution.values():
        reason = "workflow_cancelled"
    elif DiscoveryExecution.FAILED in observation.discovery_execution.values():
        reason = "workflow_failed"
    elif proof_failure_reason == "malformed_artifacts":
        reason = proof_failure_reason
    elif any(mode not in decisions for mode in _REQUIRED_MODES):
        reason = "missing_artifacts"
    elif not all(
        is_valid_exact_decision(decisions[mode], mode, checked_identity)
        or decisions[mode].get("automation_route") != "native_exact"
        for mode in _REQUIRED_MODES
    ):
        reason = "invalid_proof"
    elif any(
        decisions[mode].get("automation_route") == "bounded_discovery"
        and observation.discovery_execution.get(mode) is DiscoveryExecution.SKIPPED
        for mode in _REQUIRED_MODES
    ):
        product_state = "blocked"
        reason = "discovery_skipped"
    elif "semantic_review_issue" in automation_routes:
        product_state = "blocked"
        reason = "semantic_review_required"
    elif "external_data_deferred" in automation_routes:
        product_state = "blocked"
        reason = "external_data_deferred"
    elif "bounded_discovery" in automation_routes:
        product_state = "blocked"
        reason = "bounded_discovery_required"
    elif "exact_fixture_draft_pr" in automation_routes:
        product_state = "blocked"
        reason = "exact_fixture_review_required"
    elif "official_only" in automation_routes:
        product_state = "blocked"
        reason = "native_exactness_unproven"
    elif proof_failure_reason is not None:
        reason = proof_failure_reason
    elif verified_proof is None or verified_proof.decisions != decisions:
        reason = "invalid_authoritative_proof"
    elif all(
        is_valid_exact_decision(decisions[mode], mode, checked_identity)
        for mode in _REQUIRED_MODES
    ):
        product_state = "compatible"
        reason = "same_engine_proof_complete"
        proof_complete = True
    else:
        product_state = "blocked"
        reason = "branch_proof_missing"

    status: dict[str, Any] = {
        "schema_version": COMPATIBILITY_STATUS_VERSION,
        "identity": checked_identity,
        "observed_identity": {
            "engine_sha": current_engine_sha,
            "upstream_sha": current_upstream_sha,
        },
        "workflow": {"state": observation.workflow_execution.value},
        "product": {"state": product_state, "reason": reason},
        "discovery_execution": {
            mode: observation.discovery_execution.get(
                mode,
                DiscoveryExecution.SKIPPED,
            ).value
            for mode in _REQUIRED_MODES
        },
        "same_engine_proof": {
            "complete": proof_complete,
            "engine_sha": checked_identity["engine_sha"],
            "source_run_id": (
                verified_proof.source_run_id
                if verified_proof is not None
                else None
            ),
            "manifest_sha256": (
                verified_proof.manifest_sha256
                if verified_proof is not None
                else None
            ),
            "artifact_sha256": (
                dict(verified_proof.artifact_sha256)
                if verified_proof is not None
                else {}
            ),
        },
        "required_status_passed": proof_complete,
    }
    status["fingerprint"] = _canonical_sha256(status)
    if output_path is not None:
        write_json(output_path, status)
    return status


def _identity(document: Mapping[str, Any]) -> dict[str, str]:
    if document.get("schema_version") != "1.1.0":
        raise SpecValidationError("compatibility identity schema must be 1.1.0")
    return {
        "upstream_sha": _token(document.get("upstream_sha"), _SHA, "upstream SHA"),
        "engine_sha": _token(document.get("engine_sha"), _SHA, "engine SHA"),
        "freqtrade_digest": _token(document.get("freqtrade_digest"), _DIGEST, "Freqtrade digest"),
        "semantic_profile_sha256": _token(
            document.get("semantic_profile_sha256"),
            _SHA256,
            "semantic profile SHA-256",
        ),
        "strategy_sha256": _token(document.get("source_sha256"), _SHA256, "strategy SHA-256"),
    }


def _document(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    document = read_json(value) if isinstance(value, str | Path) else dict(value)
    if not isinstance(document, dict):
        raise SpecValidationError("compatibility identity must be an object")
    return document


def _token(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SpecValidationError(f"{label} is invalid")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
