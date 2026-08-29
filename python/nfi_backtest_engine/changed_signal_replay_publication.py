"""Deterministic published artifact set for changed-signal replays."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .errors import SpecValidationError

Mode = Literal["spot", "futures"]
Lane = Literal["official", "native"]


@dataclass(frozen=True, slots=True)
class PublishedArtifactSpec:
    """One stable producer output admitted to the public replay root."""

    role: str
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class ReplayPublication:
    """Complete identity and destination of one replay publication."""

    mode: Mode
    lane: Lane
    role_bindings_sha256: str
    destination: Path
    expected_manifest: Path


@dataclass(frozen=True, slots=True)
class PublicationValidation:
    """Promotion context for one tracked replay publication manifest."""

    mode: Mode
    lane: Lane
    manifest_path: Path
    provenance: Mapping[str, Any]
    role_bindings_sha256: str


_SPECS: Final[dict[Lane, tuple[PublishedArtifactSpec, ...]]] = {
    "official": (
        PublishedArtifactSpec(
            "official_execution", Path("official-execution.zip"), Path("execution.zip")
        ),
        PublishedArtifactSpec(
            "official_trace", Path("state-trace.nfitrace"), Path("state-trace.nfitrace")
        ),
    ),
    "native": (
        PublishedArtifactSpec(
            "native_execution",
            Path("research/simulation-result.json"),
            Path("simulation-result.json"),
        ),
        PublishedArtifactSpec(
            "native_events",
            Path("research/engine-events.jsonl"),
            Path("engine-events.jsonl"),
        ),
        PublishedArtifactSpec(
            "native_state",
            Path("engine-state-projected.trace"),
            Path("state-projection.nfitrace"),
        ),
    ),
}


def publish_replay_artifacts(
    private_root: Path,
    publication: ReplayPublication,
) -> dict[str, Any]:
    """Publish only the declared stable producer outputs and deterministic manifest."""
    staging = publication.destination.with_name(f".{publication.destination.name}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(publication.destination, ignore_errors=True)
    staging.mkdir(parents=True)
    records = []
    for spec in _SPECS[publication.lane]:
        source = private_root / spec.source
        if not source.is_file():
            raise SpecValidationError("changed signal published replay artifact is missing")
        payload = source.read_bytes()
        destination = staging / spec.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        records.append(
            {
                "role": spec.role,
                "path": spec.destination.as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        )
    manifest = _manifest(publication, records)
    expected = json.loads(publication.expected_manifest.read_text(encoding="utf-8"))
    if manifest != expected:
        raise SpecValidationError("changed signal published replay manifest differs")
    (staging / "manifest.json").write_bytes(_canonical_json(manifest))
    staging.replace(publication.destination)
    return manifest


def validate_publication_contract(validation: PublicationValidation) -> None:
    """Bind promotion evidence to the exact validator-owned published artifact set."""
    manifest = json.loads(validation.manifest_path.read_text(encoding="utf-8"))
    expected_roles = tuple(spec.role for spec in _SPECS[validation.lane])
    records = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != "changed-signal-replay-publication-v1"
        or manifest.get("mode") != validation.mode
        or manifest.get("lane") != validation.lane
        or manifest.get("role_bindings_sha256") != validation.role_bindings_sha256
        or not isinstance(records, list)
        or tuple(record.get("role") for record in records) != expected_roles
        or manifest.get("artifact_set_sha256") != _artifact_set_sha256(records)
    ):
        raise SpecValidationError("changed signal published replay contract differs")
    artifacts = {item["role"]: item for item in validation.provenance["artifacts"]}
    specs = _SPECS[validation.lane]
    for record, spec in zip(records, specs, strict=True):
        proof = artifacts.get(spec.role)
        if proof is None or record != {
            "role": spec.role,
            "path": spec.destination.as_posix(),
            "sha256": proof["sha256"],
            "bytes": proof["bytes"],
        }:
            raise SpecValidationError("changed signal published replay artifact set differs")


def publication_manifest_path(repository_root: Path, mode: Mode, lane: Lane) -> Path:
    """Return the code-owned tracked manifest location for one replay lane."""
    return repository_root / (
        f"benchmarks/evidence/m22/current-x7-raw/{mode}/"
        f"replay-publication-{lane}.json"
    )


def _manifest(
    publication: ReplayPublication,
    records: list[dict[str, str | int]],
) -> dict[str, Any]:
    return {
        "schema_version": "changed-signal-replay-publication-v1",
        "mode": publication.mode,
        "lane": publication.lane,
        "role_bindings_sha256": publication.role_bindings_sha256,
        "artifacts": records,
        "artifact_set_sha256": _artifact_set_sha256(records),
    }


def _artifact_set_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(records)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
