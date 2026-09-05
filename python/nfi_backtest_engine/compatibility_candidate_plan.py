"""Validation and planning for one bounded discovery fixture candidate."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .fixture import sha256_file, validate_fixture

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_FIXTURE_ID = re.compile(r"[a-z0-9][a-z0-9.-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_MODES: Final = {"spot", "futures"}


@dataclass(frozen=True, slots=True)
class CandidatePlanError(ValueError):
    """Candidate input violates the sealed planning contract."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class CandidatePublicationError(ValueError):
    """Candidate publication cannot proceed without violating trust."""

    message: str

    def __str__(self) -> str:
        return self.message


def candidate_branch(trading_mode: object, fingerprint: object) -> str:
    """Return the one automation branch reserved for a discovery request."""
    if trading_mode not in _MODES or _FINGERPRINT.fullmatch(str(fingerprint)) is None:
        raise CandidatePlanError("discovery candidate identity is invalid")
    return f"automation/{trading_mode}-fixture-{str(fingerprint)[:16]}"


def build_candidate_plan(
    report: Mapping[str, Any],
    candidate_directory: str | Path,
    repository_root: str | Path,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Validate candidate identity, exactness, size, and repository destinations."""
    root = Path(repository_root).resolve()
    candidate_input = Path(candidate_directory)
    if candidate_input.is_symlink():
        raise CandidatePlanError("fixture candidate root must not be a symlink")
    candidate_root = candidate_input.resolve()
    if not candidate_root.is_dir():
        raise CandidatePlanError("fixture candidate directory is missing")
    for path in candidate_root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise CandidatePlanError("fixture candidate contains a symlink or special file")
    if report.get("status") != "candidate_found":
        raise CandidatePlanError("discovery report does not contain a fixture candidate")
    fingerprint = report.get("fingerprint")
    trading_mode = report.get("trading_mode")
    candidate = report.get("candidate")
    if (
        _FINGERPRINT.fullmatch(str(fingerprint)) is None
        or trading_mode not in _MODES
        or not isinstance(candidate, Mapping)
    ):
        raise CandidatePlanError("discovery candidate identity is invalid")
    if (
        candidate.get("trade_surface_exact") is not True
        or candidate.get("full_state_exact") is not True
    ):
        raise CandidatePlanError("fixture candidate lacks independent exact evidence")
    manifest_path = candidate_root / "manifest.json"
    if not manifest_path.is_file():
        raise CandidatePlanError("fixture candidate manifest is missing")
    manifest = validate_fixture(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    fixture_id = manifest.get("fixture_id")
    if (
        not isinstance(fixture_id, str)
        or _FIXTURE_ID.fullmatch(fixture_id) is None
        or ".." in fixture_id
    ):
        raise CandidatePlanError("fixture candidate id is not a repository-safe slug")
    upstream_commit = report.get("upstream_commit")
    engine_commit = report.get("engine_commit")
    provenance = manifest.get("strategy_provenance")
    if (
        _COMMIT.fullmatch(str(upstream_commit)) is None
        or _COMMIT.fullmatch(str(engine_commit)) is None
        or not isinstance(provenance, Mapping)
        or provenance.get("upstream_commit") != upstream_commit
        or provenance.get("effective_source_sha256") != report.get("strategy_sha256")
        or manifest.get("freqtrade", {}).get("trading_mode") != trading_mode
        or candidate.get("fixture_id") != fixture_id
        or candidate.get("manifest_sha256") != manifest_sha256
    ):
        raise CandidatePlanError("fixture candidate identity differs from its sealed manifest")
    logical_bytes = sum(
        path.stat().st_size for path in candidate_root.rglob("*") if path.is_file()
    )
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
        or logical_bytes > max_bytes
        or candidate.get("logical_bytes") != logical_bytes
    ):
        raise CandidatePlanError("fixture candidate exceeds or differs from its sealed size")
    suffix = str(fingerprint)[:16]
    fixture_relative = Path("benchmarks") / "fixtures" / "captured" / fixture_id
    evidence_relative = Path("benchmarks") / "evidence" / (
        f"future-nfi-{trading_mode}-{suffix}.json"
    )
    if (root / fixture_relative).exists() or (root / evidence_relative).exists():
        raise CandidatePlanError("fixture candidate destination already exists")
    target_ids = candidate.get("target_ids")
    if not isinstance(target_ids, list) or not target_ids or not all(
        isinstance(value, str) and value for value in target_ids
    ):
        raise CandidatePlanError("fixture candidate target ids are invalid")
    return {
        "fingerprint": fingerprint,
        "branch": candidate_branch(trading_mode, fingerprint),
        "trading_mode": trading_mode,
        "fixture_id": fixture_id,
        "fixture_source": str(candidate_root),
        "fixture_destination": fixture_relative.as_posix(),
        "evidence_destination": evidence_relative.as_posix(),
        "logical_bytes": logical_bytes,
        "manifest_sha256": manifest_sha256,
        "target_ids": sorted(target_ids),
        "upstream_commit": upstream_commit,
        "engine_commit": engine_commit,
        "strategy_sha256": report.get("strategy_sha256"),
        "timerange": candidate.get("timerange"),
        "pair": candidate.get("pair"),
    }
