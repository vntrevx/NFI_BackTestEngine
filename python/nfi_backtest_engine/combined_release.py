"""Combine independently certified spot and futures evidence into one release."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .evidence_bundle import write_evidence_bundle
from .fixture import sha256_file
from .release_contract import (
    FUTURES_RELEASE_CONTRACT_ID,
    SPOT_RELEASE_CONTRACT_ID,
)
from .specs import (
    FULL_X7_CERTIFICATION_V2_SCHEMA,
    FULL_X7_COMBINED_RELEASE_SCHEMA,
    validate_schema,
)

COMBINED_RELEASE_VERSION = "1.0.0"
REQUIRED_MODE_CONTRACTS = frozenset(
    {SPOT_RELEASE_CONTRACT_ID, FUTURES_RELEASE_CONTRACT_ID}
)
REQUIRED_PLATFORM_SYSTEMS = frozenset({"windows", "linux", "darwin"})


def combine_full_x7_release(
    *,
    spot_certificate_path: str | Path,
    futures_certificate_path: str | Path,
    platform_evidence_paths: list[str | Path],
    output_directory: str | Path,
) -> dict[str, Any]:
    """Bind two exact certificates and optional three-OS evidence without reruns."""
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"combined release output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

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
    platform_evidence = _load_platform_evidence(
        [Path(path).resolve() for path in platform_evidence_paths],
        certificates=certificates,
        shared_identity=shared_identity,
    )
    bundled_evidence = _materialize_release_evidence(
        output,
        certificates=certificates,
        platform_evidence=platform_evidence,
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
        },
    }
    release_certified = all(bool(gate["met"]) for gate in gates.values())
    report = {
        "schema_version": COMBINED_RELEASE_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "certified" if release_certified else "preview",
        "release_certified": release_certified,
        "shared_candidate": shared_identity,
        "certificates": {
            mode: item["record"]
            for mode, item in sorted(certificates.items())
        },
        "platform_evidence": {
            mode: item["record"]
            for mode, item in sorted(platform_evidence.items())
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


def _load_certificate(path: Path, *, expected_mode: str) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict):
        raise SpecValidationError(f"Full X7 certificate must be an object: {path}")
    bundle = document.get("bundle")
    report = {key: value for key, value in document.items() if key != "bundle"}
    validate_schema(report, FULL_X7_CERTIFICATION_V2_SCHEMA)
    if (
        report.get("release_certified") is not True
        or report.get("status") != "certified"
        or report.get("claim_scope", {}).get("mode_contract") != expected_mode
    ):
        raise SpecValidationError(
            f"Full X7 certificate is not certified for {expected_mode}"
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
        artifact = (root / record["path"]).resolve()
        if (
            not artifact.is_relative_to(root)
            or not artifact.is_file()
            or artifact.stat().st_size != record["bytes"]
            or sha256_file(artifact) != record["sha256"]
        ):
            raise SpecValidationError(
                f"{label} bundle {key} failed hash validation"
            )
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
    member_paths: list[Path] = []
    for record in files:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
        ):
            raise SpecValidationError(f"{label} bundle manifest member is invalid")
        member = (root / record["path"]).resolve()
        if (
            not member.is_relative_to(root)
            or not member.is_file()
            or member.stat().st_size != record["bytes"]
            or sha256_file(member) != record["sha256"]
        ):
            raise SpecValidationError(f"{label} bundle member failed hash validation")
        member_paths.append(member)
        with suppress(OSError, UnicodeDecodeError, json.JSONDecodeError):
            expected_found |= read_json(member) == expected_document
    if not expected_found:
        raise SpecValidationError(f"{label} bundle does not contain its report")

    expected_archive_names = {
        path.relative_to(root).as_posix() for path in member_paths
    } | {resolved["manifest"].relative_to(root).as_posix()}
    with zipfile.ZipFile(resolved["archive"]) as archive:
        if set(archive.namelist()) != expected_archive_names:
            raise SpecValidationError(f"{label} bundle archive members differ")
        for path in [*member_paths, resolved["manifest"]]:
            archived = archive.read(path.relative_to(root).as_posix())
            if (
                len(archived) != path.stat().st_size
                or hashlib.sha256(archived).hexdigest() != sha256_file(path)
            ):
                raise SpecValidationError(f"{label} bundle archive content differs")
    return {
        **bundle,
        "_archive_path": resolved["archive"],
        "_manifest_path": resolved["manifest"],
    }


def _shared_candidate_identity(
    certificates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    identities = {
        mode: _certificate_candidate_identity(item["report"])
        for mode, item in certificates.items()
    }
    values = list(identities.values())
    if any(identity != values[0] for identity in values[1:]):
        raise SpecValidationError(
            "spot and futures certificates use different strategy, wheel, "
            "reference, or release scope identities"
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
        "engine_source_fingerprint": engine_build["source_fingerprint"],
        "reference": inputs["reference"],
        "timerange": claim["timerange"],
        "pair_count": claim["pair_count"],
        "timeframes": claim["timeframes"],
        "continuous_timerange": claim["continuous_timerange"],
    }


def _load_platform_evidence(
    paths: list[Path],
    *,
    certificates: dict[str, dict[str, Any]],
    shared_identity: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        document = read_json(path)
        if not isinstance(document, dict):
            raise SpecValidationError(f"platform evidence must be an object: {path}")
        mode = document.get("mode_contract")
        if mode not in REQUIRED_MODE_CONTRACTS:
            raise SpecValidationError("platform evidence has an unsupported mode")
        if mode in result:
            raise SpecValidationError(f"duplicate platform evidence for {mode}")
        platforms = document.get("platforms")
        workload = document.get("workload")
        if not isinstance(platforms, list):
            raise SpecValidationError(f"platform evidence is incomplete for {mode}")
        systems = {
            item.get("system")
            for item in platforms
            if isinstance(item, dict)
        }
        if (
            document.get("schema_version") != "1.0.0"
            or document.get("release_certified") is not True
            or systems != REQUIRED_PLATFORM_SYSTEMS
            or not isinstance(workload, dict)
            or workload.get("mode_contract") != mode
            or workload.get("strategy_sha256") != shared_identity["strategy_sha256"]
            or document.get("package_version") != shared_identity["package_version"]
        ):
            raise SpecValidationError(f"platform evidence is incomplete for {mode}")
        linux = next(
            (
                item
                for item in platforms
                if isinstance(item, dict) and item.get("system") == "linux"
            ),
            None,
        )
        certificate_wheel = certificates[mode]["report"]["gates"][
            "installed_wheel"
        ]["sha256"]
        if not isinstance(linux, dict) or linux.get("wheel_sha256") != certificate_wheel:
            raise SpecValidationError(
                f"Linux platform wheel differs from the {mode} certificate"
            )
        bundle_path = path.parent / "bundle.json"
        bundle = read_json(bundle_path) if bundle_path.is_file() else None
        validated_bundle = _validate_evidence_bundle(
            bundle,
            root=path.parent,
            expected_document=document,
            label=f"{mode} platform evidence",
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
