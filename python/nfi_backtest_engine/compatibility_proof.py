"""Authoritative proof loading for NFI compatibility product status."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .canonical import read_json, write_json
from .changed_target_workflow import validate_changed_target_promotion
from .compatibility_automation_core import classify_compatibility_automation
from .errors import InputBoundaryError, SpecValidationError

PROOF_MANIFEST_VERSION: Final = "compatibility-proof-manifest-v1"
SOURCE_RUN_VERSION: Final = "compatibility-source-run-v1"
_REQUIRED_ARTIFACTS: Final = frozenset(
    {
        "compatibility-identity.json",
        "strategy-diff.json",
        "semantic-profile.json",
        "report-spot.json",
        "report-futures.json",
        "targeted-report-spot.json",
        "targeted-report-futures.json",
        "qualification-spot.json",
        "qualification-futures.json",
        "automation-decision-spot.json",
        "automation-decision-futures.json",
        "changed-target-ledger.json",
        "hosted-canary.json",
        "source-run.json",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProofFailureReason(StrEnum):
    """Typed reason an authoritative proof could not be consumed."""

    MISSING = "missing_authoritative_proof"
    MALFORMED = "malformed_artifacts"
    INVALID = "invalid_authoritative_proof"


@dataclass(frozen=True, slots=True)
class VerifiedCompatibilityProof:
    """Cross-validated proof references for one immutable source run."""

    source_run_id: str
    manifest_sha256: str
    artifact_sha256: Mapping[str, str]
    decisions: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class UnavailableCompatibilityProof:
    """Fail-closed proof loading outcome."""

    reason: ProofFailureReason


type CompatibilityProof = VerifiedCompatibilityProof | UnavailableCompatibilityProof


def seal_compatibility_proof(root: Path, source_run_id: str) -> dict[str, Any]:
    """Hash the complete authoritative artifact set after all files are durable."""
    artifacts = []
    for relative in sorted(_REQUIRED_ARTIFACTS):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise InputBoundaryError(f"compatibility proof artifact is missing: {relative}")
        artifacts.append({"path": relative, "sha256": _sha256_file(path)})
    manifest = {
        "schema_version": PROOF_MANIFEST_VERSION,
        "source_run_id": source_run_id,
        "artifacts": artifacts,
    }
    write_json(root / "compatibility-proof-manifest.json", manifest)
    return manifest


def load_compatibility_proof(
    root: Path,
    *,
    expected_source_run_id: str,
) -> CompatibilityProof:
    """Parse and recompute every proof reference, returning a typed failure."""
    manifest_path = root / "compatibility-proof-manifest.json"
    if not manifest_path.is_file():
        return UnavailableCompatibilityProof(ProofFailureReason.MISSING)
    try:
        manifest = _read_object(manifest_path)
        artifact_sha256 = _validate_manifest(
            root,
            manifest,
            expected_source_run_id=expected_source_run_id,
        )
        documents = {
            relative: _read_object(root / relative)
            for relative in _REQUIRED_ARTIFACTS
        }
        decisions = _validate_documents(documents, expected_source_run_id)
    except (json.JSONDecodeError, UnicodeDecodeError, InputBoundaryError):
        return UnavailableCompatibilityProof(ProofFailureReason.MALFORMED)
    except SpecValidationError:
        return UnavailableCompatibilityProof(ProofFailureReason.INVALID)
    return VerifiedCompatibilityProof(
        source_run_id=expected_source_run_id,
        manifest_sha256=_sha256_file(manifest_path),
        artifact_sha256=artifact_sha256,
        decisions=decisions,
    )


def _validate_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    expected_source_run_id: str,
) -> dict[str, str]:
    records = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != PROOF_MANIFEST_VERSION
        or manifest.get("source_run_id") != expected_source_run_id
        or not isinstance(records, list)
    ):
        raise SpecValidationError("compatibility proof manifest identity is invalid")
    parsed: dict[str, str] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise SpecValidationError("compatibility proof manifest record is invalid")
        relative = record.get("path")
        digest = record.get("sha256")
        if (
            not isinstance(relative, str)
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or relative in parsed
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise SpecValidationError("compatibility proof manifest record is unsafe")
        parsed[relative] = digest
    if set(parsed) != set(_REQUIRED_ARTIFACTS):
        raise SpecValidationError("compatibility proof manifest is incomplete")
    for relative, digest in parsed.items():
        path = root / relative
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != digest:
            raise SpecValidationError("compatibility proof artifact hash differs")
    return parsed


def _validate_documents(
    documents: Mapping[str, Mapping[str, Any]],
    expected_source_run_id: str,
) -> dict[str, Mapping[str, Any]]:
    identity = documents["compatibility-identity.json"]
    source_run = documents["source-run.json"]
    expected_run_identity = {
        "upstream_sha": identity.get("upstream_sha"),
        "engine_sha": identity.get("engine_sha"),
        "freqtrade_digest": identity.get("freqtrade_digest"),
        "semantic_profile_sha256": identity.get("semantic_profile_sha256"),
        "source_sha256": identity.get("source_sha256"),
    }
    if (
        source_run.get("schema_version") != SOURCE_RUN_VERSION
        or source_run.get("source_run_id") != expected_source_run_id
        or source_run.get("identity") != expected_run_identity
    ):
        raise SpecValidationError("compatibility source run identity differs")
    profile = documents["semantic-profile.json"]
    if profile.get("fingerprint") != identity.get("semantic_profile_sha256"):
        raise SpecValidationError("compatibility semantic profile differs")
    difference = documents["strategy-diff.json"]
    decisions: dict[str, Mapping[str, Any]] = {}
    for mode in ("spot", "futures"):
        targeted = documents[f"targeted-report-{mode}.json"]
        qualification = documents[f"qualification-{mode}.json"]
        if targeted.get("qualification") != qualification:
            raise SpecValidationError("targeted report and qualification artifact differ")
        recomputed = classify_compatibility_automation(
            identity,
            difference,
            documents[f"report-{mode}.json"],
            targeted,
        )
        if recomputed != documents[f"automation-decision-{mode}.json"]:
            raise SpecValidationError("automation decision differs from authoritative inputs")
        decisions[mode] = recomputed
    validate_changed_target_promotion(documents["changed-target-ledger.json"], decisions)
    summary = documents["changed-target-ledger.json"].get("summary")
    if not isinstance(summary, Mapping) or summary.get("native_promotion_allowed") is not True:
        raise SpecValidationError("changed-target ledger blocks Native promotion")
    _validate_hosted_canary(documents["hosted-canary.json"], identity, decisions)
    return decisions


def _validate_hosted_canary(
    canary: Mapping[str, Any],
    identity: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
) -> None:
    expected_identity = {
        "nfi_upstream_sha": identity.get("upstream_sha"),
        "engine_sha": identity.get("engine_sha"),
        "freqtrade_digest": identity.get("freqtrade_digest"),
        "semantic_profile_sha256": identity.get("semantic_profile_sha256"),
    }
    expected_modes = [
        {
            "trading_mode": mode,
            "automation_route": decisions[mode].get("automation_route"),
            "execution_route": decisions[mode].get("execution_route"),
            "action_fingerprint": decisions[mode].get("action_fingerprint"),
        }
        for mode in ("spot", "futures")
    ]
    modes = canary.get("modes")
    projected = [
        {
            "trading_mode": item.get("trading_mode"),
            "automation_route": item.get("automation_route"),
            "execution_route": item.get("execution_route"),
            "action_fingerprint": item.get("action_fingerprint"),
        }
        for item in modes
        if isinstance(item, Mapping)
    ] if isinstance(modes, list) else []
    preimage = {key: value for key, value in canary.items() if key != "fingerprint"}
    if (
        canary.get("schema_version") != "compatibility-hosted-canary-v1"
        or canary.get("identity") != expected_identity
        or canary.get("complete") is not True
        or canary.get("identity_advancement_allowed") is not True
        or projected != expected_modes
        or canary.get("fingerprint") != _canonical_sha256(preimage)
    ):
        raise SpecValidationError("hosted canary differs from authoritative proof")


def _read_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise InputBoundaryError(f"compatibility proof JSON must be an object: {path.name}")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
