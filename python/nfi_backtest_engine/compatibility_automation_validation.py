"""Boundary parsing and contract validation for compatibility automation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .canonical import read_json
from .errors import SpecValidationError

MODES: Final = {"spot", "futures"}
CLASSIFICATIONS: Final = {"vector-only", "ir-compatible", "stateful-review"}
DISCOVERY_STATES: Final = {
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


def parse_compatibility_identity(document: Mapping[str, Any]) -> dict[str, str]:
    """Parse one trusted five-part compatibility identity."""
    if document.get("schema_version") != "1.1.0":
        raise SpecValidationError("compatibility identity schema must be 1.1.0")
    return {
        "upstream_sha": _token(document.get("upstream_sha"), _SHA, "upstream SHA"),
        "engine_sha": _token(document.get("engine_sha"), _SHA, "engine SHA"),
        "freqtrade_digest": _token(
            document.get("freqtrade_digest"),
            _DIGEST,
            "Freqtrade digest",
        ),
        "semantic_profile_sha256": _token(
            document.get("semantic_profile_sha256"),
            _SHA256,
            "semantic profile SHA-256",
        ),
        "strategy_sha256": _token(
            document.get("source_sha256"),
            _SHA256,
            "strategy SHA-256",
        ),
    }


def document(value: Mapping[str, Any] | str | Path, label: str) -> dict[str, Any]:
    """Read one mapping input without allowing non-object JSON through."""
    parsed = read_json(value) if isinstance(value, str | Path) else dict(value)
    if not isinstance(parsed, dict):
        raise SpecValidationError(f"{label} must be an object")
    return parsed


def source_sha(difference: Mapping[str, Any]) -> str:
    new = difference.get("new")
    value = new.get("sha256") if isinstance(new, Mapping) else None
    return _token(value, _SHA256, "strategy diff source SHA-256")


def mode(value: Any, label: str) -> str:
    if value not in MODES:
        raise SpecValidationError(f"{label} trading mode is invalid")
    return str(value)


def validate_static(report: Mapping[str, Any], *, trading_mode: str, source: str) -> None:
    report_source = report.get("source")
    if report.get("schema_version") != "1.0.0":
        raise SpecValidationError("compatibility report schema is unsupported")
    if report.get("trading_mode") != trading_mode:
        raise SpecValidationError("compatibility trading mode changed")
    if not isinstance(report_source, Mapping) or report_source.get("sha256") != source:
        raise SpecValidationError("compatibility report strategy source differs")
    if not isinstance(report.get("native_compatible"), bool):
        raise SpecValidationError("compatibility report lacks a completed outcome")
    _require_blocker_array(report, "compatibility report")


def validate_targeted(
    report: Mapping[str, Any],
    *,
    trading_mode: str,
    source: str,
) -> Mapping[str, Any]:
    if report.get("schema_version") != "1.0.0":
        raise SpecValidationError("targeted report schema is unsupported")
    if report.get("trading_mode") != trading_mode:
        raise SpecValidationError("targeted report trading mode differs")
    if report.get("source_sha256") != source:
        raise SpecValidationError("targeted report strategy source differs")
    if not isinstance(report.get("complete"), bool):
        raise SpecValidationError("targeted report lacks a completed outcome")
    _require_blocker_array(report, "targeted report")
    qualification = report.get("qualification")
    if not isinstance(qualification, Mapping):
        raise SpecValidationError("targeted report has no qualification")
    if qualification.get("schema_version") != "1.0.0":
        raise SpecValidationError("qualification schema is unsupported")
    if qualification.get("trading_mode") != trading_mode:
        raise SpecValidationError("qualification trading mode differs")
    if qualification.get("strategy_sha256") != source:
        raise SpecValidationError("qualification strategy source differs")
    if qualification.get("verification_state") not in {"latest_checked", "quick_verified"}:
        raise SpecValidationError("qualification verification state is invalid")
    if report.get("verification_state") != qualification.get("verification_state"):
        raise SpecValidationError("targeted and qualification states differ")
    _require_blocker_array(qualification, "qualification")
    return qualification


def validate_discovery(
    report: Mapping[str, Any],
    *,
    trading_mode: str,
    identity: Mapping[str, str],
    source: str,
) -> None:
    status = report.get("status")
    if report.get("schema_version") != "1.0.0" or status not in DISCOVERY_STATES:
        raise SpecValidationError("discovery report schema or status is unsupported")
    if report.get("trading_mode") != trading_mode:
        raise SpecValidationError("discovery report trading mode differs")
    expected = {
        "upstream_commit": identity["upstream_sha"],
        "engine_commit": identity["engine_sha"],
        "freqtrade_image_digest": identity["freqtrade_digest"],
        "strategy_sha256": source,
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


def is_exact(targeted: Mapping[str, Any], qualification: Mapping[str, Any]) -> bool:
    return bool(
        targeted.get("complete") is True
        and targeted.get("verification_state") == "quick_verified"
        and qualification.get("verification_state") == "quick_verified"
        and qualification.get("changed_branch_reached") is True
        and qualification.get("trade_surface_exact") is True
        and qualification.get("full_state_exact") is True
        and not qualification.get("blockers")
    )


def added_opcodes(difference: Mapping[str, Any]) -> list[str]:
    changes = difference.get("changes")
    opcodes = changes.get("opcodes") if isinstance(changes, Mapping) else None
    added = opcodes.get("added") if isinstance(opcodes, Mapping) else None
    if not isinstance(added, list) or not all(isinstance(value, str) for value in added):
        raise SpecValidationError("strategy diff added opcodes are invalid")
    return sorted(set(added))


def target_identities(difference: Mapping[str, Any]) -> list[dict[str, Any]]:
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


def _require_blocker_array(report: Mapping[str, Any], label: str) -> None:
    if not isinstance(report.get("blockers"), list):
        raise SpecValidationError(f"{label} blockers must be an array")


def _token(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SpecValidationError(f"{label} is invalid")
    return value
