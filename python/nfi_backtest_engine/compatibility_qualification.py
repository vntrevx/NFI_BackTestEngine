"""Conservative verification-state promotion for upstream compatibility checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError

COMPATIBILITY_QUALIFICATION_VERSION = "1.0.0"


def qualify_compatibility(
    compatibility: Mapping[str, Any] | str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    *,
    branch_proof: Mapping[str, Any] | str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Promote only branch-reaching, full-state-exact official fixtures."""

    check = _document(compatibility, "compatibility")
    difference = _document(strategy_diff, "strategy diff")
    proof = (
        _document(branch_proof, "branch proof")
        if branch_proof is not None
        else None
    )
    blockers: list[dict[str, str]] = []
    if check.get("native_compatible") is not True:
        blockers.append(
            {
                "code": "NATIVE_COMPATIBILITY_BLOCKED",
                "message": "static Native compatibility did not pass",
            }
        )
    if proof is None:
        blockers.append(
            {
                "code": "CHANGED_BRANCH_PROOF_REQUIRED",
                "message": "no bounded official fixture proof was supplied",
            }
        )
    else:
        requirements = {
            "complete": True,
            "changed_branch_reached": True,
            "trade_surface_exact": True,
            "full_state_exact": True,
        }
        for field, expected in requirements.items():
            if proof.get(field) is not expected:
                blockers.append(
                    {
                        "code": f"{field.upper()}_REQUIRED",
                        "message": f"branch proof requires {field}=true",
                    }
                )
    report = {
        "schema_version": COMPATIBILITY_QUALIFICATION_VERSION,
        "strategy_sha256": _source_sha(check),
        "classification": difference.get("classification"),
        "verification_state": "quick_verified" if not blockers else "latest_checked",
        "changed_branch_reached": (
            proof.get("changed_branch_reached") if proof is not None else False
        ),
        "trade_surface_exact": (
            proof.get("trade_surface_exact") if proof is not None else None
        ),
        "full_state_exact": (
            proof.get("full_state_exact") if proof is not None else None
        ),
        "blockers": blockers,
    }
    if output_path is not None:
        write_json(output_path, report)
    return report


def _document(
    value: Mapping[str, Any] | str | Path,
    label: str,
) -> dict[str, Any]:
    document = (
        read_json(value)
        if isinstance(value, str | Path)
        else dict(value)
    )
    if not isinstance(document, dict):
        raise SpecValidationError(f"{label} must be an object")
    return document


def _source_sha(check: Mapping[str, Any]) -> str:
    source = check.get("source")
    value = source.get("sha256") if isinstance(source, Mapping) else None
    if not isinstance(value, str) or len(value) != 64:
        raise SpecValidationError("compatibility source SHA-256 is invalid")
    return value
