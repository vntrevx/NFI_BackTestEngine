"""Combine independently certified spot and futures evidence into one release."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from .archive_security import read_zip_member, validate_zip_archive
from .canonical import loads_json_bytes, read_json, write_json
from .certification_policy import validate_certification_semantics
from .errors import BenchmarkError, SpecValidationError
from .evidence_bundle import write_evidence_bundle
from .fixture import sha256_file
from .platform_benchmark import (
    REQUIRED_PLATFORM_SLUGS,
)
from .platform_benchmark import (
    REQUIRED_PLATFORM_SYSTEMS as PRODUCT_PLATFORM_SYSTEMS,
)
from .release_contract import (
    FUTURES_RELEASE_CONTRACT_ID,
    SPOT_RELEASE_CONTRACT_ID,
)
from .release_gate import (
    CANDIDATE_CHECKSUMS_NAME,
    RELEASE_CHECKSUMS_NAME,
    _artifact_record,
    _copy_new_file,
    _parse_checksum_manifest,
    _plain_directory,
    _plain_file,
    _validate_candidate_manifest,
)
from .release_provenance import (
    DEFAULT_PROVENANCE_POLICY,
    PLATFORM_EVIDENCE_VERSION,
    ProvenancePolicy,
    abort_certificate_publication,
    candidate_distribution_identity,
    mark_certificate_published,
    require_published_certificate,
    reserve_certificate_publication,
    verify_embedded_platform_evidence,
)
from .specs import (
    FULL_X7_CERTIFICATION_V2_SCHEMA,
    FULL_X7_COMBINED_RELEASE_SCHEMA,
    validate_combined_release_gate,
    validate_schema,
)

COMBINED_RELEASE_VERSION = "1.0.0"
COMBINED_RELEASE_GATE_VERSION = "1.0.0"
COMBINED_RELEASE_GATE_NAME = "release-gate.json"
COMBINED_RELEASE_REPORT_NAME = "full-x7-release.json"
COMBINED_RELEASE_BUNDLE_NAME = "full-x7-release-bundle.zip"
PUBLIC_RELEASE_ASSET_COUNT = 10
CURRENT_PUBLIC_RELEASE_ASSET_COUNT = 9
REQUIRED_MODE_CONTRACTS = frozenset({SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID})
REQUIRED_PLATFORM_SYSTEMS = frozenset({"windows", "linux", "darwin"})
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def combine_full_x7_release(
    *,
    spot_certificate_path: str | Path,
    futures_certificate_path: str | Path,
    platform_evidence_paths: list[str | Path],
    output_directory: str | Path,
    native_score_evidence_path: str | Path | None = None,
    native_score_identity_path: str | Path | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
) -> dict[str, Any]:
    """Bind two exact certificates and supported-platform evidence after score validation."""
    from .native_scorecard import (
        require_fresh_current_ref_for_authorization,
        require_native_scorecard_candidate_binding,
        require_native_scorecard_for_promotion,
    )

    require_native_scorecard_for_promotion(
        native_score_evidence_path,
        expected_identity_path=native_score_identity_path,
        provenance_policy=provenance_policy,
        authorization_operation="combined-release-combine",
    )
    output = Path(output_directory).resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BenchmarkError(f"combined release output must be empty: {output}")

    certificates = {
        SPOT_RELEASE_CONTRACT_ID: _load_certificate(
            Path(spot_certificate_path).resolve(),
            expected_mode=SPOT_RELEASE_CONTRACT_ID,
        ),
        FUTURES_RELEASE_CONTRACT_ID: _load_certificate(
            Path(futures_certificate_path).resolve(),
            expected_mode=FUTURES_RELEASE_CONTRACT_ID,
        ),
    }
    shared_identity = _shared_candidate_identity(certificates)
    mode_scopes = {
        mode: _certificate_mode_scope(item["report"])
        for mode, item in sorted(certificates.items())
    }
    platform_evidence = _load_platform_evidence(
        [Path(path).resolve() for path in platform_evidence_paths],
        certificates=certificates,
        shared_identity=shared_identity,
        provenance_policy=provenance_policy,
    )
    provenance_identities = {
        (
            item["document"]["provenance"]["candidate_id"],
            item["document"]["provenance"]["bundle_id"],
            item["document"]["provenance"]["challenge"],
        )
        for item in platform_evidence.values()
    }
    if platform_evidence and len(provenance_identities) != 1:
        raise SpecValidationError(
            "Spot and Futures evidence use different provenance challenges"
        )
    candidate_commits = {
        item["document"]["candidate_commit"] for item in platform_evidence.values()
    }
    candidate_ids = {
        item["document"]["provenance"]["candidate_id"]
        for item in platform_evidence.values()
    }
    if len(candidate_commits) != 1 or len(candidate_ids) != 1:
        raise SpecValidationError("combined release score candidate identity is unavailable")
    require_native_scorecard_candidate_binding(
        native_score_identity_path,
        expected_candidate_commit=next(iter(candidate_commits)),
        expected_candidate_identity=next(iter(candidate_ids)),
    )
    require_fresh_current_ref_for_authorization(
        native_score_evidence_path,
        native_score_identity_path,
        "combined-release-combine-output",
    )
    output.mkdir(parents=True, exist_ok=True)
    bundled_evidence = _materialize_release_evidence(
        output,
        certificates=certificates,
        platform_evidence=platform_evidence,
    )
    if native_score_evidence_path is None:  # narrowed after fail-closed validation above
        raise SpecValidationError("Native scorecard evidence is required")
    score_root = Path(native_score_evidence_path).resolve().parent
    score_destination = output / "evidence" / "native-score"
    shutil.copytree(score_root, score_destination)
    bundled_evidence.extend(
        sorted(path for path in score_destination.rglob("*") if path.is_file())
    )
    platform_modes = set(platform_evidence)
    platforms_met = platform_modes == REQUIRED_MODE_CONTRACTS
    gates = {
        "mode_certificates": {
            "met": True,
            "required": sorted(REQUIRED_MODE_CONTRACTS),
        },
        "shared_candidate": {
            "met": True,
            "identity_sha256": _document_sha256(shared_identity),
        },
        "platform_evidence": {
            "met": platforms_met,
            "required_modes": sorted(REQUIRED_MODE_CONTRACTS),
            "completed_modes": sorted(platform_modes),
            "required_systems": sorted(REQUIRED_PLATFORM_SYSTEMS),
            "required_slugs": sorted(REQUIRED_PLATFORM_SLUGS),
        },
    }
    release_certified = all(bool(gate["met"]) for gate in gates.values())
    report = {
        "schema_version": COMBINED_RELEASE_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "certified" if release_certified else "preview",
        "release_certified": release_certified,
        "shared_candidate": shared_identity,
        "mode_scopes": mode_scopes,
        "certificates": {mode: item["record"] for mode, item in sorted(certificates.items())},
        "platform_evidence": {
            mode: item["record"] for mode, item in sorted(platform_evidence.items())
        },
        "gates": gates,
    }
    report_path = output / "full-x7-release.json"
    validate_schema(report, FULL_X7_COMBINED_RELEASE_SCHEMA)
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        output,
        evidence_id=gates["shared_candidate"]["identity_sha256"],
        release_certified=release_certified,
        archive_name="full-x7-release-bundle.zip",
        include_paths=[report_path, *bundled_evidence],
    )
    result = {**report, "bundle": bundle}
    write_json(output / "full-x7-release-result.json", result)
    return result


def seal_combined_release_candidate(
    *,
    candidate_directory: str | Path,
    combined_release_result_path: str | Path,
    candidate_commit: str,
    output_directory: str | Path,
    created_at: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    provenance_ledger_path: str | Path | None = None,
    publication_attempt_id: str | None = None,
    native_score_evidence_path: str | Path | None = None,
    native_score_identity_path: str | Path | None = None,
) -> dict[str, Any]:
    """Seal one build-once candidate after both modes and all platform targets certify it."""
    from .native_scorecard import (
        require_fresh_current_ref_for_authorization,
        require_native_scorecard_candidate_binding,
        require_native_scorecard_for_promotion,
    )

    require_native_scorecard_for_promotion(
        native_score_evidence_path,
        expected_identity_path=native_score_identity_path,
        provenance_policy=provenance_policy,
        authorization_operation="combined-release-seal",
    )
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise SpecValidationError("candidate commit must be a 40-character lowercase Git SHA")
    if (
        provenance_ledger_path is None
        or publication_attempt_id is None
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", publication_attempt_id) is None
    ):
        raise SpecValidationError(
            "certified publication requires a durable ledger and publication attempt"
        )
    candidate_root = _plain_directory(candidate_directory, label="candidate")
    combined_result_file = _plain_file(
        combined_release_result_path,
        label="combined release result",
    )
    raw_output = Path(output_directory).absolute()
    if raw_output.is_symlink():
        raise SpecValidationError("combined release output must not be a symlink")
    output = raw_output.resolve()
    if output.exists() and not output.is_dir():
        raise BenchmarkError(f"combined release output must be a directory: {output}")

    candidate = _validate_candidate_manifest(candidate_root)
    require_native_scorecard_candidate_binding(
        native_score_identity_path,
        expected_candidate_commit=candidate_commit,
        expected_candidate_identity=_candidate_distribution_id(candidate),
    )
    result = read_json(combined_result_file)
    if not isinstance(result, dict):
        raise SpecValidationError("combined release result must be a JSON object")
    bundle = result.get("bundle")
    report = {key: value for key, value in result.items() if key != "bundle"}
    validate_schema(report, FULL_X7_COMBINED_RELEASE_SCHEMA)
    if (
        report.get("status") != "certified"
        or report.get("release_certified") is not True
        or set(report.get("certificates", {})) != REQUIRED_MODE_CONTRACTS
        or set(report.get("platform_evidence", {})) != REQUIRED_MODE_CONTRACTS
    ):
        raise SpecValidationError("combined release is preview, uncertified, or missing a mode")
    validated_bundle = _validate_evidence_bundle(
        bundle,
        root=combined_result_file.parent,
        expected_document=report,
        label="combined Full X7 release",
    )

    shared = report["shared_candidate"]
    distributions = _candidate_distributions(candidate, shared)
    platform_records = _candidate_platform_records(
        candidate,
        report=report,
        shared_identity=shared,
        provenance_policy=provenance_policy,
        expected_commit=candidate_commit,
    )

    report_source = combined_result_file.parent / COMBINED_RELEASE_REPORT_NAME
    if (
        not report_source.is_file()
        or report_source.is_symlink()
        or read_json(report_source) != report
    ):
        raise SpecValidationError("combined release bundle has no canonical public report")
    bundle_source = validated_bundle["_archive_path"]
    if bundle_source.name != COMBINED_RELEASE_BUNDLE_NAME:
        raise SpecValidationError("combined release archive has a noncanonical name")

    platform_document = read_json(
        candidate["root"] / next(iter(platform_records.values()))["candidate_file"]
    )
    bundle_id = platform_document["provenance"]["bundle_id"]
    if output.exists() and any(output.iterdir()):
        recovered = _verify_combined_release_assets(
            output,
            expected_commit=candidate_commit,
            provenance_policy=provenance_policy,
        )
        certificate_sha256 = sha256_file(output / COMBINED_RELEASE_GATE_NAME)
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "combined-release-recovery-reservation",
        )
        state = reserve_certificate_publication(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        if state == "published":
            raise SpecValidationError(
                "release provenance bundle challenge was already published"
            )
        return recovered
    if output.exists():
        output.rmdir()

    stage = output.parent / f".{output.name}.stage-{publication_attempt_id}"
    if stage.exists():
        shutil.rmtree(stage)
    require_fresh_current_ref_for_authorization(
        native_score_evidence_path,
        native_score_identity_path,
        "combined-release-seal-output",
    )
    stage.mkdir(mode=0o700)
    reserved = False
    published = False
    certificate_sha256 = ""
    try:
        copied_distributions: list[Path] = []
        for source in distributions:
            destination = stage / source.name
            _copy_new_file(source, destination)
            copied_distributions.append(destination)
        _write_distribution_checksums(stage, copied_distributions)
        report_destination = stage / COMBINED_RELEASE_REPORT_NAME
        bundle_destination = stage / COMBINED_RELEASE_BUNDLE_NAME
        _copy_new_file(report_source, report_destination)
        _copy_new_file(bundle_source, bundle_destination)

        gate = {
            "schema_version": COMBINED_RELEASE_GATE_VERSION,
            "created_at": created_at or report["created_at"],
            "release_kind": "combined-full-x7",
            "status": "release_certified",
            "release_certified": True,
            "candidate_commit": candidate_commit,
            "package_version": shared["package_version"],
            "candidate_manifest": _candidate_artifact_record(
                candidate_root / CANDIDATE_CHECKSUMS_NAME,
                relative_to=candidate_root,
            ),
            "combined_release": {
                "report": _artifact_record(report_destination),
                "bundle": _artifact_record(bundle_destination),
                "mode_contracts": sorted(REQUIRED_MODE_CONTRACTS),
                "strategy_sha256": shared["strategy_sha256"],
                "wheel_sha256": shared["wheel_sha256"],
                "native_extension_sha256": shared["native_extension_sha256"],
                "portable_package_sha256": shared["portable_package_sha256"],
            },
            "distributions": [
                _artifact_record(path)
                for path in sorted(copied_distributions, key=lambda item: item.name)
            ],
            "platform_evidence": platform_records,
            "gates": {
                "candidate_manifest": True,
                "candidate_commit": True,
                "distribution_set": True,
                "combined_certificates": True,
                "shared_candidate": True,
                "portable_package": True,
                "three_os_both_modes": True,
                "preview_rejected": True,
                "public_asset_set": True,
                "native_scorecard": True,
            },
        }
        validate_combined_release_gate(gate)
        write_json(stage / COMBINED_RELEASE_GATE_NAME, gate)
        _write_public_release_checksums(stage)
        _verify_combined_release_assets(
            stage,
            expected_commit=candidate_commit,
            provenance_policy=provenance_policy,
        )
        certificate_sha256 = sha256_file(stage / COMBINED_RELEASE_GATE_NAME)
        _publication_checkpoint("before-reservation")
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "combined-release-reservation",
        )
        state = reserve_certificate_publication(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
        if state == "published":
            raise SpecValidationError(
                "release provenance bundle challenge was already used"
            )
        reserved = True
        _publication_checkpoint("after-reservation")
        _publication_checkpoint("during-staged-publication")
        require_fresh_current_ref_for_authorization(
            native_score_evidence_path,
            native_score_identity_path,
            "combined-release-publication-output",
        )
        stage.rename(output)
        published = True
        _publication_checkpoint("after-publication-before-finalize")
        return gate
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        if reserved and not published:
            abort_certificate_publication(
                provenance_ledger_path,
                bundle_id=bundle_id,
                certificate_sha256=certificate_sha256,
                attempt_id=publication_attempt_id,
            )
        raise


class RemoteDraftBackend(Protocol):
    """Transactional remote release operations used by the publication coordinator."""

    def state(self) -> Literal["absent", "draft", "public"]: ...
    def create_draft(self) -> None: ...
    def upload_assets(self) -> None: ...
    def verify_assets(self, *, public: bool) -> None: ...
    def publish_draft(self) -> None: ...
    def delete_draft(self) -> None: ...


def publish_remote_draft_release(
    backend: RemoteDraftBackend,
    *,
    finalize: Callable[[], None],
    abort: Callable[[], None],
    checkpoint: Callable[[str], None] = lambda _name: None,
) -> None:
    """Publish through a private draft and finalize only after public byte verification."""
    try:
        state = backend.state()
        if state == "absent":
            backend.create_draft()
            checkpoint("after-create")
            state = "draft"
        if state == "draft":
            backend.upload_assets()
            checkpoint("after-upload")
            backend.verify_assets(public=False)
            checkpoint("after-draft-verify")
            backend.publish_draft()
            checkpoint("after-publish")
        if backend.state() != "public":
            raise SpecValidationError("remote release did not become public")
        backend.verify_assets(public=True)
        checkpoint("after-public-verify")
        checkpoint("before-finalize")
        finalize()
        checkpoint("after-finalize")
    except BaseException:
        if backend.state() != "public":
            if backend.state() == "draft":
                backend.delete_draft()
            abort()
        raise


def finalize_combined_release_publication(
    source: str | Path,
    *,
    provenance_ledger_path: str | Path,
    publication_attempt_id: str,
    expected_commit: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    native_score_evidence_path: str | Path | None = None,
    native_score_identity_path: str | Path | None = None,
) -> dict[str, Any]:
    """Finalize a remotely public, byte-verified combined release."""
    from .native_scorecard import require_fresh_current_ref_for_authorization

    gate = _verify_combined_release_assets(
        source,
        expected_commit=expected_commit,
        provenance_policy=provenance_policy,
    )
    root = Path(source)
    bundle_id = _public_bundle_id(root / COMBINED_RELEASE_BUNDLE_NAME)
    certificate_sha256 = sha256_file(root / COMBINED_RELEASE_GATE_NAME)
    require_fresh_current_ref_for_authorization(
        native_score_evidence_path,
        native_score_identity_path,
        "combined-release-finalize-publication",
    )
    state = reserve_certificate_publication(
        provenance_ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
        attempt_id=publication_attempt_id,
    )
    if state == "reserved":
        mark_certificate_published(
            provenance_ledger_path,
            bundle_id=bundle_id,
            certificate_sha256=certificate_sha256,
            attempt_id=publication_attempt_id,
        )
    require_published_certificate(
        provenance_ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
    )
    return gate


def abort_combined_release_publication(
    source: str | Path,
    *,
    provenance_ledger_path: str | Path,
    publication_attempt_id: str,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
) -> None:
    """Abort an unpublished remote attempt without releasing its identity."""
    _verify_combined_release_assets(source, provenance_policy=provenance_policy)
    root = Path(source)
    abort_certificate_publication(
        provenance_ledger_path,
        bundle_id=_public_bundle_id(root / COMBINED_RELEASE_BUNDLE_NAME),
        certificate_sha256=sha256_file(root / COMBINED_RELEASE_GATE_NAME),
        attempt_id=publication_attempt_id,
    )


def verify_combined_release_candidate(
    source: str | Path,
    *,
    expected_commit: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    provenance_ledger_path: str | Path | None = None,
    native_score_evidence_path: str | Path | None = None,
    native_score_identity_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify public assets and their exact durable published score claim."""
    from .native_scorecard import (
        require_fresh_current_ref_for_authorization,
        require_native_scorecard_candidate_binding,
        require_native_scorecard_for_promotion,
    )

    if expected_commit is None:
        raise SpecValidationError("combined release candidate commit is required")
    require_native_scorecard_for_promotion(
        native_score_evidence_path,
        expected_identity_path=native_score_identity_path,
        provenance_policy=provenance_policy,
        authorization_operation="combined-release-verify",
    )
    release_root = _plain_directory(source, label="combined release")
    distribution_records = _parse_checksum_manifest(
        release_root / CANDIDATE_CHECKSUMS_NAME
    )
    candidate_identity = candidate_distribution_identity(
        {
            name: digest
            for name, digest in distribution_records.items()
            if name.endswith(".whl") or name.endswith(".tar.gz")
        }
    )
    require_native_scorecard_candidate_binding(
        native_score_identity_path,
        expected_candidate_commit=expected_commit,
        expected_candidate_identity=candidate_identity,
    )
    gate = _verify_combined_release_assets(
        source,
        expected_commit=expected_commit,
        provenance_policy=provenance_policy,
    )
    if provenance_ledger_path is None:
        raise SpecValidationError(
            "combined release certification requires a durable published claim"
        )
    bundle_id = _public_bundle_id(Path(source) / COMBINED_RELEASE_BUNDLE_NAME)
    require_fresh_current_ref_for_authorization(
        native_score_evidence_path,
        native_score_identity_path,
        "combined-release-verify-output",
    )
    require_published_certificate(
        provenance_ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=sha256_file(Path(source) / COMBINED_RELEASE_GATE_NAME),
    )
    return gate


def _verify_combined_release_assets(
    source: str | Path,
    *,
    expected_commit: str | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
) -> dict[str, Any]:
    """Verify a current nine-file or historical ten-file combined asset set."""

    root = _plain_directory(source, label="combined release")
    entries = list(root.iterdir())
    allowed_asset_counts = {CURRENT_PUBLIC_RELEASE_ASSET_COUNT, PUBLIC_RELEASE_ASSET_COUNT}
    if len(entries) not in allowed_asset_counts or any(
        path.is_symlink() or not path.is_file() for path in entries
    ):
        raise SpecValidationError(
            "combined release has an unsupported regular-file count"
        )

    release_manifest = root / RELEASE_CHECKSUMS_NAME
    if not release_manifest.is_file() or release_manifest.is_symlink():
        raise SpecValidationError("combined release has no complete public checksum manifest")
    release_records = _parse_checksum_manifest(release_manifest)
    expected_public_names = {path.name for path in entries if path != release_manifest}
    if set(release_records) != expected_public_names:
        raise SpecValidationError(
            "combined release checksum manifest does not cover every public asset"
        )
    _verify_checksum_records(root, release_records, label="public release")

    gate = read_json(root / COMBINED_RELEASE_GATE_NAME)
    validate_combined_release_gate(gate)
    if not isinstance(gate, dict):
        raise SpecValidationError("combined release gate must be a JSON object")
    if expected_commit is not None and gate.get("candidate_commit") != expected_commit:
        raise SpecValidationError("combined release candidate commit differs")

    distribution_manifest = root / CANDIDATE_CHECKSUMS_NAME
    distribution_records = _parse_checksum_manifest(distribution_manifest)
    gate_distributions = gate["distributions"]
    distribution_names = {
        record["file"]
        for record in gate_distributions
        if isinstance(record, dict) and isinstance(record.get("file"), str)
    }
    if (
        len(distribution_records) not in {4, 5}
        or len(entries) != len(distribution_records) + 5
        or set(distribution_records) != distribution_names
    ):
        raise SpecValidationError("distribution checksum manifest differs from the release gate")
    _verify_checksum_records(
        root,
        distribution_records,
        label="release distribution",
    )
    for record in gate_distributions:
        path = root / record["file"]
        if _artifact_record(path) != record:
            raise SpecValidationError(f"release distribution record differs: {record['file']}")

    report_path = root / COMBINED_RELEASE_REPORT_NAME
    report = read_json(report_path)
    validate_schema(report, FULL_X7_COMBINED_RELEASE_SCHEMA)
    if (
        not isinstance(report, dict)
        or report.get("status") != "certified"
        or report.get("release_certified") is not True
    ):
        raise SpecValidationError("combined public report is preview or uncertified")
    if _artifact_record(report_path) != gate["combined_release"]["report"]:
        raise SpecValidationError("combined public report differs from release gate")
    bundle_path = root / COMBINED_RELEASE_BUNDLE_NAME
    if _artifact_record(bundle_path) != gate["combined_release"]["bundle"]:
        raise SpecValidationError("combined public bundle differs from release gate")
    shared = report["shared_candidate"]
    if not isinstance(shared, dict) or any(
        not _is_sha256(shared.get(key))
        for key in (
            "strategy_sha256",
            "wheel_sha256",
            "native_extension_sha256",
            "portable_package_sha256",
        )
    ):
        raise SpecValidationError(
            "combined public report has no complete candidate identity"
        )
    if any(
        gate["combined_release"][key] != shared[key]
        for key in (
            "strategy_sha256",
            "wheel_sha256",
            "native_extension_sha256",
            "portable_package_sha256",
        )
    ):
        raise SpecValidationError("combined public report identity differs from release gate")
    if gate["package_version"] != shared["package_version"]:
        raise SpecValidationError("combined public report version differs from release gate")
    _candidate_distributions(
        {
            "root": root,
            "files": {
                record["file"]: record
                for record in gate_distributions
            },
        },
        shared,
    )
    _verify_public_combined_bundle(
        bundle_path,
        expected_report=report,
        provenance_policy=provenance_policy,
        expected_commit=gate["candidate_commit"],
        expected_candidate_id=candidate_distribution_identity(
            {record["file"]: record["sha256"] for record in gate["distributions"]}
        ),
    )
    return gate


def _candidate_distributions(
    candidate: dict[str, Any],
    shared_identity: dict[str, Any],
) -> list[Path]:
    root = candidate["root"]
    files = candidate["files"]
    wheel_names = sorted(
        name
        for name in files
        if PurePosixPath(name).parent == PurePosixPath(".") and name.endswith(".whl")
    )
    sdist_names = sorted(
        name
        for name in files
        if PurePosixPath(name).parent == PurePosixPath(".") and name.endswith(".tar.gz")
    )
    all_distribution_names = {
        name for name in files if name.endswith(".whl") or name.endswith(".tar.gz")
    }
    if (
        len(wheel_names) not in {3, 4}
        or len(sdist_names) != 1
        or all_distribution_names != {*wheel_names, *sdist_names}
    ):
        raise SpecValidationError(
            "candidate must contain three current or four historical wheels and one sdist"
        )
    version = shared_identity["package_version"]
    wheel_prefix = f"nfi_backtest_engine-{version}-"
    if any(not name.startswith(wheel_prefix) for name in wheel_names):
        raise SpecValidationError(
            "candidate wheel package version differs from combined certification"
        )
    expected_sdist = f"nfi_backtest_engine-{version}.tar.gz"
    if sdist_names != [expected_sdist]:
        raise SpecValidationError(
            "candidate sdist package version differs from combined certification"
        )
    matching_linux_wheels = [
        name for name in wheel_names if files[name]["sha256"] == shared_identity["wheel_sha256"]
    ]
    if len(matching_linux_wheels) != 1:
        raise SpecValidationError(
            "combined Linux wheel SHA does not identify exactly one candidate wheel"
        )
    if len({files[name]["sha256"] for name in wheel_names}) != len(wheel_names):
        raise SpecValidationError("candidate wheels must have distinct content hashes")
    return [root / name for name in [*wheel_names, *sdist_names]]


def _candidate_platform_records(
    candidate: dict[str, Any],
    *,
    report: dict[str, Any],
    shared_identity: dict[str, Any],
    provenance_policy: ProvenancePolicy,
    expected_commit: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    platform_wheels_by_mode: dict[str, dict[str, str]] = {}
    candidate_files = candidate["files"]
    for mode in sorted(REQUIRED_MODE_CONTRACTS):
        expected = report["platform_evidence"][mode]
        matches = [
            name
            for name, record in candidate_files.items()
            if PurePosixPath(name).name == "platform-evidence.json"
            and record["sha256"] == expected["sha256"]
        ]
        if len(matches) != 1:
            raise SpecValidationError(
                f"combined {mode} platform evidence does not identify exactly one candidate file"
            )
        relative = matches[0]
        path = candidate["root"] / relative
        if path.stat().st_size != expected["bytes"] or sha256_file(path) != expected["sha256"]:
            raise SpecValidationError(f"combined {mode} platform evidence differs from candidate")
        loaded = _load_platform_evidence(
            [path],
            certificates={mode: _synthetic_certificate_identity(shared_identity)},
            shared_identity=shared_identity,
            provenance_policy=provenance_policy,
            expected_commit=expected_commit,
            expected_candidate_id=_candidate_distribution_id(candidate),
        )
        if set(loaded) != {mode}:
            raise SpecValidationError(f"candidate platform evidence mode differs for {mode}")
        document = loaded[mode]["document"]
        platform_wheels_by_mode[mode] = {
            str(item.get("slug") or item["system"]): item["wheel_sha256"]
            for item in document["platforms"]
        }
        result[mode] = {
            "candidate_file": relative,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if len({record["candidate_file"] for record in result.values()}) != 2:
        raise SpecValidationError(
            "Spot and Futures platform evidence must be distinct candidate files"
        )
    platform_wheel_maps = list(platform_wheels_by_mode.values())
    if any(mapping != platform_wheel_maps[0] for mapping in platform_wheel_maps[1:]):
        raise SpecValidationError(
            "Spot and Futures platform evidence use different platform wheels"
        )
    candidate_wheel_hashes = {
        record["sha256"] for name, record in candidate_files.items() if name.endswith(".whl")
    }
    evidenced_wheel_hashes = set(platform_wheel_maps[0].values())
    uses_slug_contract = set(platform_wheel_maps[0]) == REQUIRED_PLATFORM_SLUGS
    valid_wheels = (
        (
            len(evidenced_wheel_hashes) == 3
            and evidenced_wheel_hashes == candidate_wheel_hashes
        )
        if uses_slug_contract
        else (
            len(evidenced_wheel_hashes) == len(REQUIRED_PLATFORM_SYSTEMS)
            and evidenced_wheel_hashes.issubset(candidate_wheel_hashes)
            and len(candidate_wheel_hashes - evidenced_wheel_hashes) == 1
        )
    )
    if not valid_wheels:
        raise SpecValidationError(
            "platform evidence does not bind the exact candidate wheel set"
        )
    return result


def _candidate_distribution_id(candidate: dict[str, Any]) -> str:
    return candidate_distribution_identity(
        {
            name: str(record["sha256"])
            for name, record in candidate["files"].items()
            if name.endswith(".whl") or name.endswith(".tar.gz")
        }
    )


def _synthetic_certificate_identity(
    shared_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "report": {
            "gates": {
                "installed_wheel": {
                    "sha256": shared_identity["wheel_sha256"],
                }
            }
        }
    }


def _write_distribution_checksums(root: Path, distributions: list[Path]) -> None:
    lines = [
        f"{sha256_file(path)}  {path.name}"
        for path in sorted(distributions, key=lambda item: item.name)
    ]
    (root / CANDIDATE_CHECKSUMS_NAME).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _candidate_artifact_record(
    path: Path,
    *,
    relative_to: Path,
) -> dict[str, Any]:
    record = _artifact_record(path, relative_to=relative_to)
    return {
        "candidate_file": record["file"],
        "bytes": record["bytes"],
        "sha256": record["sha256"],
    }


def _write_public_release_checksums(root: Path) -> None:
    paths = sorted(
        (path for path in root.iterdir() if path.is_file() and path.name != RELEASE_CHECKSUMS_NAME),
        key=lambda item: item.name,
    )
    if len(paths) not in {
        CURRENT_PUBLIC_RELEASE_ASSET_COUNT - 1,
        PUBLIC_RELEASE_ASSET_COUNT - 1,
    }:
        raise SpecValidationError("combined release does not have the exact public asset set")
    (root / RELEASE_CHECKSUMS_NAME).write_text(
        "\n".join(f"{sha256_file(path)}  {path.name}" for path in paths) + "\n",
        encoding="utf-8",
    )


def _verify_checksum_records(
    root: Path,
    records: dict[str, str],
    *,
    label: str,
) -> None:
    for name, expected in records.items():
        path = root / name
        if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
            raise SpecValidationError(f"{label} checksum failed: {name}")


def _publication_checkpoint(_name: str) -> None:
    """Deterministic fault boundary used by publication recovery tests."""


def _public_bundle_id(bundle_path: Path) -> str:
    with zipfile.ZipFile(bundle_path) as archive:
        members = validate_zip_archive(archive)
        bundle_ids = {
            document["provenance"]["bundle_id"]
            for name, info in members.items()
            if name.endswith("platform-evidence.json")
            for document in [loads_json_bytes(read_zip_member(archive, info))]
            if isinstance(document, dict)
            and isinstance(document.get("provenance"), dict)
            and _is_sha256(document["provenance"].get("bundle_id"))
        }
    if len(bundle_ids) != 1:
        raise SpecValidationError("combined public bundle identity is incomplete")
    return next(iter(bundle_ids))


def _verify_public_combined_bundle(
    bundle_path: Path,
    *,
    expected_report: dict[str, Any],
    provenance_policy: ProvenancePolicy,
    expected_commit: str,
    expected_candidate_id: str,
) -> None:
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            members = validate_zip_archive(archive)
            names = list(members)
            if "bundle-manifest.json" not in names:
                raise SpecValidationError("combined public bundle member set is invalid")
            manifest = loads_json_bytes(
                read_zip_member(archive, members["bundle-manifest.json"])
            )
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != "1.0.0"
                or not isinstance(files, list)
                or not files
            ):
                raise SpecValidationError("combined public bundle manifest is invalid")
            records: dict[str, dict[str, Any]] = {}
            report_found = False
            for record in files:
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("path"), str)
                    or not isinstance(record.get("bytes"), int)
                    or not _is_sha256(record.get("sha256"))
                ):
                    raise SpecValidationError("combined public bundle manifest member is invalid")
                name = record["path"]
                relative = PurePosixPath(name)
                if (
                    "\\" in name
                    or relative.is_absolute()
                    or relative.as_posix() != name
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or name in records
                ):
                    raise SpecValidationError("combined public bundle manifest member is invalid")
                records[name] = record
                info = members.get(name)
                if info is None:
                    raise SpecValidationError("combined public bundle member is missing")
                content = read_zip_member(archive, info)
                if (
                    len(content) != record["bytes"]
                    or hashlib.sha256(content).hexdigest() != record["sha256"]
                ):
                    raise SpecValidationError("combined public bundle member checksum failed")
                if name == COMBINED_RELEASE_REPORT_NAME:
                    report_found = loads_json_bytes(content) == expected_report
                if name.endswith("platform-evidence.json"):
                    platform_document = loads_json_bytes(content)
                    if not isinstance(platform_document, dict):
                        raise SpecValidationError(
                            "combined public bundle platform evidence is malformed"
                        )
                    validate_certification_semantics(
                        platform_document, label="combined platform evidence"
                    )
                    platforms = platform_document.get("platforms")
                    slugs = (
                        {
                            item.get("slug")
                            for item in platforms
                            if isinstance(item, dict)
                        }
                        if isinstance(platforms, list)
                        else set()
                    )
                    verify_embedded_platform_evidence(
                        platform_document,
                        policy=provenance_policy,
                        expected_commit=expected_commit,
                        expected_candidate_id=expected_candidate_id,
                        required_platform_systems=(
                            PRODUCT_PLATFORM_SYSTEMS
                            if relative.parts[:2] == ("evidence", "native-score")
                            else REQUIRED_PLATFORM_SYSTEMS
                        ),
                        required_platform_slugs=(
                            REQUIRED_PLATFORM_SLUGS
                            if slugs == REQUIRED_PLATFORM_SLUGS
                            else None
                        ),
                    )
            if set(names) != {*records, "bundle-manifest.json"}:
                raise SpecValidationError("combined public bundle members differ from its manifest")
            if not report_found:
                raise SpecValidationError(
                    "combined public bundle does not contain its public report"
                )
    except (
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        raise SpecValidationError("combined public bundle is invalid") from exc


def _load_certificate(path: Path, *, expected_mode: str) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict):
        raise SpecValidationError(f"Full X7 certificate must be an object: {path}")
    bundle = document.get("bundle")
    report = {key: value for key, value in document.items() if key != "bundle"}
    validate_schema(report, FULL_X7_CERTIFICATION_V2_SCHEMA)
    validate_certification_semantics(report, label="Full X7 certificate")
    gates = report.get("gates")
    recomputed_certified = bool(
        isinstance(gates, dict)
        and gates
        and all(isinstance(gate, dict) and gate.get("met") is True for gate in gates.values())
    )
    if (
        report.get("status") != "certified"
        or report.get("release_certified") is not True
        or not recomputed_certified
        or report.get("claim_scope", {}).get("mode_contract") != expected_mode
    ):
        raise SpecValidationError(f"Full X7 certificate is not certified for {expected_mode}")
    installed_wheel = report.get("gates", {}).get("installed_wheel")
    if (
        not isinstance(installed_wheel, dict)
        or installed_wheel.get("met") is not True
        or not _is_sha256(installed_wheel.get("sha256"))
        or not _is_sha256(installed_wheel.get("native_member_sha256"))
        or not _is_sha256(installed_wheel.get("portable_package_sha256"))
    ):
        raise SpecValidationError(
            f"Full X7 certificate has no portable candidate identity for {expected_mode}"
        )
    if not isinstance(bundle, dict):
        sibling = path.parent / "bundle.json"
        bundle = read_json(sibling) if sibling.is_file() else None
    validated_bundle = _validate_evidence_bundle(
        bundle,
        root=path.parent,
        expected_document=report,
        label="Full X7 certificate",
    )
    return {
        "report": report,
        "bundle": validated_bundle,
        "record": {
            "file": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "bundle_sha256": validated_bundle["archive"]["sha256"],
        },
    }


def _validate_evidence_bundle(
    bundle: Any,
    *,
    root: Path,
    expected_document: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Verify a bundle, every manifest member, and its bound JSON document."""

    if not isinstance(bundle, dict) or bundle.get("release_certified") is not True:
        raise SpecValidationError(f"{label} bundle is not release-certified")
    resolved: dict[str, Path] = {}
    for key in ("archive", "manifest"):
        record = bundle.get(key)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
        ):
            raise SpecValidationError(f"{label} bundle {key} is invalid")
        name = record["path"]
        relative = PurePosixPath(name)
        artifact = root / name
        if (
            "\\" in name
            or relative.is_absolute()
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
            or _traverses_symlink(artifact, root)
            or not artifact.is_file()
            or artifact.stat().st_size != record["bytes"]
            or sha256_file(artifact) != record["sha256"]
        ):
            raise SpecValidationError(f"{label} bundle {key} failed hash validation")
        resolved[key] = artifact

    manifest = read_json(resolved["manifest"])
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "1.0.0"
        or manifest.get("evidence_id") != bundle.get("evidence_id")
        or not isinstance(files, list)
        or not files
    ):
        raise SpecValidationError(f"{label} bundle manifest is invalid")
    expected_found = False
    member_records: dict[str, dict[str, Any]] = {}
    for record in files:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
        ):
            raise SpecValidationError(f"{label} bundle manifest member is invalid")
        name = record["path"]
        relative = PurePosixPath(name)
        if (
            "\\" in name
            or relative.is_absolute()
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
            or name in member_records
            or not _is_sha256(record["sha256"])
        ):
            raise SpecValidationError(f"{label} bundle manifest member is invalid")
        member_records[name] = record

    manifest_name = resolved["manifest"].relative_to(root).as_posix()
    expected_archive_names = {*member_records, manifest_name}
    try:
        with zipfile.ZipFile(resolved["archive"]) as archive:
            members = validate_zip_archive(archive)
            if set(members) != expected_archive_names:
                raise SpecValidationError(f"{label} bundle archive members differ")
            archived_manifest = read_zip_member(archive, members[manifest_name])
            if len(archived_manifest) != resolved["manifest"].stat().st_size or hashlib.sha256(
                archived_manifest
            ).hexdigest() != sha256_file(resolved["manifest"]):
                raise SpecValidationError(f"{label} bundle archive manifest differs")
            for name, record in member_records.items():
                archived = read_zip_member(archive, members[name])
                if (
                    len(archived) != record["bytes"]
                    or hashlib.sha256(archived).hexdigest() != record["sha256"]
                ):
                    raise SpecValidationError(f"{label} bundle member failed hash validation")
                with suppress(UnicodeDecodeError, json.JSONDecodeError):
                    expected_found |= loads_json_bytes(archived) == expected_document
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise SpecValidationError(f"{label} bundle archive is invalid") from exc
    if not expected_found:
        raise SpecValidationError(f"{label} bundle does not contain its report")
    return {
        **bundle,
        "_archive_path": resolved["archive"],
        "_manifest_path": resolved["manifest"],
    }


def _traverses_symlink(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def _shared_candidate_identity(
    certificates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    identities = {
        mode: _certificate_candidate_identity(item["report"]) for mode, item in certificates.items()
    }
    values = list(identities.values())
    if any(identity != values[0] for identity in values[1:]):
        raise SpecValidationError(
            "spot and futures certificates use different strategy, wheel, "
            "reference, or candidate identities"
        )
    return values[0]


def _certificate_candidate_identity(report: dict[str, Any]) -> dict[str, Any]:
    claim = report["claim_scope"]
    inputs = report["inputs"]
    environment = report["environment"]
    installed_wheel = report["gates"]["installed_wheel"]
    engine_build = environment["engine_build"]
    return {
        "strategy": claim["strategy"],
        "upstream_commit": claim["upstream_commit"],
        "strategy_sha256": inputs["strategy_sha256"],
        "package_version": environment["package_version"],
        "wheel_sha256": installed_wheel["sha256"],
        "native_extension_sha256": installed_wheel["native_member_sha256"],
        "portable_package_sha256": installed_wheel["portable_package_sha256"],
        "engine_source_fingerprint": engine_build["source_fingerprint"],
        "reference": inputs["reference"],
    }


def _certificate_mode_scope(report: dict[str, Any]) -> dict[str, Any]:
    claim = report["claim_scope"]
    inputs = report["inputs"]
    release_lock = inputs["release_lock"]
    return {
        "mode_contract": claim["mode_contract"],
        "trading_mode": claim["trading_mode"],
        "margin_mode": claim["margin_mode"],
        "exchange": claim["exchange"],
        "settlement_currency": claim["settlement_currency"],
        "required_data_roles": claim["required_data_roles"],
        "timerange": claim["timerange"],
        "pair_count": claim["pair_count"],
        "timeframes": claim["timeframes"],
        "continuous_timerange": claim["continuous_timerange"],
        "history_coverage_policy": claim["history_coverage_policy"],
        "release_lock_sha256": release_lock["sha256"],
        "release_lock_identity_sha256": release_lock["identity_sha256"],
        "config_sha256": inputs["config_sha256"],
        "data_aggregate_sha256": inputs["data_aggregate_sha256"],
        "engine_market_snapshot_sha256": inputs["engine_market_snapshot_sha256"],
        "reference_market_snapshot_sha256": inputs[
            "reference_market_snapshot_sha256"
        ],
    }


def _load_platform_evidence(
    paths: list[Path],
    *,
    certificates: dict[str, dict[str, Any]],
    shared_identity: dict[str, Any],
    provenance_policy: ProvenancePolicy,
    expected_commit: str | None = None,
    expected_candidate_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        document = read_json(path)
        if not isinstance(document, dict):
            raise SpecValidationError(f"platform evidence must be an object: {path}")
        validate_certification_semantics(document, label="platform evidence")
        mode = document.get("mode_contract")
        if mode not in REQUIRED_MODE_CONTRACTS:
            raise SpecValidationError("platform evidence has an unsupported mode")
        if mode in result:
            raise SpecValidationError(f"duplicate platform evidence for {mode}")
        platforms = document.get("platforms")
        workload = document.get("workload")
        if not isinstance(platforms, list):
            raise SpecValidationError(f"platform evidence is incomplete for {mode}")
        systems = {item.get("system") for item in platforms if isinstance(item, dict)}
        slugs = {item.get("slug") for item in platforms if isinstance(item, dict)}
        uses_slug_contract = slugs == REQUIRED_PLATFORM_SLUGS
        uses_legacy_contract = systems == REQUIRED_PLATFORM_SYSTEMS and len(platforms) == 3
        verified = verify_embedded_platform_evidence(
            document,
            policy=provenance_policy,
            expected_commit=expected_commit,
            expected_candidate_id=expected_candidate_id,
            required_platform_slugs=(
                REQUIRED_PLATFORM_SLUGS if uses_slug_contract else None
            ),
        )
        if (
            document.get("schema_version") != PLATFORM_EVIDENCE_VERSION
            or document.get("release_certified") is not True
            or document.get("candidate_commit") != verified["commit"]
            or not (uses_slug_contract or uses_legacy_contract)
            or not isinstance(workload, dict)
            or workload.get("mode_contract") != mode
            or workload.get(
                "base_strategy_sha256",
                workload.get("strategy_sha256"),
            )
            != shared_identity["strategy_sha256"]
            or document.get("package_version") != shared_identity["package_version"]
            or document.get("portable_package_sha256") != shared_identity["portable_package_sha256"]
        ):
            raise SpecValidationError(f"platform evidence is incomplete for {mode}")
        linux = next(
            (
                item
                for item in platforms
                if isinstance(item, dict)
                and (
                    item.get("slug") == "linux-x86_64"
                    or (
                        item.get("slug") is None
                        and item.get("system") == "linux"
                    )
                )
            ),
            None,
        )
        certificate_wheel = certificates[mode]["report"]["gates"]["installed_wheel"]["sha256"]
        if (
            not isinstance(linux, dict)
            or linux.get("wheel_sha256") != certificate_wheel
            or linux.get("native_extension_sha256")
            != shared_identity["native_extension_sha256"]
        ):
            raise SpecValidationError(
                f"Linux platform wheel or extension differs from the {mode} certificate"
            )
        bundle_path = path.parent / "bundle.json"
        bundle = read_json(bundle_path) if bundle_path.is_file() else None
        validated_bundle = _validate_evidence_bundle(
            bundle,
            root=path.parent,
            expected_document=document,
            label=f"{mode} platform evidence",
        )
        identity_field = "slug" if uses_slug_contract else "system"
        report_by_identity = {
            report["platform"].get(identity_field): report
            for report in verified["reports"]
        }
        for item in platforms:
            signed_report = report_by_identity.get(item.get(identity_field))
            if signed_report is None or any(
                item.get(key) != signed_report["package"].get(key)
                for key in ("wheel_sha256", "native_extension_sha256")
            ):
                raise SpecValidationError(
                    f"platform evidence projection differs from signed reports for {mode}"
                )
        result[mode] = {
            "document": document,
            "bundle": validated_bundle,
            "record": {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            },
        }
    return result


def _materialize_release_evidence(
    output: Path,
    *,
    certificates: dict[str, dict[str, Any]],
    platform_evidence: dict[str, dict[str, Any]],
) -> list[Path]:
    """Copy every validated input into the portable combined release bundle."""

    included: list[Path] = []
    for mode, certificate in sorted(certificates.items()):
        destination = output / "evidence" / mode
        report_path = destination / "certificate.json"
        write_json(report_path, certificate["report"])
        included.append(report_path)
        included.extend(
            _copy_bundle_files(
                certificate["bundle"],
                destination,
                prefix="certificate",
            )
        )
        certificate["record"] = {
            "file": report_path.relative_to(output).as_posix(),
            "bytes": report_path.stat().st_size,
            "sha256": sha256_file(report_path),
            "bundle_sha256": certificate["bundle"]["archive"]["sha256"],
        }
    for mode, evidence in sorted(platform_evidence.items()):
        destination = output / "evidence" / mode
        evidence_path = destination / "platform-evidence.json"
        write_json(evidence_path, evidence["document"])
        included.append(evidence_path)
        included.extend(
            _copy_bundle_files(
                evidence["bundle"],
                destination,
                prefix="platform",
            )
        )
        evidence["record"] = {
            "file": evidence_path.relative_to(output).as_posix(),
            "bytes": evidence_path.stat().st_size,
            "sha256": sha256_file(evidence_path),
        }
    return included


def _copy_bundle_files(
    bundle: dict[str, Any],
    destination: Path,
    *,
    prefix: str,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    copied = [
        destination / f"{prefix}-bundle.zip",
        destination / f"{prefix}-bundle-manifest.json",
    ]
    shutil.copyfile(bundle["_archive_path"], copied[0])
    shutil.copyfile(bundle["_manifest_path"], copied[1])
    return copied


def _document_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
