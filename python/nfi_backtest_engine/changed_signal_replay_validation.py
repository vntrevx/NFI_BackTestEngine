"""Promotion validation for changed-signal replay roots and role bindings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import read_json
from .changed_signal_manifest_roles import Mode
from .changed_signal_replay_publication import (
    PublicationValidation,
    publication_manifest_path,
    validate_publication_contract,
)
from .changed_signal_role_binding import (
    resolve_replay_role_bindings,
    role_bindings_sha256,
)
from .errors import SpecValidationError
from .fixture import validate_fixture


@dataclass(frozen=True, slots=True)
class ReplayRootValidation:
    """Promotion context for one authenticated replay root."""

    repository_root: Path
    mode: Mode
    provenance: Mapping[str, Any]
    capture: Mapping[str, Any]
    official: bool


def validate_replay_root(validation: ReplayRootValidation) -> None:
    """Bind manifest roles, publication set, runtime, and trust identities."""
    repository_root = validation.repository_root
    provenance = validation.provenance
    mode = validation.mode
    manifest_path = _artifact_path(repository_root, provenance, "replay_manifest")
    expected = repository_root / (
        f"benchmarks/evidence/m22/current-x7-raw/{mode}/replay/manifest.json"
    )
    if manifest_path != expected or not validate_fixture(manifest_path):
        raise SpecValidationError("changed signal clean-room replay root differs")
    manifest = read_json(manifest_path)
    lane = "official" if validation.official else "native"
    bindings = resolve_replay_role_bindings(mode, lane, manifest_path, repository_root)
    bindings_digest = role_bindings_sha256(bindings)
    if provenance["role_bindings_sha256"] != bindings_digest:
        raise SpecValidationError("changed signal replay role digest differs")
    published_manifest = _artifact_path(
        repository_root,
        provenance,
        "published_manifest",
    )
    if published_manifest != publication_manifest_path(repository_root, mode, lane):
        raise SpecValidationError("changed signal published replay manifest path differs")
    validate_publication_contract(
        PublicationValidation(
            mode=mode,
            lane=lane,
            manifest_path=published_manifest,
            provenance=provenance,
            role_bindings_sha256=bindings_digest,
        )
    )
    artifacts = {item["role"]: item for item in provenance["artifacts"]}
    for binding in bindings:
        artifact = artifacts.get(binding.role)
        if artifact is None or (
            _artifact_path(repository_root, provenance, binding.role) != binding.path
            or artifact["sha256"] != binding.sha256
            or artifact["bytes"] != binding.bytes
        ):
            raise SpecValidationError("changed signal replay role binding differs")
    freqtrade = manifest["freqtrade"]
    capture = validation.capture
    if (
        freqtrade["version"] != capture["freqtrade_version"]
        or freqtrade["image_index_digest"] != capture["image_index_digest"]
        or freqtrade["trading_mode"] != mode
        or freqtrade["strategy"] != "CurrentChangedPredicateContract"
    ):
        raise SpecValidationError("changed signal replay runtime identity differs")


def _artifact_path(
    repository_root: Path,
    provenance: Mapping[str, Any],
    role: str,
) -> Path:
    records = [item for item in provenance["artifacts"] if item["role"] == role]
    if len(records) != 1:
        raise SpecValidationError("changed signal required artifact role differs")
    path = (repository_root / records[0]["path"]).resolve()
    if not path.is_relative_to(repository_root) or not path.is_file():
        raise SpecValidationError("changed signal artifact path escapes repository")
    return path
