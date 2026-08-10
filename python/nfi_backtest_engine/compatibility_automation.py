"""Deterministic routing for checked upstream compatibility results.

This module joins the static compiler check, targeted exact qualification, and
optional bounded discovery result.  It never implements or approves semantics;
it only chooses the next safe automation lane.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError

COMPATIBILITY_AUTOMATION_VERSION = "1.0.0"
_MODES = {"spot", "futures"}
_CLASSIFICATIONS = {"vector-only", "ir-compatible", "stateful-review"}
_DISCOVERY_STATES = {
    "no_gap",
    "candidate_found",
    "budget_exhausted",
    "coverage_exhausted",
    "unsupported_semantics",
    "external_data_deferred",
    "infrastructure_failed",
}
_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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

    checked_identity = _identity(_document(identity, "compatibility identity"))
    difference = _document(strategy_diff, "strategy diff")
    static = _document(compatibility, "compatibility report")
    targeted_report = _document(targeted, "targeted report")
    discovery_report = (
        _document(discovery, "discovery report") if discovery is not None else None
    )

    source_sha256 = _source_sha(difference)
    if checked_identity["strategy_sha256"] != source_sha256:
        raise SpecValidationError("strategy diff and compatibility identity source differ")
    mode = _mode(static.get("trading_mode"), "compatibility report")
    _validate_static(static, mode=mode, source_sha256=source_sha256)
    qualification = _validate_targeted(
        targeted_report,
        mode=mode,
        source_sha256=source_sha256,
    )
    if discovery_report is not None:
        _validate_discovery(
            discovery_report,
            mode=mode,
            identity=checked_identity,
            source_sha256=source_sha256,
        )

    classification = difference.get("classification")
    if classification not in _CLASSIFICATIONS:
        raise SpecValidationError("strategy diff classification is unsupported")
    added_opcodes = _added_opcodes(difference)
    targets = _target_identities(difference)
    blockers = _blockers(static, targeted_report)
    static_compatible = static.get("native_compatible") is True
    exact = _is_exact(targeted_report, qualification) and static_compatible
    discovery_status = (
        str(discovery_report["status"]) if discovery_report is not None else None
    )

    route: str
    review_kind: str | None = None
    if exact:
        route = "native_exact"
    elif not static_compatible:
        route = "semantic_review_draft_pr"
        review_kind = "new_opcode" if added_opcodes else "generic_lowering"
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
    draft_kind = (
        review_kind
        if route == "semantic_review_draft_pr"
        else "exact_fixture"
        if route == "exact_fixture_draft_pr"
        else None
    )
    action_identity = {
        "trading_mode": mode,
        "route": route,
        "review_kind": review_kind,
        "classification": classification,
        "added_opcodes": added_opcodes,
        "target_ids": [target["id"] for target in targets],
        "blockers": blockers,
        "discovery_fingerprint": (
            discovery_report.get("fingerprint")
            if discovery_report is not None
            else None
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
        "trading_mode": mode,
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
        "added_opcodes": added_opcodes,
        "behavior_targets": targets,
        "blockers": blockers,
        "action": {
            "native_promotion_allowed": native_allowed,
            "bounded_discovery_required": route == "bounded_discovery",
            "draft_pr_allowed": draft_kind is not None,
            "draft_pr_kind": draft_kind,
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


def _identity(document: Mapping[str, Any]) -> dict[str, str]:
    if document.get("schema_version") != "1.1.0":
        raise SpecValidationError("compatibility identity schema must be 1.1.0")
    return {
        "upstream_sha": _token(document.get("upstream_sha"), _SHA, "upstream SHA"),
        "engine_sha": _token(document.get("engine_sha"), _SHA, "engine SHA"),
        "freqtrade_digest": _token(
            document.get("freqtrade_digest"), _DIGEST, "Freqtrade digest"
        ),
        "semantic_profile_sha256": _token(
            document.get("semantic_profile_sha256"),
            _SHA256,
            "semantic profile SHA-256",
        ),
        "strategy_sha256": _token(
            document.get("source_sha256"), _SHA256, "strategy SHA-256"
        ),
    }


def _source_sha(difference: Mapping[str, Any]) -> str:
    new = difference.get("new")
    value = new.get("sha256") if isinstance(new, Mapping) else None
    return _token(value, _SHA256, "strategy diff source SHA-256")


def _validate_static(
    report: Mapping[str, Any],
    *,
    mode: str,
    source_sha256: str,
) -> None:
    source = report.get("source")
    if report.get("schema_version") != "1.0.0":
        raise SpecValidationError("compatibility report schema is unsupported")
    if report.get("trading_mode") != mode:
        raise SpecValidationError("compatibility trading mode changed")
    if not isinstance(source, Mapping) or source.get("sha256") != source_sha256:
        raise SpecValidationError("compatibility report strategy source differs")
    if not isinstance(report.get("native_compatible"), bool):
        raise SpecValidationError("compatibility report lacks a completed outcome")
    _require_blocker_array(report, "compatibility report")


def _validate_targeted(
    report: Mapping[str, Any],
    *,
    mode: str,
    source_sha256: str,
) -> Mapping[str, Any]:
    if report.get("schema_version") != "1.0.0":
        raise SpecValidationError("targeted report schema is unsupported")
    if report.get("trading_mode") != mode:
        raise SpecValidationError("targeted report trading mode differs")
    if report.get("source_sha256") != source_sha256:
        raise SpecValidationError("targeted report strategy source differs")
    if not isinstance(report.get("complete"), bool):
        raise SpecValidationError("targeted report lacks a completed outcome")
    _require_blocker_array(report, "targeted report")
    qualification = report.get("qualification")
    if not isinstance(qualification, Mapping):
        raise SpecValidationError("targeted report has no qualification")
    if qualification.get("schema_version") != "1.0.0":
        raise SpecValidationError("qualification schema is unsupported")
    if qualification.get("strategy_sha256") != source_sha256:
        raise SpecValidationError("qualification strategy source differs")
    if qualification.get("verification_state") not in {
        "latest_checked",
        "quick_verified",
    }:
        raise SpecValidationError("qualification verification state is invalid")
    if report.get("verification_state") != qualification.get("verification_state"):
        raise SpecValidationError("targeted and qualification states differ")
    _require_blocker_array(qualification, "qualification")
    return qualification


def _validate_discovery(
    report: Mapping[str, Any],
    *,
    mode: str,
    identity: Mapping[str, str],
    source_sha256: str,
) -> None:
    status = report.get("status")
    if report.get("schema_version") != "1.0.0" or status not in _DISCOVERY_STATES:
        raise SpecValidationError("discovery report schema or status is unsupported")
    if report.get("trading_mode") != mode:
        raise SpecValidationError("discovery report trading mode differs")
    expected = {
        "upstream_commit": identity["upstream_sha"],
        "engine_commit": identity["engine_sha"],
        "freqtrade_image_digest": identity["freqtrade_digest"],
        "strategy_sha256": source_sha256,
    }
    if any(report.get(field) != value for field, value in expected.items()):
        raise SpecValidationError("discovery report identity differs")
    if status == "candidate_found":
        candidate = report.get("candidate")
        if (
            not isinstance(candidate, Mapping)
            or candidate.get("trade_surface_exact") is not True
            or candidate.get("full_state_exact") is not True
        ):
            raise SpecValidationError("discovery candidate lacks independent exact proof")


def _is_exact(
    targeted: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> bool:
    return bool(
        targeted.get("complete") is True
        and targeted.get("verification_state") == "quick_verified"
        and qualification.get("verification_state") == "quick_verified"
        and qualification.get("changed_branch_reached") is True
        and qualification.get("trade_surface_exact") is True
        and qualification.get("full_state_exact") is True
        and not qualification.get("blockers")
    )


def _added_opcodes(difference: Mapping[str, Any]) -> list[str]:
    changes = difference.get("changes")
    opcodes = changes.get("opcodes") if isinstance(changes, Mapping) else None
    added = opcodes.get("added") if isinstance(opcodes, Mapping) else None
    if not isinstance(added, list) or not all(isinstance(value, str) for value in added):
        raise SpecValidationError("strategy diff added opcodes are invalid")
    return sorted(set(added))


def _target_identities(difference: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets = difference.get("behavior_targets")
    if not isinstance(targets, list):
        raise SpecValidationError("strategy diff behavior targets are invalid")
    result = []
    for target in targets:
        if not isinstance(target, Mapping):
            raise SpecValidationError("strategy diff behavior target is invalid")
        target_id = _token(target.get("id"), _SHA256, "behavior target id")
        kind = target.get("kind")
        change = target.get("change")
        if not isinstance(kind, str) or change not in {"added", "removed", "changed"}:
            raise SpecValidationError("strategy diff behavior target identity is invalid")
        result.append(
            {
                "id": target_id,
                "kind": kind,
                "change": change,
                "runtime_observable": target.get("runtime_observable") is True,
            }
        )
    return sorted(result, key=lambda item: item["id"])


def _blockers(*reports: Mapping[str, Any]) -> list[dict[str, str]]:
    unique = {
        (str(item.get("code", "UNKNOWN")), str(item.get("message", "")))
        for report in reports
        for item in report.get("blockers", [])
        if isinstance(item, Mapping)
    }
    return [
        {"code": code, "message": message}
        for code, message in sorted(unique)
    ]


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
    if route == "semantic_review_draft_pr":
        subject = "new generic opcode" if review_kind == "new_opcode" else "generic lowering"
        return f"Static lowering is blocked; open a {subject} review Draft PR."
    return messages[route]


def _document(
    value: Mapping[str, Any] | str | Path,
    label: str,
) -> dict[str, Any]:
    document = read_json(value) if isinstance(value, str | Path) else dict(value)
    if not isinstance(document, dict):
        raise SpecValidationError(f"{label} must be an object")
    return document


def _mode(value: Any, label: str) -> str:
    if value not in _MODES:
        raise SpecValidationError(f"{label} trading mode is invalid")
    return str(value)


def _require_blocker_array(report: Mapping[str, Any], label: str) -> None:
    if not isinstance(report.get("blockers"), list):
        raise SpecValidationError(f"{label} blockers must be an array")


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
