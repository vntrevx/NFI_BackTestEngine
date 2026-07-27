"""Fail-closed binding of a built candidate to host and platform certificates."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .specs import (
    FULL_X7_CERTIFICATION_V2_SCHEMA,
    validate_release_gate,
    validate_schema,
)

RELEASE_GATE_VERSION = "1.0.0"
CANDIDATE_CHECKSUMS_NAME = "SHA256SUMS.txt"
RELEASE_CHECKSUMS_NAME = "RELEASE-SHA256SUMS.txt"
RELEASE_GATE_NAME = "release-gate.json"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SYSTEMS = frozenset({"windows", "linux", "darwin"})


def seal_release_gate(
    *,
    candidate_directory: str | Path,
    certificate_path: str | Path,
    certificate_evidence_path: str | Path,
    platform_evidence_path: str | Path,
    candidate_commit: str,
    output_directory: str | Path,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Validate identities and copy a build-once candidate into a publishable bundle."""
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise SpecValidationError("candidate commit must be a 40-character lowercase Git SHA")
    candidate_root = _plain_directory(candidate_directory, label="candidate")
    raw_output = Path(output_directory).absolute()
    if raw_output.is_symlink():
        raise SpecValidationError("release gate output must not be a symlink")
    output = raw_output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BenchmarkError(f"release gate output must be empty: {output}")

    candidate = _validate_candidate_manifest(candidate_root)
    certificate_file = _plain_file(certificate_path, label="host certificate")
    certificate_evidence = _plain_file(
        certificate_evidence_path,
        label="host certificate evidence",
    )
    candidate_names = {path.name for path in candidate["paths"]}
    certificate_names = {certificate_file.name, certificate_evidence.name}
    if len(certificate_names) != 2 or candidate_names & certificate_names:
        raise SpecValidationError("release assets have a filename collision")
    platform_file = _plain_file(platform_evidence_path, label="platform evidence")
    if platform_file.parent != candidate_root:
        raise SpecValidationError("platform evidence must come from the sealed candidate bundle")
    platform_record = candidate["files"].get(platform_file.name)
    if (
        platform_record is None
        or platform_record["sha256"] != sha256_file(platform_file)
    ):
        raise SpecValidationError("platform evidence is absent from candidate SHA256SUMS.txt")

    certificate = _load_certificate(certificate_file)
    _validate_certificate_archive(certificate_evidence, certificate)
    platform = _load_platform_evidence(platform_file)
    identities = _match_release_identities(
        candidate,
        certificate,
        platform,
    )

    output.mkdir(parents=True, exist_ok=True)
    for source in candidate["paths"]:
        _copy_new_file(source, output / source.name)
    _copy_new_file(certificate_file, output / certificate_file.name)
    _copy_new_file(certificate_evidence, output / certificate_evidence.name)

    source_assets = _asset_records(
        path
        for path in output.iterdir()
        if path.name not in {RELEASE_GATE_NAME, RELEASE_CHECKSUMS_NAME}
    )
    gate = {
        "schema_version": RELEASE_GATE_VERSION,
        "created_at": created_at
        or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "release_certified",
        "release_certified": True,
        "candidate_commit": candidate_commit,
        "package_version": identities["package_version"],
        "candidate_manifest": _artifact_record(output / CANDIDATE_CHECKSUMS_NAME),
        "certificate": {
            **_artifact_record(output / certificate_file.name),
            "evidence": _artifact_record(output / certificate_evidence.name),
            "mode_contract": identities["mode_contract"],
            "wheel_sha256": identities["wheel_sha256"],
            "portable_package_sha256": identities["portable_package_sha256"],
        },
        "platform_evidence": {
            **_artifact_record(output / platform_file.name),
            "systems": sorted(_REQUIRED_SYSTEMS),
            "portable_package_sha256": identities["portable_package_sha256"],
        },
        "gates": {
            "candidate_manifest": True,
            "candidate_commit": True,
            "host_certificate": True,
            "candidate_wheel": True,
            "portable_package": True,
            "three_os_evidence": True,
            "preview_rejected": True,
        },
        "sealed_assets": source_assets,
    }
    validate_release_gate(gate)
    write_json(output / RELEASE_GATE_NAME, gate)
    _write_complete_checksums(output)
    verify_release_gate(output, expected_commit=candidate_commit)
    return gate


def verify_release_gate(
    source: str | Path,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Verify the complete checksum manifest and certified promotion verdict."""
    root = _plain_directory(source, label="release gate")
    manifest = root / RELEASE_CHECKSUMS_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise SpecValidationError("release gate has no complete checksum manifest")
    records = _parse_checksum_manifest(manifest)
    actual_names = {
        path.name
        for path in root.iterdir()
        if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME
    }
    if set(records) != actual_names:
        raise SpecValidationError("release checksum manifest does not cover every asset")
    for name, expected in records.items():
        target = root / name
        if target.is_symlink() or sha256_file(target) != expected:
            raise SpecValidationError(f"release asset failed checksum validation: {name}")

    document = read_json(root / RELEASE_GATE_NAME)
    validate_release_gate(document)
    if (
        not isinstance(document, dict)
        or document.get("release_certified") is not True
        or document.get("status") != "release_certified"
    ):
        raise SpecValidationError("release gate is preview or uncertified")
    if expected_commit is not None and document.get("candidate_commit") != expected_commit:
        raise SpecValidationError("release gate candidate commit differs")
    return document


def _validate_candidate_manifest(root: Path) -> dict[str, Any]:
    paths = sorted(root.iterdir(), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise SpecValidationError("candidate bundle must contain only regular top-level files")
    manifest = root / CANDIDATE_CHECKSUMS_NAME
    if not manifest.is_file():
        raise SpecValidationError("candidate bundle is missing SHA256SUMS.txt")
    records = _parse_checksum_manifest(manifest)
    actual = {path.name for path in paths if path.name != CANDIDATE_CHECKSUMS_NAME}
    if set(records) != actual:
        raise SpecValidationError("candidate SHA256SUMS.txt does not cover every candidate asset")
    for name, expected in records.items():
        if sha256_file(root / name) != expected:
            raise SpecValidationError(f"candidate asset failed checksum validation: {name}")
    if RELEASE_CHECKSUMS_NAME in actual or RELEASE_GATE_NAME in actual:
        raise SpecValidationError("candidate bundle already contains release-gate output")
    return {
        "root": root,
        "paths": paths,
        "files": {
            name: {
                "path": root / name,
                "sha256": digest,
            }
            for name, digest in records.items()
        },
    }


def _load_certificate(path: Path) -> dict[str, Any]:
    document = read_json(path)
    validate_schema(document, FULL_X7_CERTIFICATION_V2_SCHEMA)
    if not isinstance(document, dict):
        raise SpecValidationError("host certificate must be a JSON object")
    installed = document.get("gates", {}).get("installed_wheel")
    if (
        document.get("status") != "certified"
        or document.get("release_certified") is not True
        or not isinstance(installed, dict)
        or installed.get("met") is not True
        or installed.get("installed_extension_equal") is not True
        or not _is_sha256(installed.get("sha256"))
        or not _is_sha256(installed.get("portable_package_sha256"))
    ):
        raise SpecValidationError("host certificate is preview, failed, or incomplete")
    return document


def _validate_certificate_archive(path: Path, certificate: Mapping[str, Any]) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            matches = []
            for name in archive.namelist():
                if not name.endswith("full-x7-certification.json"):
                    continue
                try:
                    document = json.loads(archive.read(name))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise SpecValidationError(
                        "host certificate evidence contains invalid JSON"
                    ) from exc
                if document == certificate:
                    matches.append(name)
    except zipfile.BadZipFile as exc:
        raise SpecValidationError("host certificate evidence is not a ZIP archive") from exc
    if len(matches) != 1:
        raise SpecValidationError(
            "host certificate evidence does not contain exactly one matching certificate"
        )


def _load_platform_evidence(path: Path) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict):
        raise SpecValidationError("platform evidence must be a JSON object")
    platforms = document.get("platforms")
    platform_records = (
        [item for item in platforms if isinstance(item, dict)]
        if isinstance(platforms, list)
        else []
    )
    systems = (
        {item.get("system") for item in platform_records}
        if len(platform_records) == 3
        else set()
    )
    if (
        document.get("schema_version") != "1.0.0"
        or document.get("release_certified") is not True
        or document.get("lane") != "exact-fixture"
        or not isinstance(document.get("package_version"), str)
        or not document["package_version"]
        or len(platform_records) != 3
        or systems != _REQUIRED_SYSTEMS
        or any(
            not _is_sha256(item.get("wheel_sha256"))
            for item in platform_records
        )
        or not _is_sha256(document.get("portable_package_sha256"))
    ):
        raise SpecValidationError("platform evidence is preview, incomplete, or not three-OS")
    return document


def _match_release_identities(
    candidate: Mapping[str, Any],
    certificate: Mapping[str, Any],
    platform: Mapping[str, Any],
) -> dict[str, str]:
    installed = certificate["gates"]["installed_wheel"]
    wheel_sha = str(installed["sha256"])
    portable_sha = str(installed["portable_package_sha256"])
    version = str(certificate["environment"]["package_version"])
    mode = str(certificate["claim_scope"]["mode_contract"])
    matching_wheels = [
        name
        for name, record in candidate["files"].items()
        if name.endswith(".whl") and record["sha256"] == wheel_sha
    ]
    if len(matching_wheels) != 1:
        raise SpecValidationError(
            "host certificate wheel SHA does not identify exactly one candidate wheel"
        )
    version_prefix = f"nfi_backtest_engine-{version}-"
    if not matching_wheels[0].startswith(version_prefix):
        raise SpecValidationError("host certificate package version differs from candidate wheel")
    if (
        platform.get("mode_contract") != mode
        or platform.get("package_version") != version
        or platform.get("portable_package_sha256") != portable_sha
    ):
        raise SpecValidationError(
            "host certificate portable package differs from three-OS evidence"
        )
    platform_wheels = {
        item.get("wheel_sha256")
        for item in platform["platforms"]
        if isinstance(item, dict)
    }
    if wheel_sha not in platform_wheels:
        raise SpecValidationError(
            "host certificate wheel is absent from three-OS platform evidence"
        )
    return {
        "package_version": version,
        "mode_contract": mode,
        "wheel_sha256": wheel_sha,
        "portable_package_sha256": portable_sha,
    }


def _parse_checksum_manifest(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        digest, separator, raw_name = line.partition("  ")
        name = raw_name.removeprefix("./")
        if (
            not separator
            or _SHA256_PATTERN.fullmatch(digest) is None
            or not name
            or "/" in name
            or "\\" in name
            or name in records
        ):
            raise SpecValidationError(
                f"invalid checksum manifest record at {path.name}:{line_number}"
            )
        records[name] = digest
    if not records:
        raise SpecValidationError(f"checksum manifest is empty: {path.name}")
    return records


def _write_complete_checksums(root: Path) -> None:
    assets = sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME
    )
    lines = [f"{sha256_file(path)}  {path.name}" for path in assets]
    (root / RELEASE_CHECKSUMS_NAME).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _asset_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [_artifact_record(path) for path in sorted(paths, key=lambda item: item.name)]


def _artifact_record(path: Path) -> dict[str, Any]:
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _copy_new_file(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SpecValidationError(f"release asset name collision: {destination.name}")
    shutil.copyfile(source, destination)


def _plain_directory(source: str | Path, *, label: str) -> Path:
    raw = Path(source).absolute()
    if raw.is_symlink():
        raise SpecValidationError(f"{label} directory must not be a symlink")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise SpecValidationError(f"{label} directory does not exist: {resolved}")
    return resolved


def _plain_file(source: str | Path, *, label: str) -> Path:
    raw = Path(source).absolute()
    if raw.is_symlink():
        raise SpecValidationError(f"{label} must not be a symlink")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise SpecValidationError(f"{label} does not exist: {resolved}")
    return resolved


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None
