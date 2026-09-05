"""Fixed-candidate operations soak and public 10/10 release audit."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .combined_release import (
    COMBINED_RELEASE_GATE_NAME,
    COMBINED_RELEASE_REPORT_NAME,
    verify_combined_release_assets,
)
from .errors import SpecValidationError
from .fixture import sha256_file
from .product_support_contract import load_product_support_contract
from .release_gate import RELEASE_CHECKSUMS_NAME
from .release_provenance import DEFAULT_PROVENANCE_POLICY, ProvenancePolicy

SOAK_CYCLE_VERSION = "operations-soak-cycle-v1"
TEN_OF_TEN_AUDIT_VERSION = "ten-of-ten-release-audit-v1"
REQUIRED_SOAK_CHECKS = frozenset(
    {"compatibility", "discovery", "nightly", "public_install", "asset_restore"}
)
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RC_TAG = re.compile(r"^v(\d+\.\d+\.\d+)-rc\.\d+$")
MIN_SOAK_CYCLE_INTERVAL = timedelta(hours=24)
REQUIRED_SOAK_CYCLES = 7
MIN_SOAK_DURATION = MIN_SOAK_CYCLE_INTERVAL * (REQUIRED_SOAK_CYCLES - 1)


def record_operations_soak_cycle(
    *,
    candidate_commit: str,
    release_tag: str,
    cycle: int,
    checked_at: str,
    public_manifest_sha256: str,
    checks: Mapping[str, Mapping[str, Any]],
    output_path: str | Path,
) -> dict[str, Any]:
    """Seal one successful cycle without executing or duplicating existing workflows."""
    _validate_identity(candidate_commit, release_tag)
    if cycle not in range(1, REQUIRED_SOAK_CYCLES + 1):
        raise SpecValidationError("operations soak cycle must be between 1 and 7")
    _timestamp(checked_at)
    if _SHA256.fullmatch(public_manifest_sha256) is None:
        raise SpecValidationError("operations soak public manifest hash is invalid")
    normalized = _validate_checks(checks, candidate_commit)
    document = {
        "schema_version": SOAK_CYCLE_VERSION,
        "candidate_commit": candidate_commit,
        "release_tag": release_tag,
        "cycle": cycle,
        "checked_at": checked_at,
        "public_manifest_sha256": public_manifest_sha256,
        "checks": normalized,
        "complete": True,
    }
    destination = Path(output_path)
    if destination.exists():
        raise SpecValidationError("operations soak receipt already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, document)
    return document


def seal_ten_of_ten_release_audit(
    *,
    release_directory: str | Path,
    candidate_commit: str,
    release_tag: str,
    soak_receipt_paths: Sequence[str | Path],
    output_path: str | Path,
    product_contract_path: str | Path | None = None,
    provenance_policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
) -> dict[str, Any]:
    """Bind seven fixed-RC cycles to the already certified public asset graph."""
    version = _validate_identity(candidate_commit, release_tag)
    contract = load_product_support_contract(product_contract_path)
    required_cycles = contract["operations"]["release_soak_cycles"]
    if (
        required_cycles != REQUIRED_SOAK_CYCLES
        or len(soak_receipt_paths) != required_cycles
    ):
        raise SpecValidationError("10/10 audit requires exactly seven soak receipts")

    receipts: list[tuple[Path, dict[str, Any]]] = []
    for supplied in soak_receipt_paths:
        path = Path(supplied)
        document = read_json(path)
        if not isinstance(document, dict):
            raise SpecValidationError("operations soak receipt must be an object")
        _validate_receipt(document, candidate_commit, release_tag, contract)
        receipts.append((path, document))
    receipts.sort(key=lambda item: item[1]["cycle"])
    if [item[1]["cycle"] for item in receipts] != list(
        range(1, REQUIRED_SOAK_CYCLES + 1)
    ):
        raise SpecValidationError("operations soak receipts must cover cycles 1 through 7")
    timestamps = [_timestamp(item[1]["checked_at"]) for item in receipts]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise SpecValidationError("operations soak receipt timestamps are not increasing")
    if any(
        right - left < MIN_SOAK_CYCLE_INTERVAL
        for left, right in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise SpecValidationError("operations soak cycles must be at least 24 hours apart")
    if timestamps[-1] - timestamps[0] < MIN_SOAK_DURATION:
        raise SpecValidationError("operations soak must cover the full seven-cycle span")
    manifest_hashes = {item[1]["public_manifest_sha256"] for item in receipts}
    if len(manifest_hashes) != 1:
        raise SpecValidationError("operations soak cycles do not target identical public bytes")

    root = Path(release_directory)
    gate = verify_combined_release_assets(
        root,
        expected_commit=candidate_commit,
        provenance_policy=provenance_policy,
    )
    if gate.get("package_version") != version:
        raise SpecValidationError("release tag version differs from combined release")
    supply_chain = gate.get("supply_chain")
    if not isinstance(supply_chain, dict):
        raise SpecValidationError("10/10 audit requires public supply-chain identity")
    manifest = root / RELEASE_CHECKSUMS_NAME
    if sha256_file(manifest) != next(iter(manifest_hashes)):
        raise SpecValidationError("soak receipts differ from audited public release bytes")

    legacy = contract["strategies"]["official_only_legacy"]
    if any(
        item.get("fallback_status") != "qualified"
        or item.get("native_supported") is not False
        for item in legacy
    ):
        raise SpecValidationError("legacy strategy boundary is not qualified Official-only")
    graph = {
        "product_support_contract": _path_record(
            Path(product_contract_path)
            if product_contract_path is not None
            else _packaged_contract_path()
        ),
        "release_gate": _path_record(root / COMBINED_RELEASE_GATE_NAME),
        "combined_report": _path_record(root / COMBINED_RELEASE_REPORT_NAME),
        "public_manifest": _path_record(manifest),
        "distributions": supply_chain["distribution_sha256"],
        "distribution_identity": _path_record(
            root / supply_chain["distribution_identity"]["file"]
        ),
        "sbom": _path_record(root / supply_chain["sbom"]["file"]),
        "cross_channel_identity_sha256": supply_chain["identity_sha256"],
        "platform_provenance": gate["platform_evidence"],
        "soak_receipts": [_path_record(path) for path, _document in receipts],
    }
    document_without_identity = {
        "schema_version": TEN_OF_TEN_AUDIT_VERSION,
        "candidate_commit": candidate_commit,
        "release_tag": release_tag,
        "package_version": version,
        "completed_at": receipts[-1][1]["checked_at"],
        "combined_full_x7_certified": True,
        "supported_platform_slugs": contract["platforms"]["supported"],
        "legacy_boundary": {
            "default_selection_excluded": True,
            "official_only": [item["family"] for item in legacy],
            "native_certification_allowed": False,
        },
        "operations": {
            "cycles": required_cycles,
            "checks_per_cycle": sorted(REQUIRED_SOAK_CHECKS),
            "fixed_public_manifest_sha256": next(iter(manifest_hashes)),
        },
        "identity_graph": graph,
        "complete": True,
    }
    document = {
        **document_without_identity,
        "identity_sha256": _canonical_sha256(document_without_identity),
    }
    destination = Path(output_path)
    if destination.exists():
        raise SpecValidationError("10/10 audit report already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, document)
    return document


def _validate_receipt(
    document: Mapping[str, Any],
    candidate_commit: str,
    release_tag: str,
    contract: Mapping[str, Any],
) -> None:
    cycle = document.get("cycle")
    if (
        document.get("schema_version") != SOAK_CYCLE_VERSION
        or document.get("candidate_commit") != candidate_commit
        or document.get("release_tag") != release_tag
        or document.get("complete") is not True
        or not isinstance(cycle, int)
        or isinstance(cycle, bool)
        or cycle not in range(1, REQUIRED_SOAK_CYCLES + 1)
        or _SHA256.fullmatch(str(document.get("public_manifest_sha256"))) is None
    ):
        raise SpecValidationError("operations soak receipt identity is incomplete")
    _timestamp(str(document.get("checked_at")))
    checks = document.get("checks")
    if not isinstance(checks, dict):
        raise SpecValidationError("operations soak receipt checks are malformed")
    normalized = _validate_checks(checks, candidate_commit)
    maximum_age = contract["operations"]["latest_checked_max_age_seconds"]
    for name in ("compatibility", "discovery"):
        age = normalized[name].get("latest_checked_age_seconds")
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= maximum_age:
            raise SpecValidationError(f"operations soak {name} freshness exceeds policy")


def _validate_checks(
    checks: Mapping[str, Mapping[str, Any]], candidate_commit: str
) -> dict[str, dict[str, Any]]:
    if set(checks) != REQUIRED_SOAK_CHECKS:
        raise SpecValidationError("operations soak checks are incomplete")
    normalized: dict[str, dict[str, Any]] = {}
    for name in sorted(checks):
        record = checks[name]
        if (
            not isinstance(record, Mapping)
            or record.get("conclusion") != "success"
            or record.get("head_sha") != candidate_commit
            or not isinstance(record.get("run_id"), int)
            or isinstance(record.get("run_id"), bool)
            or record["run_id"] <= 0
            or _SHA256.fullmatch(str(record.get("evidence_sha256"))) is None
        ):
            raise SpecValidationError(f"operations soak {name} check is incomplete")
        normalized[name] = dict(record)
    return normalized


def _validate_identity(candidate_commit: str, release_tag: str) -> str:
    if _SHA1.fullmatch(candidate_commit) is None:
        raise SpecValidationError("10/10 audit candidate commit is invalid")
    matched = _RC_TAG.fullmatch(release_tag)
    if matched is None:
        raise SpecValidationError("10/10 audit requires a vX.Y.Z-rc.N tag")
    return matched.group(1)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpecValidationError("operations soak timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SpecValidationError("operations soak timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _path_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SpecValidationError(f"10/10 audit input is not a regular file: {path}")
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _packaged_contract_path() -> Path:
    return Path(__file__).resolve().parent / "contracts" / "product-support-contract-v1.json"


def _canonical_sha256(document: Mapping[str, Any]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "MIN_SOAK_CYCLE_INTERVAL",
    "MIN_SOAK_DURATION",
    "REQUIRED_SOAK_CYCLES",
    "REQUIRED_SOAK_CHECKS",
    "SOAK_CYCLE_VERSION",
    "TEN_OF_TEN_AUDIT_VERSION",
    "record_operations_soak_cycle",
    "seal_ten_of_ten_release_audit",
]
