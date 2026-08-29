"""Validator-owned trust anchors for changed-signal source and capture roles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .changed_signal_filesystem_trust import TrustedFileSnapshot, read_stable_file
from .changed_signal_git_trust import (
    UPSTREAM_SOURCE_CONTRACT,
    UpstreamSourceContract,
    resolve_upstream_source,
)
from .errors import SpecValidationError


@dataclass(frozen=True, slots=True)
class CaptureContract:
    """Code-owned identity for one executable capture implementation."""

    version: str
    implementation_path: Path
    implementation_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedCapture:
    """Registry contract joined to one immutable implementation snapshot."""

    contract: CaptureContract
    snapshot: TrustedFileSnapshot


_OFFICIAL_CAPTURE_VERSION: Final = "changed-signal-official-v1"
_CAPTURE_CONTRACTS: Final = {
    _OFFICIAL_CAPTURE_VERSION: CaptureContract(
        version=_OFFICIAL_CAPTURE_VERSION,
        implementation_path=Path("benchmarks/reference/capture/current_changed_signal.py"),
        implementation_sha256="f83111eff6e9a3b2b47493ecc3284755c387b678acd161638919382c8fb2d745",
    )
}


def trusted_upstream_source(
    repository_root: Path,
    replay_source: Path,
) -> TrustedFileSnapshot:
    """Match one stable replay snapshot to replacement-immune canonical Git bytes."""
    expected = repository_root / (
        "benchmarks/evidence/m22/current-x7-raw/"
        "upstream-NostalgiaForInfinityX7.source"
    )
    snapshot = read_stable_file(replay_source, expected)
    canonical = resolve_upstream_source(repository_root)
    if snapshot.payload != canonical:
        raise SpecValidationError("changed signal replay source differs from upstream Git blob")
    return snapshot


def official_capture_contract() -> CaptureContract:
    """Return the validator-owned official capture contract."""
    return _CAPTURE_CONTRACTS[_OFFICIAL_CAPTURE_VERSION]


def validate_official_capture(
    repository_root: Path,
    implementation: Path,
) -> ValidatedCapture:
    """Match one stable capture snapshot to the validator-owned registry."""
    contract = official_capture_contract()
    expected = repository_root / contract.implementation_path
    snapshot = read_stable_file(implementation, expected)
    digest = hashlib.sha256(snapshot.payload).hexdigest()
    if digest != contract.implementation_sha256:
        raise SpecValidationError("changed signal capture implementation contract differs")
    return ValidatedCapture(contract=contract, snapshot=snapshot)


def official_capture_attestation(implementation: Path, mode: str) -> dict[str, str | list[str]]:
    """Build producer output only after its executing bytes satisfy the registry."""
    snapshot = read_stable_file(implementation, implementation)
    contract = official_capture_contract()
    digest = hashlib.sha256(snapshot.payload).hexdigest()
    if digest != contract.implementation_sha256:
        raise SpecValidationError("changed signal executing capture implementation differs")
    return expected_official_capture_attestation(mode)


def expected_official_capture_attestation(mode: str) -> dict[str, str | list[str]]:
    """Return the command/output identity required from the official producer."""
    contract = official_capture_contract()
    return {
        "version": contract.version,
        "path": contract.implementation_path.as_posix(),
        "sha256": contract.implementation_sha256,
        "command": ["python", contract.implementation_path.as_posix(), mode],
    }


__all__ = [
    "UPSTREAM_SOURCE_CONTRACT",
    "UpstreamSourceContract",
    "ValidatedCapture",
    "expected_official_capture_attestation",
    "official_capture_attestation",
    "official_capture_contract",
    "trusted_upstream_source",
    "validate_official_capture",
]
