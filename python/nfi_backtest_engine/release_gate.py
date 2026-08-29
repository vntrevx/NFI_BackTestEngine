"""Fail-closed binding of a built candidate to host and platform certificates."""

from __future__ import annotations

import re
import shutil
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive_security import read_zip_member, validate_zip_archive
from .canonical import loads_json_bytes, read_json, write_json
from .certification_policy import validate_certification_semantics
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .portable_paths import (
    parse_portable_relative_path,
    validate_portable_filesystem_path,
)
from .release_provenance import (
    DEFAULT_PROVENANCE_POLICY,
    PLATFORM_EVIDENCE_VERSION,
    ProvenancePolicy,
    abort_certificate_publication,
    candidate_distribution_identity,
    mark_certificate_published,
    reserve_certificate_publication,
    verify_embedded_platform_evidence,
)
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
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    provenance_ledger_path: str | Path | None = None,
    publication_attempt_id: str | None = None,
    native_score_evidence_path: str | Path | None = None,
    native_score_identity_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate identities and copy a build-once candidate into a publishable bundle."""
    from .native_scorecard import (
        require_fresh_current_ref_for_authorization,
        require_native_scorecard_candidate_binding,
        require_native_scorecard_for_promotion,
    )

    require_native_scorecard_for_promotion(
        native_score_evidence_path,
        expected_identity_path=native_score_identity_path,
        provenance_policy=provenance_policy,
        authorization_operation="release-gate-seal",
    )
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise SpecValidationError("candidate commit must be a 40-character lowercase Git SHA")
    ledger_enabled = provenance_ledger_path is not None
    if ledger_enabled != (publication_attempt_id is not None) or (
        publication_attempt_id is not None
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", publication_attempt_id) is None
    ):
        raise SpecValidationError(
            "durable release gate publication requires a ledger and publication attempt"
        )
    candidate_root = _plain_directory(candidate_directory, label="candidate")
    raw_output = validate_portable_filesystem_path(output_directory)
    if raw_output.is_symlink():
        raise SpecValidationError("release gate output must not be a symlink")
    output = raw_output.resolve()
    if output.exists() and (
        not output.is_dir() or (any(output.iterdir()) and not ledger_enabled)
    ):
        raise BenchmarkError(f"release gate output must be empty: {output}")

    candidate = _validate_candidate_manifest(candidate_root)
    require_native_scorecard_candidate_binding(
        native_score_identity_path,
        expected_candidate_commit=candidate_commit,
        expected_candidate_identity=_candidate_distribution_id(candidate),
    )
    certificate_file = _plain_file(certificate_path, label="host certificate")
    certificate_evidence = _plain_file(
        certificate_evidence_path,
        label="host certificate evidence",
    )
    candidate_names = {
        path.relative_to(candidate_root).as_posix()
        for path in candidate["paths"]
    }
    certificate_names = {certificate_file.name, certificate_evidence.name}
    if len(certificate_names) != 2 or candidate_names & certificate_names:
        raise SpecValidationError("release assets have a filename collision")
    platform_file = _plain_file(platform_evidence_path, label="platform evidence")
    if not platform_file.is_relative_to(candidate_root):
        raise SpecValidationError("platform evidence must come from the sealed candidate bundle")
    platform_relative = platform_file.relative_to(candidate_root).as_posix()
    platform_record = candidate["files"].get(platform_relative)
    if (
        platform_record is None
        or platform_record["sha256"] != sha256_file(platform_file)
    ):
        raise SpecValidationError("platform evidence is absent from candidate SHA256SUMS.txt")

    certificate = _load_certificate(certificate_file)
    _validate_certificate_archive(certificate_evidence, certificate)
    candidate_id = _candidate_distribution_id(candidate)
    platform = _load_platform_evidence(
        platform_file,
        provenance_policy=provenance_policy,
        expected_commit=candidate_commit,
        expected_candidate_id=candidate_id,
    )
    identities = _match_release_identities(
        candidate,
        certificate,
        platform,
    )
    bundle_id = str(platform["provenance"]["bundle_id"])
    if ledger_enabled and output.exists() and any(output.iterdir()):
        assert provenance_ledger_path is not None
        assert publication_attempt_id is not None
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "release-gate-recovery-reservation",
        )
        recovered = verify_release_gate(
            output,
            expected_commit=candidate_commit,
            provenance_policy=provenance_policy,
        )
        certificate_sha256 = sha256_file(output / RELEASE_GATE_NAME)
        state = reserve_certificate_publication(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        if state == "published":
            raise SpecValidationError("release provenance bundle was already published")
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "release-gate-recovery-publication",
        )
        mark_certificate_published(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        return recovered
    if output.exists():
        output.rmdir()

    stage = (
        output.parent / f".{output.name}.stage-{publication_attempt_id}"
        if ledger_enabled
        else output
    )
    if stage.exists():
        if stage.is_symlink() or not stage.is_dir():
            raise BenchmarkError(f"release gate stage must be a directory: {stage}")
        shutil.rmtree(stage)
    require_fresh_current_ref_for_authorization(
        native_score_evidence_path,
        native_score_identity_path,
        "release-gate-output",
    )
    stage.mkdir(parents=True, mode=0o700)
    reserved = False
    exposed = False
    certificate_sha256 = ""
    try:
        for source in candidate["paths"]:
            relative = source.relative_to(candidate_root)
            _copy_new_file(source, stage / relative)
        _copy_new_file(certificate_file, stage / certificate_file.name)
        _copy_new_file(certificate_evidence, stage / certificate_evidence.name)

        source_assets = _asset_records(
            (
                path
                for path in stage.rglob("*")
                if path.is_file()
                and path.name not in {RELEASE_GATE_NAME, RELEASE_CHECKSUMS_NAME}
            ),
            relative_to=stage,
        )
        gate = {
            "schema_version": RELEASE_GATE_VERSION,
            "created_at": created_at
            or (
                str(platform["created_at"])
                if ledger_enabled
                else datetime.now(UTC).isoformat().replace("+00:00", "Z")
            ),
            "status": "release_certified",
            "release_certified": True,
            "candidate_commit": candidate_commit,
            "package_version": identities["package_version"],
            "candidate_manifest": _artifact_record(stage / CANDIDATE_CHECKSUMS_NAME),
            "certificate": {
                **_artifact_record(stage / certificate_file.name),
                "evidence": _artifact_record(stage / certificate_evidence.name),
                "mode_contract": identities["mode_contract"],
                "wheel_sha256": identities["wheel_sha256"],
                "portable_package_sha256": identities["portable_package_sha256"],
            },
            "platform_evidence": {
                **_artifact_record(stage / platform_relative, relative_to=stage),
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
                "native_scorecard": True,
            },
            "sealed_assets": source_assets,
        }
        validate_release_gate(gate)
        write_json(stage / RELEASE_GATE_NAME, gate)
        _write_complete_checksums(stage)
        verify_release_gate(
            stage,
            expected_commit=candidate_commit,
            provenance_policy=provenance_policy,
        )
        if not ledger_enabled:
            return gate

        assert provenance_ledger_path is not None
        assert publication_attempt_id is not None
        certificate_sha256 = sha256_file(stage / RELEASE_GATE_NAME)
        _publication_checkpoint("before-reservation")
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "release-gate-reservation",
        )
        state = reserve_certificate_publication(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        if state == "published":
            raise SpecValidationError("release provenance bundle was already used")
        reserved = True
        _publication_checkpoint("after-reservation")
        _publication_checkpoint("during-staged-publication")
        stage.rename(output)
        exposed = True
        _publication_checkpoint("after-publication-before-finalize")
        verify_release_gate(
            output,
            expected_commit=candidate_commit,
            provenance_policy=provenance_policy,
        )
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "release-gate-publication",
        )
        mark_certificate_published(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        return gate
    except BaseException:
        if stage.exists() and (stage != output or not exposed):
            shutil.rmtree(stage)
        if reserved and not exposed:
            assert provenance_ledger_path is not None
            assert publication_attempt_id is not None
            abort_certificate_publication(
                provenance_ledger_path,
                bundle_id=bundle_id,
                certificate_sha256=certificate_sha256,
                attempt_id=publication_attempt_id,
            )
        raise


def _publication_checkpoint(_name: str) -> None:
    """Deterministic interruption hook for durable publication tests."""


def verify_release_gate(
    source: str | Path,
    *,
    expected_commit: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
) -> dict[str, Any]:
    """Verify the complete checksum manifest and certified promotion verdict."""
    root = _plain_directory(source, label="release gate")
    manifest = root / RELEASE_CHECKSUMS_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise SpecValidationError("release gate has no complete checksum manifest")
    records = _parse_checksum_manifest(manifest)
    actual_names = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME
    }
    if set(records) != actual_names:
        raise SpecValidationError("release checksum manifest does not cover every asset")
    for name, expected in records.items():
        target = root / name
        if not target.is_file() or target.is_symlink() or sha256_file(target) != expected:
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
    candidate_records = _parse_checksum_manifest(root / CANDIDATE_CHECKSUMS_NAME)
    candidate = {
        "root": root,
        "files": {
            name: {"path": root / name, "sha256": digest}
            for name, digest in candidate_records.items()
        },
    }
    certificate_record = document["certificate"]
    certificate_path = root / certificate_record["file"]
    certificate = _load_certificate(certificate_path)
    _validate_certificate_archive(
        root / certificate_record["evidence"]["file"], certificate
    )
    platform_record = document["platform_evidence"]
    platform = _load_platform_evidence(
        root / platform_record["file"],
        provenance_policy=provenance_policy,
        expected_commit=document["candidate_commit"],
        expected_candidate_id=_candidate_distribution_id(candidate),
    )
    identities = _match_release_identities(candidate, certificate, platform)
    if (
        document["package_version"] != identities["package_version"]
        or certificate_record["wheel_sha256"] != identities["wheel_sha256"]
        or certificate_record["portable_package_sha256"]
        != identities["portable_package_sha256"]
        or platform_record["portable_package_sha256"]
        != identities["portable_package_sha256"]
    ):
        raise SpecValidationError("release gate recomputed identities differ")
    return document


def _validate_candidate_manifest(root: Path) -> dict[str, Any]:
    entries = sorted(
        root.rglob("*"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if any(
        path.is_symlink() or (not path.is_file() and not path.is_dir())
        for path in entries
    ):
        raise SpecValidationError("candidate bundle must contain only regular files")
    paths = [path for path in entries if path.is_file()]
    manifest = root / CANDIDATE_CHECKSUMS_NAME
    if not manifest.is_file():
        raise SpecValidationError("candidate bundle is missing SHA256SUMS.txt")
    records = _parse_checksum_manifest(manifest)
    actual = {
        path.relative_to(root).as_posix()
        for path in paths
        if path != manifest
    }
    if set(records) != actual:
        raise SpecValidationError("candidate SHA256SUMS.txt does not cover every candidate asset")
    for name, expected in records.items():
        target = (root / name).resolve()
        if (
            not target.is_relative_to(root)
            or not target.is_file()
            or target.is_symlink()
            or sha256_file(target) != expected
        ):
            raise SpecValidationError(f"candidate asset failed checksum validation: {name}")
    if any(
        PurePosixPath(name).name in {RELEASE_CHECKSUMS_NAME, RELEASE_GATE_NAME}
        for name in actual
    ):
        raise SpecValidationError("candidate bundle already contains release-gate output")
    return {
        "root": root,
        "paths": paths,
        "files": {
            name: {
                "path": (root / name).resolve(),
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
    try:
        validate_certification_semantics(document, label="host certificate")
    except SpecValidationError as exc:
        raise SpecValidationError(
            "host certificate is preview, failed, or incomplete"
        ) from exc
    gates = document.get("gates")
    installed = document.get("gates", {}).get("installed_wheel")
    recomputed_certified = bool(
        isinstance(gates, dict)
        and gates
        and all(isinstance(gate, dict) and gate.get("met") is True for gate in gates.values())
    )
    if (
        document.get("status") != "certified"
        or document.get("release_certified") is not True
        or not recomputed_certified
        or not isinstance(installed, dict)
        or installed.get("met") is not True
        or not _is_sha256(installed.get("sha256"))
        or not _is_sha256(installed.get("portable_package_sha256"))
    ):
        raise SpecValidationError("host certificate is preview, failed, or incomplete")
    return document


def _validate_certificate_archive(path: Path, certificate: Mapping[str, Any]) -> None:
    validate_portable_filesystem_path(path)
    if path.is_symlink() or not path.is_file():
        raise SpecValidationError("host certificate evidence must be a regular non-symlink ZIP")
    try:
        with zipfile.ZipFile(path) as archive:
            members = validate_zip_archive(archive)
            matches = []
            for name, info in members.items():
                if not name.endswith("full-x7-certification.json"):
                    continue
                try:
                    document = loads_json_bytes(read_zip_member(archive, info))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise SpecValidationError(
                        "host certificate evidence contains invalid JSON"
                    ) from exc
                if document == certificate:
                    matches.append(name)
    except (ValueError, zipfile.BadZipFile) as exc:
        raise SpecValidationError("host certificate evidence is not a ZIP archive") from exc
    if len(matches) != 1:
        raise SpecValidationError(
            "host certificate evidence does not contain exactly one matching certificate"
        )


def _load_platform_evidence(
    path: Path,
    *,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    expected_commit: str | None = None,
    expected_candidate_id: str | None = None,
) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict):
        raise SpecValidationError("platform evidence must be a JSON object")
    validate_certification_semantics(document, label="platform evidence")
    verified = verify_embedded_platform_evidence(
        document,
        policy=provenance_policy,
        expected_commit=expected_commit,
        expected_candidate_id=expected_candidate_id,
    )
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
        document.get("schema_version") != PLATFORM_EVIDENCE_VERSION
        or document.get("release_certified") is not True
        or document.get("candidate_commit") != verified["commit"]
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
    report_by_system = {
        report["platform"]["system"]: report for report in verified["reports"]
    }
    for item in platform_records:
        report = report_by_system.get(item.get("system"))
        if report is None or any(
            item.get(key) != report["package"].get(key)
            for key in ("wheel_sha256", "native_extension_sha256")
        ):
            raise SpecValidationError("platform evidence projection differs from signed reports")
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
    linux = next(
        (item for item in platform["platforms"] if item.get("system") == "linux"),
        None,
    )
    if (
        not isinstance(linux, dict)
        or linux.get("native_extension_sha256") != installed.get("native_member_sha256")
    ):
        raise SpecValidationError(
            "host certificate native extension differs from signed Linux evidence"
        )
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


def _candidate_distribution_id(candidate: Mapping[str, Any]) -> str:
    records = {
        name: str(record["sha256"])
        for name, record in candidate["files"].items()
        if name.endswith(".whl") or name.endswith(".tar.gz")
    }
    return candidate_distribution_identity(records)


def _parse_checksum_manifest(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    normalized_names: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        digest, separator, raw_name = line.partition("  ")
        name = raw_name.removeprefix("./")
        try:
            parse_portable_relative_path(name)
        except ValueError as exc:
            raise SpecValidationError(
                f"invalid checksum manifest record at {path.name}:{line_number}"
            ) from exc
        normalized = unicodedata.normalize("NFC", name).casefold()
        if (
            not separator
            or _SHA256_PATTERN.fullmatch(digest) is None
            or name in records
            or normalized in normalized_names
        ):
            raise SpecValidationError(
                f"invalid checksum manifest record at {path.name}:{line_number}"
            )
        records[name] = digest
        normalized_names.add(normalized)
    if not records:
        raise SpecValidationError(f"checksum manifest is empty: {path.name}")
    return records


def _write_complete_checksums(root: Path) -> None:
    assets = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    lines = [
        f"{sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in assets
    ]
    (root / RELEASE_CHECKSUMS_NAME).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _asset_records(
    paths: Iterable[Path],
    *,
    relative_to: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        _artifact_record(path, relative_to=relative_to)
        for path in sorted(
            paths,
            key=lambda item: (
                item.relative_to(relative_to).as_posix()
                if relative_to is not None
                else item.name
            ),
        )
    ]


def _artifact_record(
    path: Path,
    *,
    relative_to: Path | None = None,
) -> dict[str, Any]:
    return {
        "file": (
            path.relative_to(relative_to).as_posix()
            if relative_to is not None
            else path.name
        ),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _copy_new_file(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SpecValidationError(f"release asset name collision: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
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
