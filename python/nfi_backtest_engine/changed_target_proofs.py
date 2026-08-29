"""Fixture and exact execution proof projection for changed targets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

_BASE_IDENTITY_FIELDS: Final = (
    "source_sha256",
    "freqtrade_digest",
    "semantic_profile_sha256",
    "semantic_registry_fingerprint",
)
_COMMIT_IDENTITY_FIELDS: Final = (*_BASE_IDENTITY_FIELDS, "upstream_commit")
_ORACLE_IDENTITY_FIELDS: Final = (*_COMMIT_IDENTITY_FIELDS, "oracle_digest")


@dataclass(frozen=True, slots=True)
class ModeProofInputs:
    """Identity-bound inputs for one target and trading mode proof join."""

    target: Mapping[str, Any]
    mode: str
    identity: Mapping[str, str]
    fixtures: Mapping[str, Any]
    report: Mapping[str, Any] | None


def mode_proof(inputs: ModeProofInputs) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Join one target to one mode's exact fixture, Oracle, and Native proof."""
    target_id = str(inputs.target["id"])
    mode = inputs.mode
    identity = inputs.identity
    fixtures = inputs.fixtures
    report = inputs.report
    mode_bundles = [
        bundle
        for bundle in fixtures.get("bundles", [])
        if isinstance(bundle, Mapping) and bundle.get("trading_mode") == mode
    ]
    exact_bundles = [
        bundle
        for bundle in mode_bundles
        if _proof_identity_matches(bundle, identity, _ORACLE_IDENTITY_FIELDS)
    ]
    blockers: list[dict[str, str]] = []
    if len(exact_bundles) != 1:
        code = "MISSING_FIXTURE_IDENTITY" if not mode_bundles else "STALE_FIXTURE_IDENTITY"
        blockers.append(blocker(code, target_id, mode))

    matching_runs: list[Mapping[str, Any]] = []
    report_shape_valid = False
    report_blocked = False
    report_stale = False
    if report is not None:
        qualification = report.get("qualification")
        proof = report.get("proof")
        report_shape_valid = (
            isinstance(report.get("runs"), list)
            and isinstance(report.get("blockers"), list)
            and isinstance(qualification, Mapping)
            and isinstance(proof, Mapping)
        )
        if report_shape_valid and isinstance(qualification, Mapping) and isinstance(
            proof, Mapping
        ):
            report_blocked = (
                report.get("complete") is not True
                or report.get("verification_state") != "quick_verified"
                or proof.get("complete") is not True
                or qualification.get("verification_state") != "quick_verified"
                or bool(report["blockers"])
                or bool(qualification.get("blockers"))
            )
            report_stale = report.get("trading_mode") != mode or not (
                _proof_identity_matches(report, identity, _COMMIT_IDENTITY_FIELDS)
            )
    if report is None:
        blockers.append(blocker("MISSING_TARGETED_PROOF", target_id, mode))
    elif not report_shape_valid or report_blocked:
        blockers.append(blocker("BLOCKED_TARGETED_REPORT", target_id, mode))
    elif report_stale:
        blockers.append(blocker("STALE_TARGETED_PROOF", target_id, mode))
    else:
        matching_runs = [
            run
            for run in report["runs"]
            if isinstance(run, Mapping)
            and target_id in run.get("target_ids", [])
            and isinstance(run.get("coverage"), Mapping)
            and target_id in run["coverage"].get("reached_target_ids", [])
        ]
        if not matching_runs:
            blockers.append(blocker("UNRESOLVED_CHANGED_TARGET", target_id, mode))
        elif len(matching_runs) != 1:
            blockers.append(blocker("AMBIGUOUS_TARGETED_PROOF", target_id, mode))

    captures = [run.get("capture") for run in matching_runs]
    run_stale = any(
        not _proof_identity_matches(run, identity, _ORACLE_IDENTITY_FIELDS)
        for run in matching_runs
    )
    capture_stale = any(
        not isinstance(capture, Mapping)
        or not _proof_identity_matches(capture, identity, _ORACLE_IDENTITY_FIELDS)
        for capture in captures
    )
    if run_stale:
        blockers.append(blocker("STALE_TARGETED_PROOF", target_id, mode))
    if capture_stale:
        blockers.append(blocker("STALE_ORACLE_PROOF", target_id, mode))

    fixture_links_valid = all(
        sum(
            run.get("fixture_id") in bundle.get("fixture_ids", [])
            for bundle in exact_bundles
            if isinstance(bundle.get("fixture_ids"), list)
        )
        == 1
        for run in matching_runs
    )
    if matching_runs and not fixture_links_valid:
        blockers.append(blocker("MISSING_FIXTURE_LINK", target_id, mode))

    unique_run = matching_runs[0] if len(matching_runs) == 1 else None
    capture = captures[0] if len(captures) == 1 else None
    oracle = (
        isinstance(capture, Mapping)
        and capture.get("complete") is True
        and not capture_stale
        and not run_stale
    )
    native = (
        unique_run is not None
        and unique_run.get("trade_surface_exact") is True
        and not run_stale
    )
    full_state = (
        unique_run is not None
        and unique_run.get("full_state_exact") is True
        and not run_stale
    )
    if matching_runs and not oracle:
        blockers.append(blocker("MISSING_ORACLE_PROOF", target_id, mode))
    if matching_runs and not native:
        blockers.append(blocker("MISSING_NATIVE_ACTION_PROOF", target_id, mode))
    if matching_runs and not full_state:
        blockers.append(blocker("MISSING_FULL_STATE_PROOF", target_id, mode))
    fixture_ids = sorted(
        str(value) for bundle in exact_bundles for value in bundle.get("fixture_ids", [])
    )
    return (
        {
            "trading_mode": mode,
            "fixture_identity": "exact" if len(exact_bundles) == 1 else "blocked",
            "fixture_ids": fixture_ids,
            "oracle_proof": oracle,
            "native_action_proof": native,
            "full_state_proof": full_state,
            "target_resolved": unique_run is not None,
        },
        blockers,
    )


def _proof_identity_matches(
    proof: Mapping[str, Any],
    identity: Mapping[str, str],
    required_fields: tuple[str, ...],
) -> bool:
    expected = {
        "source_sha256": identity["new_source_sha256"],
        "freqtrade_digest": identity["freqtrade_digest"],
        "semantic_profile_sha256": identity["semantic_profile_sha256"],
        "semantic_registry_fingerprint": identity["semantic_registry_fingerprint"],
    }
    expected["upstream_commit"] = identity["upstream_head"]
    expected["oracle_digest"] = identity["freqtrade_digest"]
    aliases = {"freqtrade_digest": "freqtrade_image_digest"}
    return all(
        proof.get(field, proof.get(aliases.get(field, ""))) == expected[field]
        for field in required_fields
    )


def blocker(code: str, target_id: str, mode: str | None = None) -> dict[str, str]:
    """Create one deterministic typed hard blocker."""
    result = {"code": code, "target_id": target_id}
    if mode is not None:
        result["trading_mode"] = mode
    return result


def unique_blockers(blockers: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate and canonically order blockers without losing their fields."""
    return [
        dict(key)
        for key in sorted(
            {tuple(sorted(item.items())) for item in blockers},
        )
    ]
