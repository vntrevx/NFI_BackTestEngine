"""Deterministic routing for checked upstream compatibility results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .canonical import write_json
from .compatibility_automation_validation import (
    CLASSIFICATIONS,
    added_opcodes,
    document,
    is_exact,
    mode,
    parse_compatibility_identity,
    source_sha,
    target_identities,
    validate_discovery,
    validate_static,
    validate_targeted,
)
from .errors import SpecValidationError

COMPATIBILITY_AUTOMATION_VERSION: Final = "1.0.0"


def classify_compatibility_automation(
    identity: Mapping[str, Any] | str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility: Mapping[str, Any] | str | Path,
    targeted: Mapping[str, Any] | str | Path,
    *,
    discovery: Mapping[str, Any] | str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Choose one fail-closed Native, review, discovery, or official lane."""
    checked_identity = parse_compatibility_identity(document(identity, "compatibility identity"))
    difference = document(strategy_diff, "strategy diff")
    static = document(compatibility, "compatibility report")
    targeted_report = document(targeted, "targeted report")
    discovery_report = document(discovery, "discovery report") if discovery is not None else None

    strategy_sha256 = source_sha(difference)
    if checked_identity["strategy_sha256"] != strategy_sha256:
        raise SpecValidationError("strategy diff and compatibility identity source differ")
    trading_mode = mode(static.get("trading_mode"), "compatibility report")
    validate_static(static, trading_mode=trading_mode, source=strategy_sha256)
    qualification = validate_targeted(
        targeted_report,
        trading_mode=trading_mode,
        source=strategy_sha256,
    )
    if discovery_report is not None:
        validate_discovery(
            discovery_report,
            trading_mode=trading_mode,
            identity=checked_identity,
            source=strategy_sha256,
        )

    classification = difference.get("classification")
    if classification not in CLASSIFICATIONS:
        raise SpecValidationError("strategy diff classification is unsupported")
    opcodes = added_opcodes(difference)
    targets = target_identities(difference)
    blockers = _blockers(static, targeted_report)
    static_compatible = static.get("native_compatible") is True
    exact = is_exact(targeted_report, qualification) and static_compatible
    discovery_status = str(discovery_report["status"]) if discovery_report is not None else None

    route: str
    review_kind: str | None = None
    if exact:
        route = "native_exact"
    elif not static_compatible:
        route = "semantic_review_issue"
        review_kind = "new_opcode" if opcodes else "generic_lowering"
    elif discovery_status is None or discovery_status == "budget_exhausted":
        route = "bounded_discovery"
    elif discovery_status == "candidate_found":
        route = "exact_fixture_draft_pr"
    elif discovery_status == "external_data_deferred":
        route = "external_data_deferred"
    elif discovery_status == "infrastructure_failed":
        route = "automation_failed"
    else:
        route = "official_only"

    native_allowed = route == "native_exact"
    draft_kind = "exact_fixture" if route == "exact_fixture_draft_pr" else None
    action_identity = {
        "trading_mode": trading_mode,
        "route": route,
        "review_kind": review_kind,
        "classification": classification,
        "added_opcodes": opcodes,
        "target_ids": [target["id"] for target in targets],
        "blockers": blockers,
        "discovery_fingerprint": (
            discovery_report.get("fingerprint") if discovery_report is not None else None
        ),
    }
    action_fingerprint = _canonical_sha256(action_identity)
    decision_identity = {
        "identity": checked_identity,
        "action_fingerprint": action_fingerprint,
        "verification_state": qualification.get("verification_state"),
        "discovery_status": discovery_status,
    }
    report: dict[str, Any] = {
        "schema_version": COMPATIBILITY_AUTOMATION_VERSION,
        "identity": checked_identity,
        "trading_mode": trading_mode,
        "strategy_classification": classification,
        "automation_route": route,
        "execution_route": "native" if native_allowed else "official_only",
        "review_kind": review_kind,
        "verification": {
            "state": qualification.get("verification_state"),
            "changed_branch_reached": qualification.get("changed_branch_reached", False),
            "trade_surface_exact": qualification.get("trade_surface_exact"),
            "full_state_exact": qualification.get("full_state_exact"),
            "exact": native_allowed,
        },
        "discovery_status": discovery_status,
        "added_opcodes": opcodes,
        "behavior_targets": targets,
        "blockers": blockers,
        "action": {
            "native_promotion_allowed": native_allowed,
            "bounded_discovery_required": route == "bounded_discovery",
            "draft_pr_allowed": draft_kind is not None,
            "draft_pr_kind": draft_kind,
            "issue_required": route == "semantic_review_issue",
            "official_fallback_available": True,
            "automatic_semantic_merge_allowed": False,
            "external_data_deferred_is_exact": False,
        },
        "action_fingerprint": action_fingerprint,
        "decision_fingerprint": _canonical_sha256(decision_identity),
        "message": _message(route, review_kind=review_kind),
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _blockers(*reports: Mapping[str, Any]) -> list[dict[str, str]]:
    unique = {
        (str(item.get("code", "UNKNOWN")), str(item.get("message", "")))
        for report in reports
        for item in report.get("blockers", [])
        if isinstance(item, Mapping)
    }
    return [{"code": code, "message": message} for code, message in sorted(unique)]


def _message(route: str, *, review_kind: str | None) -> str:
    messages = {
        "native_exact": "Changed behavior is independently exact and may run Native.",
        "bounded_discovery": "Exact branch evidence is missing; continue bounded discovery.",
        "exact_fixture_draft_pr": "An exact compact fixture may be proposed as a Draft PR.",
        "external_data_deferred": (
            "External data is deferred; keep official-only execution and reuse this state."
        ),
        "automation_failed": (
            "Discovery infrastructure failed; keep official-only execution and retry safely."
        ),
        "official_only": "Native exactness is unproven; use the announced official fallback.",
    }
    if route == "semantic_review_issue":
        subject = "new generic opcode" if review_kind == "new_opcode" else "generic lowering"
        return f"Static lowering is blocked; track the {subject} in the compatibility issue."
    return messages[route]


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
