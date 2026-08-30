"""Validate one paired discovery result before ledger or Draft-PR mutation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final

from .canonical import read_json, write_json
from .compatibility_automation import classify_compatibility_automation
from .compatibility_automation_validation import document, parse_compatibility_identity
from .errors import SpecValidationError
from .futures_discovery import (
    DISCOVERY_CURSOR_VERSION,
    DISCOVERY_REPORT_VERSION,
    DISCOVERY_REQUEST_VERSION,
    build_discovery_request,
    load_discovery_policy,
)

DISCOVERY_PUBLICATION_VERSION: Final = "1.0.0"
_MODES: Final = ("spot", "futures")
_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"[1-9][0-9]*")
_REPORT_STATES: Final = {
    "no_gap",
    "candidate_found",
    "budget_exhausted",
    "coverage_exhausted",
    "unsupported_semantics",
    "external_data_deferred",
}
_ATTEMPT_OUTCOMES: Final = {
    "miss",
    "candidate",
    "unsupported",
    "external_data_deferred",
}


def authorize_discovery_publication(
    identity: Mapping[str, Any] | str | Path,
    strategy_diff: Mapping[str, Any] | str | Path,
    compatibility_reports: Mapping[str, Mapping[str, Any] | str | Path],
    targeted_reports: Mapping[str, Mapping[str, Any] | str | Path],
    bundle_directories: Mapping[str, str | Path],
    policy_paths: Mapping[str, str | Path],
    fixtures_root: str | Path,
    *,
    source_run_id: str,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Authorize paired compact discovery state independently of product exactness."""
    if _RUN_ID.fullmatch(source_run_id) is None:
        raise SpecValidationError("discovery source run id is invalid")
    identity_document = document(identity, "compatibility identity")
    checked_identity = parse_compatibility_identity(identity_document)
    difference = document(strategy_diff, "strategy diff")
    if difference.get("new", {}).get("sha256") != checked_identity["strategy_sha256"]:
        raise SpecValidationError("discovery strategy diff and identity source differ")

    modes: dict[str, dict[str, Any]] = {}
    for mode in _MODES:
        try:
            compatibility_value = compatibility_reports[mode]
            targeted_value = targeted_reports[mode]
            bundle_value = bundle_directories[mode]
            policy_value = policy_paths[mode]
        except KeyError as exc:
            raise SpecValidationError(f"discovery publication lacks {mode} input") from exc
        compatibility = document(compatibility_value, f"{mode} compatibility report")
        targeted = document(targeted_value, f"{mode} targeted report")
        bundle_root = Path(bundle_value).resolve()
        if not bundle_root.is_dir() or bundle_root.is_symlink():
            raise SpecValidationError(f"{mode} discovery bundle directory is invalid")
        request = _read_object(bundle_root / "discovery-request.json", f"{mode} request")
        report = _read_object(bundle_root / "discovery-report.json", f"{mode} report")
        cursor = _read_object(bundle_root / "cursor.json", f"{mode} cursor")
        decision = _read_object(bundle_root / "automation-decision.json", f"{mode} decision")
        candidate_root = bundle_root / "candidate-fixture"

        policy = load_discovery_policy(policy_value)
        if policy.trading_mode != mode:
            raise SpecValidationError(f"{mode} discovery policy mode differs")
        _validate_request(
            request,
            identity_document=identity_document,
            checked_identity=checked_identity,
            difference=difference,
            compatibility=compatibility,
            fixtures_root=fixtures_root,
            policy=policy,
        )
        _validate_report_and_cursor(
            report,
            cursor,
            request=request,
            checked_identity=checked_identity,
            deferred_http_statuses=policy.deferred_http_statuses,
        )
        expected_decision = classify_compatibility_automation(
            identity_document,
            difference,
            compatibility,
            targeted,
            discovery=report,
        )
        if decision != expected_decision:
            raise SpecValidationError(f"{mode} discovery automation decision differs")
        candidate_expected = decision["automation_route"] == "exact_fixture_draft_pr"
        if candidate_expected != candidate_root.is_dir() or candidate_root.is_symlink():
            raise SpecValidationError(f"{mode} discovery candidate presence differs")

        bundle_fingerprint = _canonical_sha256(
            {
                "request": request,
                "report": report,
                "cursor": cursor,
                "decision": decision,
            }
        )
        modes[mode] = {
            "trading_mode": mode,
            "bundle_fingerprint": bundle_fingerprint,
            "request_fingerprint": request["fingerprint"],
            "status": report["status"],
            "automation_route": decision["automation_route"],
            "exact_fixture_draft_pr": candidate_expected,
        }

    authorization_identity = {
        "source_run_id": source_run_id,
        "identity": checked_identity,
        "modes": modes,
    }
    authorization = {
        "schema_version": DISCOVERY_PUBLICATION_VERSION,
        **authorization_identity,
        "authorization_fingerprint": _canonical_sha256(authorization_identity),
    }
    if output_path is not None:
        write_json(output_path, authorization)
    return authorization


def validate_discovery_authorization(
    authorization: Mapping[str, Any] | str | Path,
    bundle_directories: Mapping[str, str | Path],
    *,
    expected_identity: Mapping[str, str],
    expected_source_run_id: str,
) -> dict[str, Any]:
    """Recheck an authorization against the exact artifacts at a mutation boundary."""
    value = document(authorization, "discovery publication authorization")
    if (
        value.get("schema_version") != DISCOVERY_PUBLICATION_VERSION
        or value.get("source_run_id") != expected_source_run_id
        or value.get("identity") != dict(expected_identity)
    ):
        raise SpecValidationError("discovery publication authorization identity differs")
    modes = value.get("modes")
    if not isinstance(modes, Mapping) or set(modes) != set(_MODES):
        raise SpecValidationError("discovery publication authorization modes differ")
    authorization_identity = {
        "source_run_id": value["source_run_id"],
        "identity": value["identity"],
        "modes": modes,
    }
    if value.get("authorization_fingerprint") != _canonical_sha256(
        authorization_identity
    ):
        raise SpecValidationError("discovery publication authorization fingerprint differs")

    for mode in _MODES:
        bundle_root = Path(bundle_directories[mode]).resolve()
        if not bundle_root.is_dir() or bundle_root.is_symlink():
            raise SpecValidationError(f"{mode} authorized discovery bundle is invalid")
        request = _read_object(bundle_root / "discovery-request.json", f"{mode} request")
        report = _read_object(bundle_root / "discovery-report.json", f"{mode} report")
        cursor = _read_object(bundle_root / "cursor.json", f"{mode} cursor")
        decision = _read_object(bundle_root / "automation-decision.json", f"{mode} decision")
        candidate_expected = decision.get("automation_route") == "exact_fixture_draft_pr"
        candidate_root = bundle_root / "candidate-fixture"
        if candidate_expected != candidate_root.is_dir() or candidate_root.is_symlink():
            raise SpecValidationError(f"{mode} authorized candidate presence differs")
        expected_mode = {
            "trading_mode": mode,
            "bundle_fingerprint": _canonical_sha256(
                {
                    "request": request,
                    "report": report,
                    "cursor": cursor,
                    "decision": decision,
                }
            ),
            "request_fingerprint": request.get("fingerprint"),
            "status": report.get("status"),
            "automation_route": decision.get("automation_route"),
            "exact_fixture_draft_pr": candidate_expected,
        }
        if modes.get(mode) != expected_mode:
            raise SpecValidationError(f"{mode} discovery bundle differs from authorization")
    return value


def _validate_request(
    request: Mapping[str, Any],
    *,
    identity_document: Mapping[str, Any],
    checked_identity: Mapping[str, str],
    difference: Mapping[str, Any],
    compatibility: Mapping[str, Any],
    fixtures_root: str | Path,
    policy: Any,
) -> None:
    if request.get("schema_version") != DISCOVERY_REQUEST_VERSION:
        raise SpecValidationError("discovery request schema is unsupported")
    request_identity = request.get("identity")
    if not isinstance(request_identity, Mapping):
        raise SpecValidationError("discovery request identity is invalid")
    completed_through = request_identity.get("completed_through")
    try:
        as_of = date.fromisoformat(str(completed_through))
    except ValueError as exc:
        raise SpecValidationError("discovery request completion date is invalid") from exc

    baseline_upstream = identity_document.get("baseline_upstream_sha")
    if not isinstance(baseline_upstream, str) or _SHA.fullmatch(baseline_upstream) is None:
        baseline_upstream = None
    old = difference.get("old")
    baseline_source = old.get("sha256") if isinstance(old, Mapping) else None
    if baseline_upstream is None:
        baseline_source = None
    elif not isinstance(baseline_source, str):
        raise SpecValidationError("discovery baseline source is missing")

    expected = build_discovery_request(
        difference,
        compatibility,
        fixtures_root,
        policy=policy,
        upstream_commit=checked_identity["upstream_sha"],
        engine_commit=checked_identity["engine_sha"],
        as_of=as_of,
        baseline_upstream_commit=baseline_upstream,
        baseline_source_sha256=baseline_source,
    )
    if request != expected:
        raise SpecValidationError("discovery request differs from authoritative inputs")


def _validate_report_and_cursor(
    report: Mapping[str, Any],
    cursor: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    checked_identity: Mapping[str, str],
    deferred_http_statuses: frozenset[int],
) -> None:
    status = report.get("status")
    if report.get("schema_version") != DISCOVERY_REPORT_VERSION or status not in _REPORT_STATES:
        raise SpecValidationError("discovery publication report status is unsupported")
    if cursor.get("schema_version") != DISCOVERY_CURSOR_VERSION:
        raise SpecValidationError("discovery publication cursor schema is unsupported")
    mode = request.get("trading_mode")
    request_identity = request.get("identity")
    if not isinstance(request_identity, Mapping):
        raise SpecValidationError("discovery publication request identity is invalid")
    expected_report_identity = {
        "trading_mode": mode,
        "fingerprint": request.get("fingerprint"),
        "upstream_commit": checked_identity["upstream_sha"],
        "baseline_upstream_commit": request_identity.get("baseline_upstream_commit"),
        "engine_commit": checked_identity["engine_sha"],
        "freqtrade_image_digest": checked_identity["freqtrade_digest"],
        "strategy_sha256": checked_identity["strategy_sha256"],
        "policy_sha256": request_identity.get("policy_sha256"),
        "target_count": len(request.get("missing_targets", [])),
        "searchable_target_count": len(request.get("searchable_targets", [])),
        "unsearchable_target_ids": sorted(
            str(target.get("id"))
            for target in request.get("unsearchable_targets", [])
            if isinstance(target, Mapping)
        ),
    }
    if any(report.get(field) != value for field, value in expected_report_identity.items()):
        raise SpecValidationError("discovery publication report identity differs")
    attempts = report.get("attempts")
    if not isinstance(attempts, list) or report.get("searched_shard_count") != len(attempts):
        raise SpecValidationError("discovery publication attempts are invalid")
    timeranges = request_identity.get("timeranges")
    if not isinstance(timeranges, list):
        raise SpecValidationError("discovery publication timeranges are invalid")
    previous_index = -1
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise SpecValidationError("discovery publication attempt is invalid")
        index = attempt.get("index")
        outcome = attempt.get("outcome")
        target_ids = attempt.get("target_ids")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index <= previous_index
            or index < 0
            or index >= len(timeranges)
            or attempt.get("timerange") != timeranges[index]
            or outcome not in _ATTEMPT_OUTCOMES
            or not isinstance(attempt.get("message"), str)
            or not isinstance(target_ids, list)
            or not all(isinstance(value, str) for value in target_ids)
        ):
            raise SpecValidationError("discovery publication attempt fields differ")
        previous_index = index

    next_shard = report.get("next_shard")
    shard_count = report.get("shard_count")
    elapsed = report.get("elapsed_seconds")
    complete = status in {
        "no_gap",
        "candidate_found",
        "coverage_exhausted",
        "unsupported_semantics",
    }
    if (
        not isinstance(next_shard, int)
        or isinstance(next_shard, bool)
        or not isinstance(shard_count, int)
        or isinstance(shard_count, bool)
        or shard_count != len(timeranges)
        or not 0 <= next_shard <= shard_count
        or not isinstance(elapsed, int | float)
        or isinstance(elapsed, bool)
        or not math.isfinite(elapsed)
        or elapsed < 0
        or report.get("complete") is not complete
    ):
        raise SpecValidationError("discovery publication progress is invalid")
    if attempts:
        expected_next = previous_index if status == "external_data_deferred" else previous_index + 1
        if next_shard != expected_next:
            raise SpecValidationError("discovery publication cursor did not follow attempts")

    cursor_expected = {
        "schema_version": DISCOVERY_CURSOR_VERSION,
        "fingerprint": report.get("fingerprint"),
        "next_shard": next_shard,
        "shard_count": shard_count,
        "complete": complete,
        "status": status,
    }
    external = report.get("external_data")
    if status == "external_data_deferred":
        if (
            not isinstance(external, Mapping)
            or external.get("reason") != "http_status"
            or external.get("http_status") not in deferred_http_statuses
            or external.get("retry") != "identity-change-or-manual"
        ):
            raise SpecValidationError("discovery publication deferral is invalid")
        cursor_expected["deferred_external"] = dict(external)
    elif external is not None:
        raise SpecValidationError("discovery publication has unexpected external state")
    if cursor != cursor_expected:
        raise SpecValidationError("discovery publication cursor differs from report")

    candidate = report.get("candidate")
    if status == "candidate_found":
        if (
            not isinstance(candidate, Mapping)
            or candidate.get("trade_surface_exact") is not True
            or candidate.get("full_state_exact") is not True
        ):
            raise SpecValidationError("discovery publication candidate is not exact")
    elif candidate is not None:
        raise SpecValidationError("discovery publication has an unexpected candidate")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise SpecValidationError(f"{label} must be an object")
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
