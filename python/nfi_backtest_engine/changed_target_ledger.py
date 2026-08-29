"""Canonical current-HEAD ledger for changed semantic targets and their proofs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from . import changed_target_identity, changed_target_ownership, changed_target_proofs
from .canonical import read_json
from .changed_target_models import ChangedTargetLedgerSources
from .errors import SpecValidationError
from .specs import CHANGED_TARGET_LEDGER_SCHEMA, validate_schema

CHANGED_TARGET_LEDGER_VERSION: Final = "changed-target-ledger-v1"


def build_changed_target_ledger(
    sources: ChangedTargetLedgerSources,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Join diff, ownership, fixture, and exact proof identities fail-closed."""
    difference = _document(sources.strategy_diff, "strategy diff")
    registry = _document(sources.semantic_registry, "semantic registry", max_bytes=192 << 20)
    fixtures = _document(sources.fixture_registry, "fixture registry")
    targets = changed_target_identity.changed_targets(difference)
    identity = changed_target_identity.ledger_identity(sources, difference, registry)
    dependencies = sorted(
        str(item["path"])
        for item in registry["source_closure"]["files"]
        if item.get("role") != "strategy-root"
    )
    ownership_records, duplicate_ids = changed_target_ownership.ownership_index(registry)
    reports = {
        mode: (
            _document(path, f"{mode} targeted report")
            if path.is_file()
            else None
        )
        for mode, path in sources.targeted_reports.items()
    }
    ledger_targets: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for target in targets:
        target_id = str(target["id"])
        ownership = changed_target_ownership.target_ownership(
            target,
            ownership_records,
            duplicate_ids,
        )
        target_blockers: list[dict[str, str]] = []
        if ownership["obligation_count"] == 0:
            target_blockers.append(
                changed_target_proofs.blocker("MISSING_STATIC_OWNERSHIP", target_id)
            )
        if ownership["duplicate_obligation_count"]:
            target_blockers.append(
                changed_target_proofs.blocker("DUPLICATE_STATIC_OWNERSHIP", target_id)
            )
        if ownership["mapping"] in {None, "official-only-blocker"}:
            target_blockers.append(
                changed_target_proofs.blocker("NON_NATIVE_STATIC_OWNERSHIP", target_id)
            )
        if not ownership["reachable"]:
            target_blockers.append(
                changed_target_proofs.blocker("UNREACHABLE_STATIC_OWNERSHIP", target_id)
            )
        callers = sorted({str(value) for value in target.get("semantic_callers", [])})
        methods = sorted({str(value) for value in target.get("methods", [])})
        static_reachable = bool(methods or callers)
        if not static_reachable:
            target_blockers.append(
                changed_target_proofs.blocker("UNREACHABLE_CHANGED_TARGET", target_id)
            )
        modes = changed_target_ownership.affected_modes(target)
        mode_proofs = []
        for mode in modes:
            proof, mode_blockers = changed_target_proofs.mode_proof(
                changed_target_proofs.ModeProofInputs(
                    target=target,
                    mode=mode,
                    identity=identity,
                    fixtures=fixtures,
                    report=reports[mode],
                )
            )
            mode_proofs.append(proof)
            target_blockers.extend(mode_blockers)
        target_blockers = changed_target_proofs.unique_blockers(target_blockers)
        blockers.extend(target_blockers)
        ledger_targets.append(
            {
                "target_id": target_id,
                "kind": str(target["kind"]),
                "change": str(target["change"]),
                "value": target.get("value"),
                "methods": methods,
                "semantic_callers": callers,
                "dependencies": dependencies,
                "affected_modes": modes,
                "ownership": ownership,
                "reachability": {
                    "static": static_reachable,
                    "transitive": bool(set(callers) - set(methods)),
                },
                "mode_proofs": mode_proofs,
                "hard_blockers": target_blockers,
                "native_promotion_allowed": not target_blockers,
            }
        )
    blockers = changed_target_proofs.unique_blockers(blockers)
    report: dict[str, Any] = {
        "schema_version": CHANGED_TARGET_LEDGER_VERSION,
        "identity": identity,
        "targets": sorted(ledger_targets, key=lambda item: item["target_id"]),
        "hard_blockers": blockers,
        "summary": {
            "target_count": len(ledger_targets),
            "blocked_target_count": sum(
                not item["native_promotion_allowed"] for item in ledger_targets
            ),
            "hard_blocker_count": len(blockers),
            "native_promotion_allowed": bool(ledger_targets) and not blockers,
        },
    }
    report["fingerprint"] = _sha256_json(report)
    validate_schema(report, CHANGED_TARGET_LEDGER_SCHEMA)
    if output_path is not None:
        _publish(Path(output_path), report)
    return report


def _document(path: Path, label: str, *, max_bytes: int = 64 << 20) -> dict[str, Any]:
    document = read_json(path, max_bytes=max_bytes)
    if not isinstance(document, dict):
        raise SpecValidationError(f"{label} must be a JSON object")
    return document


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _publication_checkpoint(_temporary: Path) -> None:
    """Test synchronization point before the authoritative atomic replace."""


def _publish(destination: Path, report: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(name)
    try:
        payload = (json.dumps(report, indent=2, sort_keys=False) + "\n").encode()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _publication_checkpoint(temporary)
        temporary.replace(destination)
        if os.name == "posix":
            parent_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["ChangedTargetLedgerSources", "build_changed_target_ledger"]
