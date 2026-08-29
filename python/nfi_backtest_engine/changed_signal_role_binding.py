"""Canonical replay-input role bindings shared by replay and promotion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, assert_never

from .canonical import read_json
from .changed_signal_filesystem_trust import (
    FileIdentity,
    read_stable_file,
    validate_distinct_files,
)
from .changed_signal_manifest_roles import Mode, resolve_manifest_bindings
from .changed_signal_trust import (
    UPSTREAM_SOURCE_CONTRACT,
    trusted_upstream_source,
    validate_official_capture,
)

Lane = Literal["official", "native"]

_UPSTREAM_PATH: Final = Path(
    "benchmarks/evidence/m22/current-x7-raw/upstream-NostalgiaForInfinityX7.source"
)
_CAPTURE_PATHS: Final[dict[Lane, Path]] = {
    "official": Path("benchmarks/reference/capture/current_changed_signal.py"),
    "native": Path("python/nfi_backtest_engine/changed_signal_native_capture.py"),
}


@dataclass(frozen=True, slots=True)
class ReplayRoleBinding:
    """One exact file identity consumed by a replay producer."""

    role: str
    path: Path
    sha256: str
    bytes: int
    identity: FileIdentity
    trust: tuple[tuple[str, str], ...] = ()


def resolve_replay_role_bindings(
    mode: Mode,
    lane: Lane,
    manifest_path: Path,
    repository_root: Path,
) -> tuple[ReplayRoleBinding, ...]:
    """Resolve and semantically check the canonical replay files for one lane."""
    manifest = read_json(manifest_path)
    records = [
        ReplayRoleBinding(
            role=role,
            path=path,
            sha256=digest,
            bytes=size,
            identity=identity,
        )
        for role, path, digest, size, identity in resolve_manifest_bindings(
            mode,
            manifest,
            manifest_path.parent,
        )
    ]
    records.extend(
        (
            _source_binding(repository_root, repository_root / _UPSTREAM_PATH),
            _capture_binding(lane, repository_root, repository_root / _CAPTURE_PATHS[lane]),
            _repository_binding("replay_manifest", manifest_path),
        )
    )
    validate_distinct_files(tuple(record.identity for record in records))
    return tuple(records)


def role_bindings_sha256(bindings: tuple[ReplayRoleBinding, ...]) -> str:
    """Return the canonical digest embedded in replay completion output."""
    payload = []
    for binding in bindings:
        record: dict[str, str | int | dict[str, str]] = {
            "role": binding.role,
            "sha256": binding.sha256,
            "bytes": binding.bytes,
        }
        if binding.trust:
            record["trust"] = dict(binding.trust)
        payload.append(record)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_binding(repository_root: Path, path: Path) -> ReplayRoleBinding:
    snapshot = trusted_upstream_source(repository_root, path)
    contract = UPSTREAM_SOURCE_CONTRACT
    return ReplayRoleBinding(
        role="source_input",
        path=path,
        sha256=hashlib.sha256(snapshot.payload).hexdigest(),
        bytes=len(snapshot.payload),
        identity=snapshot.metadata.identity,
        trust=(
            ("repository", contract.repository_url),
            ("current_refs", ",".join(contract.current_refs)),
            ("commit", contract.commit),
            ("tree_oid", contract.tree_oid),
            ("path", contract.source_path),
            ("blob_oid", contract.blob_oid),
        ),
    )


def _capture_binding(lane: Lane, repository_root: Path, path: Path) -> ReplayRoleBinding:
    match lane:
        case "official":
            validated = validate_official_capture(repository_root, path)
            contract = validated.contract
            return ReplayRoleBinding(
                role="capture_input",
                path=path,
                sha256=hashlib.sha256(validated.snapshot.payload).hexdigest(),
                bytes=len(validated.snapshot.payload),
                identity=validated.snapshot.metadata.identity,
                trust=(
                    ("version", contract.version),
                    ("path", contract.implementation_path.as_posix()),
                    ("sha256", contract.implementation_sha256),
                ),
            )
        case "native":
            return _repository_binding("capture_input", path)
        case unreachable:
            assert_never(unreachable)


def _repository_binding(role: str, path: Path) -> ReplayRoleBinding:
    snapshot = read_stable_file(path, path)
    return ReplayRoleBinding(
        role=role,
        path=path,
        sha256=hashlib.sha256(snapshot.payload).hexdigest(),
        bytes=len(snapshot.payload),
        identity=snapshot.metadata.identity,
    )
