"""Boundary parsing for changed-target source and current upstream identity."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final

from .changed_target_models import ChangedTargetLedgerSources
from .errors import SpecValidationError
from .semantic_registry import _registry_fingerprint, _source_closure_merkle

_SHA: Final = re.compile(r"[0-9a-f]{40}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def ledger_identity(
    sources: ChangedTargetLedgerSources,
    difference: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, str]:
    """Parse and cross-bind source diff, registry, and execution-time HEAD."""
    old = difference.get("old")
    new = difference.get("new")
    strategy = registry.get("strategy")
    upstream = strategy.get("upstream") if isinstance(strategy, Mapping) else None
    source = strategy.get("source") if isinstance(strategy, Mapping) else None
    freqtrade = registry.get("freqtrade")
    reference = freqtrade.get("reference") if isinstance(freqtrade, Mapping) else None
    profile = freqtrade.get("semantic_profile") if isinstance(freqtrade, Mapping) else None
    closure = registry.get("source_closure")
    _validate_materialized_sources(sources, old, new)
    _validate_registry_identity(registry, closure)
    values = {
        "upstream_repository": sources.upstream_repository,
        "upstream_ref": sources.upstream_ref,
        "upstream_head": sources.upstream_head,
        "baseline_commit": sources.baseline_commit,
        "old_source_sha256": old.get("sha256") if isinstance(old, Mapping) else None,
        "new_source_sha256": new.get("sha256") if isinstance(new, Mapping) else None,
        "source_closure_merkle_root": (
            closure.get("merkle_root") if isinstance(closure, Mapping) else None
        ),
        "freqtrade_digest": (
            reference.get("image_index_digest") if isinstance(reference, Mapping) else None
        ),
        "semantic_profile_sha256": (
            profile.get("fingerprint") if isinstance(profile, Mapping) else None
        ),
        "semantic_registry_fingerprint": registry.get("fingerprint"),
    }
    if (
        _SHA.fullmatch(sources.upstream_head) is None
        or _SHA.fullmatch(sources.baseline_commit) is None
        or not all(
            isinstance(values[key], str) and _SHA256.fullmatch(values[key])
            for key in (
                "old_source_sha256",
                "new_source_sha256",
                "source_closure_merkle_root",
                "semantic_profile_sha256",
                "semantic_registry_fingerprint",
            )
        )
    ):
        raise SpecValidationError("strategy diff or ledger identity is malformed")
    if (
        not isinstance(reference, Mapping)
        or not isinstance(values["freqtrade_digest"], str)
        or not values["freqtrade_digest"].startswith("sha256:")
        or not isinstance(upstream, Mapping)
        or upstream.get("repository") != sources.upstream_repository
        or upstream.get("ref") != sources.upstream_ref
        or upstream.get("configured_commit") != sources.upstream_head
        or upstream.get("observed_commit") != sources.upstream_head
        or not isinstance(source, Mapping)
        or source.get("sha256") != values["new_source_sha256"]
        or not isinstance(old, Mapping)
        or old.get("commit") != sources.baseline_commit
        or not isinstance(new, Mapping)
        or new.get("commit") != sources.upstream_head
    ):
        raise SpecValidationError("strategy diff, registry, and current HEAD identity differ")
    return {key: str(value) for key, value in values.items()}


def changed_targets(difference: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse the versioned deterministic source-diff target array."""
    raw = difference.get("behavior_targets")
    if difference.get("schema_version") != "1.3.0" or not isinstance(raw, list):
        raise SpecValidationError("strategy diff contract is unsupported")
    result = []
    for target in raw:
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("id"), str)
            or _SHA256.fullmatch(target["id"]) is None
            or target.get("change") not in {"added", "removed", "changed"}
            or not isinstance(target.get("kind"), str)
            or not isinstance(target.get("methods"), list)
            or not isinstance(target.get("semantic_callers"), list)
            or not isinstance(target.get("tags"), list)
        ):
            raise SpecValidationError("strategy diff behavior target identity is malformed")
        preimage = {
            key: target[key]
            for key in ("kind", "change", "value", "methods", "semantic_callers", "tags")
        }
        if target["id"] != _sha256_json(preimage):
            raise SpecValidationError("strategy diff behavior target ID differs from its preimage")
        result.append(target)
    if not result or len({target["id"] for target in result}) != len(result):
        raise SpecValidationError("strategy diff targets are missing or duplicated")
    return sorted(result, key=lambda item: item["id"])


def _validate_materialized_sources(
    sources: ChangedTargetLedgerSources,
    old: Any,
    new: Any,
) -> None:
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise SpecValidationError("strategy diff source identities are malformed")
    for record in (old, new):
        name = record.get("name")
        if not isinstance(name, str) or name != str(name).split("/")[-1]:
            raise SpecValidationError("strategy diff source path is not local")
        path = sources.strategy_diff.parent / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != record.get("bytes")
            or hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256")
        ):
            raise SpecValidationError("strategy diff materialized source identity differs")


def _validate_registry_identity(registry: Mapping[str, Any], closure: Any) -> None:
    if (
        not isinstance(closure, Mapping)
        or not isinstance(closure.get("files"), list)
        or closure.get("merkle_root") != _source_closure_merkle(closure["files"])
        or registry.get("fingerprint") != _registry_fingerprint(registry)
    ):
        raise SpecValidationError("semantic registry identity is not canonically derived")


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
