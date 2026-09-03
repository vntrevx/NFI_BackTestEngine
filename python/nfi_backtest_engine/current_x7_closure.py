"""Compact, source-bound inventory for one current upstream X7 closure run."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from .changed_target_identity import changed_targets
from .errors import SpecValidationError
from .release_provenance import canonical_sha256

CURRENT_X7_CLOSURE_VERSION: Final = "current-x7-closure-v1"
_MODES: Final = ("spot", "futures")
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def build_current_x7_closure(
    strategy_diff: Mapping[str, Any],
    compatibility_reports: Mapping[str, Mapping[str, Any]],
    discovery_requests: Mapping[str, Mapping[str, Any]],
    discovery_policies: Mapping[str, Mapping[str, Any]],
    *,
    upstream_repository: str,
    upstream_ref: str,
    upstream_commit: str,
    baseline_upstream_commit: str,
    engine_commit: str,
    semantic_profile_sha256: str,
    observed_at: str,
) -> dict[str, Any]:
    """Cross-bind one source diff to both mode queues and resumable cursors."""
    targets = changed_targets(strategy_diff)
    old_source = _source_identity(strategy_diff.get("old"), "baseline")
    new_source = _source_identity(strategy_diff.get("new"), "current")
    _sha(upstream_commit, "upstream commit")
    _sha(baseline_upstream_commit, "baseline upstream commit")
    _sha(engine_commit, "engine commit")
    _sha256(semantic_profile_sha256, "semantic profile")
    if old_source.get("commit") != baseline_upstream_commit:
        raise SpecValidationError("strategy diff baseline commit differs")
    if new_source.get("commit") != upstream_commit:
        raise SpecValidationError("strategy diff upstream commit differs")

    target_inventory = [
        {
            key: target[key]
            for key in (
                "id",
                "kind",
                "change",
                "value",
                "methods",
                "semantic_callers",
                "tags",
                "runtime_observable",
            )
        }
        for target in targets
    ]
    target_ids = [str(target["id"]) for target in targets]
    modes = {
        mode: _mode_inventory(
            mode,
            report=compatibility_reports.get(mode),
            request=discovery_requests.get(mode),
            policy=discovery_policies.get(mode),
            upstream_commit=upstream_commit,
            baseline_upstream_commit=baseline_upstream_commit,
            engine_commit=engine_commit,
            old_source_sha256=str(old_source["sha256"]),
            new_source_sha256=str(new_source["sha256"]),
            target_ids=target_ids,
        )
        for mode in _MODES
    }
    document = {
        "schema_version": CURRENT_X7_CLOSURE_VERSION,
        "observed_at": observed_at,
        "identity": {
            "upstream_repository": upstream_repository,
            "upstream_ref": upstream_ref,
            "upstream_commit": upstream_commit,
            "baseline_upstream_commit": baseline_upstream_commit,
            "engine_commit": engine_commit,
            "baseline_source_sha256": old_source["sha256"],
            "source_sha256": new_source["sha256"],
            "semantic_profile_sha256": semantic_profile_sha256,
        },
        "source_analysis": {
            "classification": strategy_diff.get("classification"),
            "changed_callbacks": strategy_diff.get("changes", {})
            .get("callbacks", {})
            .get("changed", []),
            "target_count": len(target_inventory),
            "targets": target_inventory,
        },
        "modes": modes,
    }
    document["fingerprint"] = canonical_sha256(document)
    validate_current_x7_closure(document)
    return document


def validate_current_x7_closure(document: Mapping[str, Any]) -> None:
    """Validate a compact closure inventory without trusting its summary fields."""
    if document.get("schema_version") != CURRENT_X7_CLOSURE_VERSION:
        raise SpecValidationError("current X7 closure schema is unsupported")
    unsigned = {key: value for key, value in document.items() if key != "fingerprint"}
    if document.get("fingerprint") != canonical_sha256(unsigned):
        raise SpecValidationError("current X7 closure fingerprint differs")
    identity = document.get("identity")
    analysis = document.get("source_analysis")
    modes = document.get("modes")
    if not all(isinstance(value, Mapping) for value in (identity, analysis, modes)):
        raise SpecValidationError("current X7 closure structure is malformed")
    assert isinstance(identity, Mapping)
    assert isinstance(analysis, Mapping)
    assert isinstance(modes, Mapping)
    for field in ("upstream_commit", "baseline_upstream_commit", "engine_commit"):
        _sha(identity.get(field), field)
    for field in (
        "baseline_source_sha256",
        "source_sha256",
        "semantic_profile_sha256",
    ):
        _sha256(identity.get(field), field)
    targets = analysis.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SpecValidationError("current X7 closure target inventory is empty")
    target_ids: list[str] = []
    for target in targets:
        target_id = target.get("id") if isinstance(target, Mapping) else None
        if not isinstance(target_id, str) or _SHA256.fullmatch(target_id) is None:
            raise SpecValidationError("current X7 closure target inventory differs")
        target_ids.append(target_id)
    if (
        target_ids != sorted(target_ids)
        or len(set(target_ids)) != len(target_ids)
        or analysis.get("target_count") != len(target_ids)
        ):
        raise SpecValidationError("current X7 closure target inventory differs")
    changed_targets(
        {
            "schema_version": "1.3.0",
            "behavior_targets": [dict(target) for target in targets],
        }
    )
    if tuple(modes) != _MODES:
        raise SpecValidationError("current X7 closure mode inventory differs")
    for mode in _MODES:
        _validate_mode(mode, modes[mode], target_ids)


def _mode_inventory(
    mode: str,
    *,
    report: Mapping[str, Any] | None,
    request: Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None,
    upstream_commit: str,
    baseline_upstream_commit: str,
    engine_commit: str,
    old_source_sha256: str,
    new_source_sha256: str,
    target_ids: list[str],
) -> dict[str, Any]:
    if not all(isinstance(value, Mapping) for value in (report, request, policy)):
        raise SpecValidationError(f"current X7 {mode} inputs are incomplete")
    assert report is not None
    assert request is not None
    assert policy is not None
    source = report.get("source")
    identity = request.get("identity")
    if (
        report.get("trading_mode") != mode
        or report.get("native_compatible") is not True
        or not isinstance(source, Mapping)
        or source.get("sha256") != new_source_sha256
        or request.get("trading_mode") != mode
        or request.get("native_compatible") is not True
        or not isinstance(identity, Mapping)
    ):
        raise SpecValidationError(f"current X7 {mode} compatibility identity differs")
    expected_identity = {
        "upstream_commit": upstream_commit,
        "baseline_upstream_commit": baseline_upstream_commit,
        "engine_commit": engine_commit,
        "strategy_sha256": new_source_sha256,
        "baseline_strategy_sha256": old_source_sha256,
        "trading_mode": mode,
        "target_ids": target_ids,
    }
    if any(identity.get(field) != value for field, value in expected_identity.items()):
        raise SpecValidationError(f"current X7 {mode} discovery identity differs")
    if policy.get("trading_mode") != mode:
        raise SpecValidationError(f"current X7 {mode} policy mode differs")
    universe = policy.get("universe")
    if not isinstance(universe, Mapping):
        raise SpecValidationError(f"current X7 {mode} universe is malformed")
    missing = _ids(request.get("missing_targets"), f"{mode} missing targets")
    searchable = _ids(request.get("searchable_targets"), f"{mode} searchable targets")
    unsearchable = _ids(request.get("unsearchable_targets"), f"{mode} unsearchable targets")
    if sorted((*searchable, *unsearchable)) != missing:
        raise SpecValidationError(f"current X7 {mode} discovery partition differs")
    search = request.get("search")
    if not isinstance(search, Mapping) or not isinstance(search.get("timeranges"), list):
        raise SpecValidationError(f"current X7 {mode} search bounds are malformed")
    fingerprint = request.get("fingerprint")
    _sha256(fingerprint, f"{mode} discovery fingerprint")
    if fingerprint != canonical_sha256(identity):
        raise SpecValidationError(f"current X7 {mode} discovery fingerprint differs")
    return {
        "native_compatible": True,
        "plan_status": request.get("plan_status"),
        "target_ids": target_ids,
        "missing_target_ids": missing,
        "searchable_target_ids": searchable,
        "unsearchable_target_ids": unsearchable,
        "policy_sha256": identity.get("policy_sha256"),
        "market_catalog": universe.get("market_catalog"),
        "market_snapshot": universe.get("market_snapshot"),
        "discovery_request_fingerprint": fingerprint,
        "initial_cursor": {
            "schema_version": "1.0.0",
            "fingerprint": fingerprint,
            "next_shard": 0,
            "shard_count": len(search["timeranges"]),
        },
    }


def _validate_mode(mode: str, value: Any, target_ids: list[str]) -> None:
    if not isinstance(value, Mapping):
        raise SpecValidationError(f"current X7 {mode} inventory is malformed")
    missing = _string_ids(value.get("missing_target_ids"), f"{mode} missing targets")
    searchable = _string_ids(
        value.get("searchable_target_ids"), f"{mode} searchable targets"
    )
    unsearchable = _string_ids(
        value.get("unsearchable_target_ids"), f"{mode} unsearchable targets"
    )
    cursor = value.get("initial_cursor")
    if (
        value.get("native_compatible") is not True
        or value.get("target_ids") != target_ids
        or value.get("plan_status") != ("coverage-gap" if missing else "ready")
        or sorted((*searchable, *unsearchable)) != missing
        or any(item not in target_ids for item in missing)
        or not isinstance(cursor, Mapping)
        or cursor.get("schema_version") != "1.0.0"
        or cursor.get("fingerprint") != value.get("discovery_request_fingerprint")
        or cursor.get("next_shard") != 0
        or not isinstance(cursor.get("shard_count"), int)
        or cursor["shard_count"] <= 0
    ):
        raise SpecValidationError(f"current X7 {mode} cursor or target inventory differs")
    for field in ("policy_sha256", "discovery_request_fingerprint"):
        _sha256(value.get(field), f"{mode} {field}")


def _source_identity(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecValidationError(f"strategy diff {label} source identity is malformed")
    _sha256(value.get("sha256"), f"{label} source")
    return value


def _ids(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise SpecValidationError(f"{label} are malformed")
    result = sorted(
        str(item.get("id"))
        for item in value
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    )
    if len(result) != len(value) or any(_SHA256.fullmatch(item) is None for item in result):
        raise SpecValidationError(f"{label} are malformed")
    return result


def _string_ids(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in value)
        or value != sorted(value)
        or len(set(value)) != len(value)
    ):
        raise SpecValidationError(f"{label} are malformed")
    return value


def _sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise SpecValidationError(f"current X7 {label} is malformed")


def _sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SpecValidationError(f"current X7 {label} is malformed")


__all__ = [
    "CURRENT_X7_CLOSURE_VERSION",
    "build_current_x7_closure",
    "validate_current_x7_closure",
]
