"""DSSE Ed25519 provenance for offline release certification."""
# noqa: E501  # SIZE_OK — one fail-closed provenance and durable replay state machine.

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, assert_never

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .canonical import loads_json_bytes, write_json
from .errors import SpecValidationError
from .sqlite_admission import (
    SqliteAdmissionPolicy,
    SqliteAdmissionTarget,
    admit_sqlite_state,
)
from .sqlite_publication import publish_sqlite_main, recover_sqlite_publication

PROVENANCE_ENVELOPE_VERSION = "2.0.0"
PLATFORM_EVIDENCE_VERSION = "2.0.0"
DSSE_PAYLOAD_TYPE = "application/vnd.nfi.release-provenance.v2+json"
DEFAULT_REPOSITORY = "vntrevx/NFI_BackTestEngine"
DEFAULT_REPOSITORY_REF = "refs/heads/main"
DEFAULT_PLATFORM_WORKFLOW = "Build release candidate"
DEFAULT_PLATFORM_WORKFLOW_REF = ".github/workflows/release.yml@refs/heads/main"
DEFAULT_SIGNING_JOB = "provenance-signing"
PRODUCTION_KEY_ID = "nfi-release-ed25519-2026-03"
PRODUCTION_PUBLIC_KEY = base64.b64decode("2Mn2hsM1wkqgwkgX17HlevcwcTytLjuyO7BRwTEM+qI=")
_SUPPORTED_PROVENANCE_PLATFORM_SYSTEMS = frozenset({"darwin", "linux", "windows"})
_PRODUCT_PROVENANCE_PLATFORM_SYSTEMS = frozenset({"darwin", "linux"})
_LEDGER_UMASK_LOCK = threading.Lock()
_LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS certificate_publications ("
    "bundle_id TEXT PRIMARY KEY, certificate_sha256 TEXT NOT NULL, "
    "attempt_id TEXT NOT NULL, state TEXT NOT NULL "
    "CHECK(state IN ('reserved', 'published', 'aborted')), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
)
_LEDGER_STORED_SCHEMA = _LEDGER_SCHEMA.replace(" IF NOT EXISTS", "")
_LEGACY_LEDGER_STORED_SCHEMA = (
    "CREATE TABLE used_certificates (bundle_id TEXT PRIMARY KEY, "
    "certificate_sha256 TEXT NOT NULL, used_at TEXT NOT NULL)"
)
_ACCEPTED_LEDGER_TABLES = frozenset(
    {
        frozenset({"certificate_publications"}),
        frozenset({"used_certificates"}),
        frozenset({"certificate_publications", "used_certificates"}),
    }
)
_LEDGER_ADMISSION_POLICY = SqliteAdmissionPolicy(
    expected_schemas={
        "certificate_publications": _LEDGER_STORED_SCHEMA,
        "used_certificates": _LEGACY_LEDGER_STORED_SCHEMA,
    },
    accepted_tables=_ACCEPTED_LEDGER_TABLES,
)


@dataclass(frozen=True, slots=True)
class ProvenancePolicy:
    """Pinned public roots and exact GitHub producer identity."""

    policy_id: str
    repository: str
    repository_ref: str
    workflow: str
    workflow_ref: str
    job: str
    keys: Mapping[str, bytes]
    max_lifetime: timedelta = timedelta(hours=24)


DEFAULT_PROVENANCE_POLICY = ProvenancePolicy(
    policy_id="nfi-github-release-dsse-v2",
    repository=DEFAULT_REPOSITORY,
    repository_ref=DEFAULT_REPOSITORY_REF,
    workflow=DEFAULT_PLATFORM_WORKFLOW,
    workflow_ref=DEFAULT_PLATFORM_WORKFLOW_REF,
    job=DEFAULT_SIGNING_JOB,
    keys={PRODUCTION_KEY_ID: PRODUCTION_PUBLIC_KEY},
)


def canonical_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def workload_identity(workload: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {key: value for key, value in workload.items() if key != "identity_sha256"}
    )


def candidate_distribution_identity(records: Mapping[str, str]) -> str:
    """Hash the sorted distribution filename/digest set, excluding evidence files."""
    if not records or any(not _is_sha256(value) for value in records.values()):
        raise SpecValidationError("candidate distribution identity is incomplete")
    return canonical_sha256(dict(sorted(records.items())))


def create_platform_statement(
    report_path: str | Path,
    *,
    repository: str,
    repository_ref: str,
    workflow: str,
    workflow_ref: str,
    job: str,
    commit: str,
    run_id: str,
    run_attempt: int,
    candidate_id: str,
    bundle_id: str,
    challenge: str,
    nonce: str,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Construct the complete statement signed by the protected coordinator."""
    path = Path(report_path)
    report_bytes = path.read_bytes()
    report = loads_json_bytes(report_bytes)
    if not isinstance(report, dict):
        raise SpecValidationError("platform provenance subject must be a JSON object")
    package = _mapping(report, "package")
    platform = _mapping(report, "platform")
    workload = _mapping(report, "workload")
    issued = _parse_timestamp(issued_at or _utc_now())
    expires = _parse_timestamp(
        expires_at
        or (issued + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    )
    bundle = {
        "bundle_id": bundle_id,
        "candidate_id": candidate_id,
        "challenge": challenge,
        "nonce": nonce,
    }
    statement: dict[str, Any] = {
        "schema_version": PROVENANCE_ENVELOPE_VERSION,
        "signature_algorithm": "Ed25519",
        "subject": {
            "name": path.name,
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
            "document_sha256": canonical_sha256(report),
        },
        "producer": {
            "repository": repository,
            "repository_ref": repository_ref,
            "workflow": workflow,
            "workflow_ref": workflow_ref,
            "job": job,
            "commit": commit,
            "run_id": str(run_id),
            "run_attempt": run_attempt,
        },
        "runner": {
            "os": platform.get("system"),
            "machine": platform.get("machine"),
        },
        "bundle": bundle,
        "candidate": {
            "wheel_sha256": package.get("wheel_sha256"),
            "native_extension_sha256": package.get("native_extension_sha256"),
            "installed_extension_sha256": package.get("installed_extension_sha256"),
            "portable_package_sha256": package.get("portable_package_sha256"),
        },
        "workload_identity_sha256": workload_identity(workload),
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
    }
    bundle["attestation_id"] = canonical_sha256(statement)
    _validate_statement(statement)
    return statement


def prepare_statement_signing_bytes(
    statement: Mapping[str, Any],
) -> tuple[bytes, bytes]:
    """Return canonical payload and standard DSSE PAE bytes for isolated signing."""
    _validate_statement(statement)
    payload = _canonical_bytes(statement)
    return payload, _dsse_pae(DSSE_PAYLOAD_TYPE, payload)


def assemble_statement_envelope(
    payload: bytes,
    signature: bytes,
    *,
    key_id: str = PRODUCTION_KEY_ID,
    public_key_bytes: bytes = PRODUCTION_PUBLIC_KEY,
) -> dict[str, Any]:
    """Assemble and verify an externally signed canonical DSSE envelope."""
    statement = loads_json_bytes(payload)
    _validate_statement(statement)
    assert isinstance(statement, dict)
    if payload != _canonical_bytes(statement):
        raise SpecValidationError("provenance payload is not canonical JSON")
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature, _dsse_pae(DSSE_PAYLOAD_TYPE, payload)
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise SpecValidationError("provenance signature verification failed") from exc
    return {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {"keyid": key_id, "sig": base64.b64encode(signature).decode("ascii")}
        ],
    }


def sign_statement(
    statement: Mapping[str, Any],
    *,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    """Create a DSSE envelope using cryptography's reviewed Ed25519 implementation."""
    payload, pae = prepare_statement_signing_bytes(statement)
    signature = private_key.sign(pae)
    return {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {"keyid": key_id, "sig": base64.b64encode(signature).decode("ascii")}
        ],
    }


def load_signing_key(pem: bytes, *, password: bytes | None = None) -> Ed25519PrivateKey:
    """Load only an Ed25519 PEM key; callers own secret acquisition and zeroization."""
    try:
        key = load_pem_private_key(pem, password=password)
    except (TypeError, ValueError) as exc:
        raise SpecValidationError("release provenance private key is malformed") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SpecValidationError("release provenance key must be Ed25519")
    return key


def write_signed_platform_provenance(
    report_path: str | Path,
    envelope_path: str | Path,
    *,
    repository: str,
    repository_ref: str,
    workflow: str,
    workflow_ref: str,
    job: str,
    commit: str,
    run_id: str,
    run_attempt: int,
    candidate_id: str,
    bundle_id: str,
    challenge: str,
    nonce: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    statement = create_platform_statement(
        report_path,
        repository=repository,
        repository_ref=repository_ref,
        workflow=workflow,
        workflow_ref=workflow_ref,
        job=job,
        commit=commit,
        run_id=run_id,
        run_attempt=run_attempt,
        candidate_id=candidate_id,
        bundle_id=bundle_id,
        challenge=challenge,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    envelope = sign_statement(statement, key_id=key_id, private_key=private_key)
    write_json(envelope_path, envelope)
    return envelope


def verify_platform_envelope(
    report: Mapping[str, Any],
    envelope: Any,
    *,
    report_bytes: bytes,
    policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    expected_commit: str | None = None,
    expected_run_id: str | None = None,
    expected_run_attempt: int | None = None,
    expected_candidate_id: str | None = None,
    expected_bundle_id: str | None = None,
    expected_challenge: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify DSSE, producer policy, freshness, artifact bytes, and report claims."""
    payload, signature_record = _decode_dsse(envelope)
    key_id = signature_record["keyid"]
    public_bytes = policy.keys.get(key_id)
    if public_bytes is None:
        raise SpecValidationError("provenance uses an untrusted signing key")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        signature = base64.b64decode(signature_record["sig"], validate=True)
        public_key.verify(signature, _dsse_pae(DSSE_PAYLOAD_TYPE, payload))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise SpecValidationError("provenance signature verification failed") from exc
    try:
        statement = loads_json_bytes(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SpecValidationError("provenance payload is malformed") from exc
    _validate_statement(statement)
    assert isinstance(statement, dict)
    if payload != _canonical_bytes(statement):
        raise SpecValidationError("provenance payload is not canonical JSON")

    producer = _mapping(statement, "producer")
    expected_producer = {
        "repository": policy.repository,
        "repository_ref": policy.repository_ref,
        "workflow": policy.workflow,
        "workflow_ref": policy.workflow_ref,
        "job": policy.job,
    }
    if any(producer[key] != value for key, value in expected_producer.items()):
        raise SpecValidationError("provenance repository, ref, workflow, or job is not trusted")
    if expected_commit is not None and producer["commit"] != expected_commit:
        raise SpecValidationError("provenance commit differs from candidate commit")
    if expected_run_id is not None and producer["run_id"] != expected_run_id:
        raise SpecValidationError("provenance run identity differs")
    if expected_run_attempt is not None and producer["run_attempt"] != expected_run_attempt:
        raise SpecValidationError("provenance run attempt differs")
    bundle = _mapping(statement, "bundle")
    for value, field, message in (
        (expected_candidate_id, "candidate_id", "candidate identity"),
        (expected_bundle_id, "bundle_id", "bundle identity"),
        (expected_challenge, "challenge", "bundle challenge"),
    ):
        if value is not None and bundle[field] != value:
            raise SpecValidationError(f"provenance {message} differs")

    issued = _parse_timestamp(statement["issued_at"])
    expires = _parse_timestamp(statement["expires_at"])
    current = now or datetime.now(UTC)
    if (
        expires <= issued
        or expires - issued > policy.max_lifetime
        or current < issued - timedelta(minutes=5)
        or current >= expires
    ):
        raise SpecValidationError("provenance is stale, expired, or has a future timestamp")

    package = _mapping(report, "package")
    platform = _mapping(report, "platform")
    workload = _mapping(report, "workload")
    subject = _mapping(statement, "subject")
    if (
        loads_json_bytes(report_bytes) != report
        or subject["sha256"] != hashlib.sha256(report_bytes).hexdigest()
        or subject["document_sha256"] != canonical_sha256(report)
    ):
        raise SpecValidationError("provenance subject digest differs from report bytes")
    candidate = _mapping(statement, "candidate")
    for key in (
        "wheel_sha256",
        "native_extension_sha256",
        "installed_extension_sha256",
        "portable_package_sha256",
    ):
        if candidate[key] != package.get(key):
            raise SpecValidationError("provenance candidate wheel or extension digest differs")
    recomputed_workload = workload_identity(workload)
    if (
        workload.get("identity_sha256") != recomputed_workload
        or statement["workload_identity_sha256"] != recomputed_workload
    ):
        raise SpecValidationError("provenance workload identity differs")
    runner = _mapping(statement, "runner")
    if runner["os"] != platform.get("system") or runner["machine"] != platform.get("machine"):
        raise SpecValidationError("provenance runner identity differs from report")
    _recompute_platform_success(report)
    return statement


def verify_embedded_platform_evidence(
    document: Mapping[str, Any],
    *,
    policy: ProvenancePolicy = DEFAULT_PROVENANCE_POLICY,
    expected_commit: str | None = None,
    expected_run_id: str | None = None,
    expected_run_attempt: int | None = None,
    expected_candidate_id: str | None = None,
    expected_bundle_id: str | None = None,
    expected_challenge: str | None = None,
    required_platform_systems: frozenset[str] = _SUPPORTED_PROVENANCE_PLATFORM_SYSTEMS,
) -> dict[str, Any]:
    """Recompute a complete embedded graph for the required platforms."""
    provenance = document.get("provenance")
    attestations = provenance.get("attestations") if isinstance(provenance, dict) else None
    if document.get("schema_version") != PLATFORM_EVIDENCE_VERSION or not isinstance(
        attestations, list
    ):
        raise SpecValidationError("platform evidence has no signed provenance graph")
    assert isinstance(provenance, dict)
    if required_platform_systems not in (
        _PRODUCT_PROVENANCE_PLATFORM_SYSTEMS,
        _SUPPORTED_PROVENANCE_PLATFORM_SYSTEMS,
    ):
        raise SpecValidationError("platform evidence required systems are unauthorized")
    if (
        provenance.get("policy_id") != policy.policy_id
        or len(attestations) != len(required_platform_systems)
    ):
        raise SpecValidationError("platform evidence provenance policy or cardinality differs")
    graph_bundle_id = provenance.get("bundle_id")
    graph_candidate_id = provenance.get("candidate_id")
    graph_challenge = provenance.get("challenge")
    for supplied, actual, label in (
        (expected_bundle_id, graph_bundle_id, "bundle identity"),
        (expected_candidate_id, graph_candidate_id, "candidate identity"),
        (expected_challenge, graph_challenge, "bundle challenge"),
    ):
        if supplied is not None and supplied != actual:
            raise SpecValidationError(f"platform evidence {label} differs")

    verified: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for item in attestations:
        if not isinstance(item, dict) or set(item) != {"report", "report_bytes", "envelope"}:
            raise SpecValidationError("malformed embedded platform attestation")
        report = item["report"]
        if not isinstance(report, dict):
            raise SpecValidationError("malformed embedded platform report")
        try:
            report_bytes = base64.b64decode(item["report_bytes"], validate=True)
        except (TypeError, ValueError) as exc:
            raise SpecValidationError("malformed embedded platform report bytes") from exc
        verified.append(
            verify_platform_envelope(
                report,
                item["envelope"],
                report_bytes=report_bytes,
                policy=policy,
                expected_commit=expected_commit,
                expected_run_id=expected_run_id,
                expected_run_attempt=expected_run_attempt,
                expected_candidate_id=str(graph_candidate_id),
                expected_bundle_id=str(graph_bundle_id),
                expected_challenge=str(graph_challenge),
            )
        )
        reports.append(report)
    attestation_ids = {item["bundle"]["attestation_id"] for item in verified}
    nonces = {item["bundle"]["nonce"] for item in verified}
    run_identities = {
        (item["producer"]["run_id"], item["producer"]["run_attempt"]) for item in verified
    }
    if (
        len(attestation_ids) != len(required_platform_systems)
        or len(nonces) != len(required_platform_systems)
        or len(run_identities) != 1
    ):
        raise SpecValidationError("platform provenance run, nonce, or attestation was replayed")
    commits = {item["producer"]["commit"] for item in verified}
    if len(commits) != 1:
        raise SpecValidationError("platform provenance commits differ")
    commit = next(iter(commits))
    _verify_evidence_projection(
        document, reports, commit, required_platform_systems=required_platform_systems
    )
    return {
        "commit": commit,
        "candidate_id": graph_candidate_id,
        "bundle_id": graph_bundle_id,
        "challenge": graph_challenge,
        "reports": reports,
        "statements": verified,
    }


def reserve_certificate_publication(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
    attempt_id: str,
) -> str:
    """Reserve a hash-bound publication for one durable attempt."""
    _validate_publication_identity(bundle_id, certificate_sha256, attempt_id)
    with _secure_ledger(ledger_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT certificate_sha256, attempt_id, state "
            "FROM certificate_publications WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        now = datetime.now(UTC).isoformat()
        if row is None:
            connection.execute(
                "INSERT INTO certificate_publications "
                "(bundle_id, certificate_sha256, attempt_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, 'reserved', ?, ?)",
                (bundle_id, certificate_sha256, attempt_id, now, now),
            )
            connection.commit()
            _ledger_transition_checkpoint(bundle_id, attempt_id, None, "reserved")
            return "reserved"
        existing_hash, existing_attempt, state = row
        if existing_hash != certificate_sha256 or existing_attempt != attempt_id:
            connection.rollback()
            raise SpecValidationError(
                "release provenance bundle challenge was already used by another publication"
            )
        if state == "published":
            connection.rollback()
            return "published"
        connection.execute(
            "UPDATE certificate_publications SET state = 'reserved', updated_at = ? "
            "WHERE bundle_id = ?",
            (now, bundle_id),
        )
        connection.commit()
        if state != "reserved":
            _ledger_transition_checkpoint(bundle_id, attempt_id, state, "reserved")
        return "reserved"


def _ledger_transition_checkpoint(
    _bundle_id: str,
    _attempt_id: str,
    _previous: str | None,
    _current: str,
) -> None:
    """Deterministic ledger transition observation hook for concurrency tests."""


def mark_certificate_published(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
    attempt_id: str,
) -> None:
    """Finalize a matching reservation after the atomic public rename."""
    _set_publication_state(
        ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
        attempt_id=attempt_id,
        target="published",
    )


def abort_certificate_publication(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
    attempt_id: str,
) -> None:
    """Record a failed owned attempt without releasing its replay identity."""
    _set_publication_state(
        ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
        attempt_id=attempt_id,
        target="aborted",
    )


def require_published_certificate(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
) -> None:
    """Require a durable published row matching the exact certificate bytes."""
    if not _is_sha256(bundle_id) or not _is_sha256(certificate_sha256):
        raise SpecValidationError("certificate replay ledger identity is malformed")
    with _secure_ledger(ledger_path, write=False) as connection:
        row = connection.execute(
            "SELECT certificate_sha256, state FROM certificate_publications "
            "WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
    if row != (certificate_sha256, "published"):
        raise SpecValidationError(
            "combined release has no matching durable published claim"
        )


def claim_certificate_once(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
) -> None:
    """Compatibility wrapper for one-step non-public certificate claims."""
    attempt_id = f"legacy-{certificate_sha256}"
    state = reserve_certificate_publication(
        ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
        attempt_id=attempt_id,
    )
    if state == "published":
        raise SpecValidationError("release provenance bundle challenge was already used")
    mark_certificate_published(
        ledger_path,
        bundle_id=bundle_id,
        certificate_sha256=certificate_sha256,
        attempt_id=attempt_id,
    )


def _set_publication_state(
    ledger_path: str | Path,
    *,
    bundle_id: str,
    certificate_sha256: str,
    attempt_id: str,
    target: str,
) -> None:
    _validate_publication_identity(bundle_id, certificate_sha256, attempt_id)
    with _secure_ledger(ledger_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT certificate_sha256, attempt_id, state "
            "FROM certificate_publications WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        if row is None or row[0] != certificate_sha256 or row[1] != attempt_id:
            connection.rollback()
            raise SpecValidationError("publication reservation ownership differs")
        if target == "published" and row[2] not in {"reserved", "published"}:
            connection.rollback()
            raise SpecValidationError("publication reservation is not active")
        if target == "aborted" and row[2] != "reserved":
            connection.rollback()
            return
        connection.execute(
            "UPDATE certificate_publications SET state = ?, updated_at = ? "
            "WHERE bundle_id = ?",
            (target, datetime.now(UTC).isoformat(), bundle_id),
        )
        connection.commit()
        if row[2] != target:
            _ledger_transition_checkpoint(bundle_id, attempt_id, row[2], target)


def _validate_publication_identity(
    bundle_id: str, certificate_sha256: str, attempt_id: str
) -> None:
    if (
        not _is_sha256(bundle_id)
        or not _is_sha256(certificate_sha256)
        or not isinstance(attempt_id, str)
        or not attempt_id
        or len(attempt_id) > 200
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in attempt_id
        )
    ):
        raise SpecValidationError("certificate replay ledger identity is malformed")


@contextmanager
def _secure_ledger(
    ledger_path: str | Path,
    *,
    write: bool = True,
) -> Generator[sqlite3.Connection]:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_NOATIME")
    ):
        raise SpecValidationError(
            "durable publication ledger requires POSIX no-follow and no-atime support"
        )
    import fcntl

    path = Path(ledger_path).absolute()
    parent = path.parent
    _reject_symlink_components(parent)
    parent_stat = parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise SpecValidationError(
            "publication ledger parent must be owner-controlled with 0700 permissions"
        )
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    database_fd = -1
    connection: sqlite3.Connection | None = None
    created = False
    initialized = False
    database_stat: os.stat_result | None = None
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        recover_sqlite_publication(path)
        path_exists = path.exists() or path.is_symlink()
        if path_exists:
            existing_stat = path.lstat()
            if not stat.S_ISREG(existing_stat.st_mode):
                raise SpecValidationError(
                    "publication ledger must not be a symlink and must be a regular file"
                )
        database_fd = os.open(
            path.name,
            os.O_RDWR
            | os.O_NOFOLLOW
            | os.O_NOATIME
            | (0 if path_exists else os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=directory_fd,
        )
        created = not path_exists
        database_stat = os.fstat(database_fd)
        if (
            not stat.S_ISREG(database_stat.st_mode)
            or database_stat.st_uid != os.getuid()
            or stat.S_IMODE(database_stat.st_mode) != 0o600
            or database_stat.st_nlink != 1
        ):
            raise SpecValidationError(
                "publication ledger must be an owner-controlled, single-link 0600 regular file"
            )
        fcntl.flock(database_fd, fcntl.LOCK_EX)
        path_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (path_stat.st_dev, path_stat.st_ino) != (
            database_stat.st_dev,
            database_stat.st_ino,
        ):
            raise SpecValidationError("publication ledger path changed during secure open")
        _validate_sqlite_sidecars(path)
        parent_entries = frozenset(item.name for item in parent.iterdir())
        if created and parent_entries.intersection(
            {f"{path.name}-wal", f"{path.name}-shm", f"{path.name}-journal"}
        ):
            raise SpecValidationError(
                "publication ledger sidecars require a pre-existing main database"
            )
        descriptor_snapshot = _snapshot_file_descriptors()
        preexisting_main_fds = {
            fd
            for fd, identity in descriptor_snapshot.items()
            if fd != database_fd and identity[2] == str(path)
        }
        if preexisting_main_fds:
            raise SpecValidationError(
                "publication ledger is already open outside the trusted connection"
            )
        admission_context = (
            admit_sqlite_state(
                SqliteAdmissionTarget(
                    path=path,
                    directory_fd=directory_fd,
                    database_fd=database_fd,
                    expected=database_stat,
                    parent_entries=parent_entries,
                ),
                _LEDGER_ADMISSION_POLICY,
            )
            if not created
            else nullcontext(None)
        )
        with admission_context as admission:
            _ledger_preflight_checkpoint(path)
            if admission is not None:
                admission.revalidate()
                private_context = nullcontext(admission.private_path)
            else:
                _verify_preflight_identity(
                    path,
                    directory_fd=directory_fd,
                    database_fd=database_fd,
                    expected=database_stat,
                    parent_entries=parent_entries,
                )
                private_context = tempfile.TemporaryDirectory(
                    prefix="nfi-ledger-private-"
                )
            with private_context as private_value:
                match private_value:
                    case Path() as admitted_path:
                        private_path = admitted_path
                    case str() as private_directory:
                        private_path = Path(private_directory) / path.name
                    case unreachable:
                        assert_never(unreachable)
                completed = False
                with _LEDGER_UMASK_LOCK:
                    previous_umask = os.umask(0o077)
                    try:
                        if admission is not None:
                            admission.revalidate()
                        connection = sqlite3.connect(
                            private_path,
                            timeout=30,
                            isolation_level=None,
                        )
                        _ledger_private_checkpoint("connected")
                        connection.execute("PRAGMA journal_mode=WAL")
                        connection.execute("PRAGMA synchronous=FULL")
                        _initialize_ledger_schema(connection)
                        yield connection
                        _ledger_private_checkpoint("sql-complete")
                        completed = True
                    finally:
                        if connection is not None:
                            if completed and write:
                                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                                connection.execute("PRAGMA journal_mode=DELETE")
                            connection.close()
                            connection = None
                        os.umask(previous_umask)
                if completed and write:
                    _validate_private_ledger(private_path)
                    if admission is not None:
                        admission.revalidate(atime_must_match=False)
                    else:
                        _verify_preflight_identity(
                            path,
                            directory_fd=directory_fd,
                            database_fd=database_fd,
                            expected=database_stat,
                            parent_entries=parent_entries,
                            atime_must_match=False,
                        )
                    publish_sqlite_main(
                        path,
                        private_path,
                        _ledger_publication_checkpoint,
                    )
                    initialized = True
    except OSError as exc:
        raise SpecValidationError("publication ledger secure open failed") from exc
    finally:
        if (
            created
            and not initialized
            and database_fd >= 0
            and database_stat is not None
        ):
            _remove_new_empty_ledger(
                path,
                directory_fd=directory_fd,
                database_fd=database_fd,
                expected=database_stat,
            )
        if database_fd >= 0:
            os.close(database_fd)
        os.close(directory_fd)


def _ledger_preflight_checkpoint(_path: Path) -> None:
    """Deterministic race boundary after content acceptance and before SQLite opens."""


def _ledger_private_checkpoint(_name: str) -> None:
    """Deterministic observation hook for private SQLite work."""


def _ledger_publication_checkpoint(_name: str) -> None:
    """Deterministic observation hook for recoverable public replacement."""


def _validate_private_ledger(path: Path) -> None:
    sidecars = [path.with_name(f"{path.name}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    if any(sidecar.exists() for sidecar in sidecars):
        raise SpecValidationError("private publication ledger was not checkpointed")
    try:
        uri = f"file:{path}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            schemas = {
                str(table): str(schema)
                for table, schema in connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            }
    except sqlite3.DatabaseError as exc:
        raise SpecValidationError("private publication ledger is invalid") from exc
    if integrity != [("ok",)] or frozenset(schemas) not in _ACCEPTED_LEDGER_TABLES or any(
        schemas[table] != _LEDGER_ADMISSION_POLICY.expected_schemas[table]
        for table in schemas
    ):
        raise SpecValidationError("private publication ledger is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _verify_preflight_identity(
    path: Path,
    *,
    directory_fd: int,
    database_fd: int,
    expected: os.stat_result,
    parent_entries: frozenset[str],
    atime_must_match: bool = True,
) -> None:
    opened = os.fstat(database_fd)
    current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    identity = _stat_identity if atime_must_match else _stat_identity_without_atime
    if (
        identity(opened) != identity(expected)
        or identity(current) != identity(expected)
        or frozenset(item.name for item in path.parent.iterdir()) != parent_entries
    ):
        raise SpecValidationError("publication ledger changed during schema preflight")


def _stat_identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_atime_ns,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _stat_identity_without_atime(item: os.stat_result) -> tuple[int, ...]:
    identity = _stat_identity(item)
    return (*identity[:7], *identity[8:])


def _remove_new_empty_ledger(
    path: Path,
    *,
    directory_fd: int,
    database_fd: int,
    expected: os.stat_result,
) -> None:
    """Remove only the exact newly-created untouched inode after pre-init failure."""
    try:
        opened = os.fstat(database_fd)
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    identity = (expected.st_dev, expected.st_ino)
    if (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and (opened.st_dev, opened.st_ino) == identity
        and (current.st_dev, current.st_ino) == identity
        and opened.st_uid == os.getuid()
        and current.st_uid == os.getuid()
        and stat.S_IMODE(opened.st_mode) == 0o600
        and stat.S_IMODE(current.st_mode) == 0o600
        and opened.st_size == 0
        and current.st_size == 0
    ):
        os.unlink(path.name, dir_fd=directory_fd)


def _initialize_ledger_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_LEDGER_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(certificate_publications)")
    }
    if columns != {
        "bundle_id",
        "certificate_sha256",
        "attempt_id",
        "state",
        "created_at",
        "updated_at",
    }:
        raise SpecValidationError("publication ledger schema is incompatible")
    legacy = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'used_certificates'"
    ).fetchone()
    if legacy is not None:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT OR IGNORE INTO certificate_publications "
            "(bundle_id, certificate_sha256, attempt_id, state, created_at, updated_at) "
            "SELECT bundle_id, certificate_sha256, 'legacy-migrated', 'published', ?, ? "
            "FROM used_certificates",
            (now, now),
        )


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            item_stat = current.lstat()
        except FileNotFoundError as exc:
            raise SpecValidationError("publication ledger parent must already exist") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise SpecValidationError("publication ledger path must not contain symlinks")


def _process_file_descriptors() -> set[int]:
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        raise SpecValidationError(
            "durable publication ledger requires /proc file descriptor inspection"
        )
    names = [item.name for item in proc_fds.iterdir() if item.name.isdigit()]
    descriptors: set[int] = set()
    for name in names:
        try:
            os.stat(proc_fds / name)
        except FileNotFoundError:
            continue
        descriptors.add(int(name))
    return descriptors


def _snapshot_file_descriptors() -> dict[int, tuple[int, int, str]]:
    snapshot: dict[int, tuple[int, int, str]] = {}
    for fd in _process_file_descriptors():
        try:
            item = os.stat(f"/proc/self/fd/{fd}")
            target = os.readlink(f"/proc/self/fd/{fd}")
        except FileNotFoundError:
            continue
        snapshot[fd] = (item.st_dev, item.st_ino, target)
    return snapshot


def _validate_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            item_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_uid != os.getuid()
            or stat.S_IMODE(item_stat.st_mode) != 0o600
            or item_stat.st_nlink != 1
        ):
            raise SpecValidationError(
                "publication ledger sidecar must be an owned 0600 regular file; "
                "untrusted sidecars are never modified"
            )


def _recompute_platform_success(report: Mapping[str, Any]) -> None:
    package = _mapping(report, "package")
    measurement = _mapping(report, "measurement")
    runs = measurement.get("runs")
    results = measurement.get("result_sha256")
    extension_equal = (
        _is_sha256(package.get("native_extension_sha256"))
        and package.get("native_extension_sha256") == package.get("installed_extension_sha256")
    )
    runs_complete = bool(
        isinstance(runs, list)
        and len(runs) >= 3
        and all(
            isinstance(run, dict)
            and run.get("complete") is True
            and run.get("exit_code") == 0
            and run.get("timed_out") is False
            and _is_sha256(run.get("result_sha256"))
            for run in runs
        )
    )
    run_results = (
        {run["result_sha256"] for run in runs}
        if isinstance(runs, list) and runs_complete
        else set()
    )
    deterministic = bool(
        isinstance(results, list)
        and len(results) == 1
        and _is_sha256(results[0])
        and runs_complete
        and run_results == {results[0]}
    )
    if report.get("complete") is not True or not deterministic:
        raise SpecValidationError(
            "platform report explicit or recomputed complete verdict is false"
        )
    if package.get("installed_extension_equal") is not True or not extension_equal:
        raise SpecValidationError(
            "platform report explicit or recomputed installed extension equality is false"
        )


def _verify_evidence_projection(
    document: Mapping[str, Any],
    reports: list[dict[str, Any]],
    commit: str,
    *,
    required_platform_systems: frozenset[str],
) -> None:
    systems = {report["platform"]["system"] for report in reports}
    projections = document.get("platforms")
    if systems != required_platform_systems or not isinstance(projections, list):
        raise SpecValidationError("platform provenance systems are incomplete")
    report_by_system = {report["platform"]["system"]: report for report in reports}
    for item in projections:
        if not isinstance(item, dict):
            raise SpecValidationError("platform evidence projection is malformed")
        report = report_by_system.get(item.get("system"))
        if report is None or any(
            item.get(key) != report[source].get(source_key)
            for key, source, source_key in (
                ("machine", "platform", "machine"),
                ("wheel_sha256", "package", "wheel_sha256"),
                ("native_extension_sha256", "package", "native_extension_sha256"),
                ("measured_repetitions", "measurement", "measured_repetitions"),
            )
        ):
            raise SpecValidationError("platform evidence projection differs from signed reports")
    first = reports[0]
    result_hashes = {
        value for report in reports for value in report["measurement"]["result_sha256"]
    }
    if (
        document.get("release_certified") is not True
        or document.get("candidate_commit") != commit
        or document.get("lane") != first.get("lane")
        or document.get("mode_contract") != first["workload"].get("mode_contract")
        or document.get("workload_identity_sha256")
        != first["workload"].get("identity_sha256")
        or document.get("workload") != first.get("workload")
        or document.get("package_version") != first["package"].get("version")
        or document.get("portable_package_sha256")
        != first["package"].get("portable_package_sha256")
        or len(result_hashes) != 1
        or document.get("result_sha256") != next(iter(result_hashes))
    ):
        raise SpecValidationError("platform evidence recomputed fields differ from signed reports")


def _decode_dsse(envelope: Any) -> tuple[bytes, dict[str, str]]:
    if not isinstance(envelope, dict) or set(envelope) != {
        "payloadType", "payload", "signatures"
    }:
        raise SpecValidationError("malformed DSSE provenance envelope")
    signatures = envelope["signatures"]
    if envelope["payloadType"] != DSSE_PAYLOAD_TYPE or not isinstance(signatures, list):
        raise SpecValidationError("unsupported DSSE provenance envelope")
    if len(signatures) != 1 or not isinstance(signatures[0], dict) or set(signatures[0]) != {
        "keyid", "sig"
    }:
        raise SpecValidationError("DSSE provenance requires exactly one signature")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
    except (TypeError, ValueError) as exc:
        raise SpecValidationError("DSSE provenance payload is malformed") from exc
    if not all(isinstance(signatures[0][key], str) for key in ("keyid", "sig")):
        raise SpecValidationError("DSSE provenance signature record is malformed")
    return payload, signatures[0]


def _validate_statement(statement: Any) -> None:
    required = {
        "schema_version", "signature_algorithm", "subject", "producer", "runner",
        "bundle", "candidate",
        "workload_identity_sha256", "issued_at", "expires_at",
    }
    if not isinstance(statement, dict) or set(statement) != required:
        raise SpecValidationError("malformed provenance statement")
    subject = _mapping(statement, "subject")
    producer = _mapping(statement, "producer")
    runner = _mapping(statement, "runner")
    bundle = _mapping(statement, "bundle")
    candidate = _mapping(statement, "candidate")
    if (
        statement["schema_version"] != PROVENANCE_ENVELOPE_VERSION
        or statement["signature_algorithm"] != "Ed25519"
        or set(subject) != {"name", "sha256", "document_sha256"}
        or not isinstance(subject["name"], str)
        or not _is_sha256(subject["sha256"])
        or not _is_sha256(subject["document_sha256"])
        or set(producer) != {
            "repository", "repository_ref", "workflow", "workflow_ref", "job",
            "commit", "run_id", "run_attempt",
        }
        or not all(
            isinstance(producer[key], str) and producer[key]
            for key in ("repository", "repository_ref", "workflow", "workflow_ref", "job")
        )
        or not _is_commit(producer["commit"])
        or not isinstance(producer["run_id"], str)
        or not producer["run_id"].isdigit()
        or not isinstance(producer["run_attempt"], int)
        or producer["run_attempt"] < 1
        or set(runner) != {"os", "machine"}
        or runner["os"] not in {"windows", "linux", "darwin"}
        or not isinstance(runner["machine"], str)
        or set(bundle) != {"bundle_id", "candidate_id", "challenge", "nonce", "attestation_id"}
        or not all(_is_sha256(bundle[key]) for key in bundle)
        or set(candidate) != {
            "wheel_sha256", "native_extension_sha256", "installed_extension_sha256",
            "portable_package_sha256",
        }
        or not all(_is_sha256(candidate[key]) for key in candidate)
        or not _is_sha256(statement["workload_identity_sha256"])
        or not isinstance(statement["issued_at"], str)
        or not isinstance(statement["expires_at"], str)
    ):
        raise SpecValidationError("malformed provenance statement")
    claimed_id = bundle["attestation_id"]
    bundle_without_id = {key: value for key, value in bundle.items() if key != "attestation_id"}
    recomputed = dict(statement)
    recomputed["bundle"] = bundle_without_id
    if claimed_id != canonical_sha256(recomputed):
        raise SpecValidationError("provenance attestation identity differs")
    _parse_timestamp(statement["issued_at"])
    _parse_timestamp(statement["expires_at"])


def _dsse_pae(payload_type: str, payload: bytes) -> bytes:
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(type_bytes), type_bytes, len(payload), payload)


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SpecValidationError("provenance contains noncanonical JSON") from exc


def _mapping(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise SpecValidationError(f"provenance {key} must be an object")
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SpecValidationError("provenance timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SpecValidationError("provenance timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_commit(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )
