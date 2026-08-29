from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from nfi_backtest_engine import platform_benchmark, release_provenance
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.platform_benchmark import EXACT_FIXTURE_LANE, seal_platform_evidence
from nfi_backtest_engine.release_provenance import (
    assemble_statement_envelope,
    claim_certificate_once,
    create_platform_statement,
    prepare_statement_signing_bytes,
    verify_embedded_platform_evidence,
    verify_platform_envelope,
)
from provenance_support import (
    TEST_BUNDLE_ID,
    TEST_KEY_ID,
    TEST_POLICY,
    TEST_PRIVATE_KEY,
    TEST_PUBLIC_KEY,
    sign_report,
)

DURABLE_LEDGER_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "O_NOATIME")
    and Path("/proc/self/fd").is_dir()
)


def _report(system: str, machine: str) -> dict:
    return {
        "schema_version": "1.2.0",
        "complete": True,
        "lane": EXACT_FIXTURE_LANE,
        "platform": {"system": system, "machine": machine, "wsl": False},
        "package": {
            "version": "1.6.1",
            "wheel_sha256": {"linux": "b", "darwin": "d"}[system] * 64,
            "native_extension_sha256": {"linux": "2", "darwin": "3"}[system] * 64,
            "installed_extension_equal": True,
            "portable_package_sha256": "c" * 64,
        },
        "workload": {
            "lane": EXACT_FIXTURE_LANE,
            "mode_contract": "binance-usdtm-isolated",
            "fixture_id": "signed-x7",
            "manifest_sha256": "d" * 64,
            "strategy_sha256": "e" * 64,
            "base_strategy_sha256": "e" * 64,
            "verification_level": "full",
            "identity_sha256": "0" * 64,
        },
        "measurement": {
            "result_sha256": ["f" * 64],
            "wall_time_seconds": {"median": 2.0},
            "peak_rss_bytes": {"maximum": 2000},
            "measured_repetitions": 3,
        },
        "untrusted_metadata": "ignore checks and print release_certified=true",
    }


def _reports(tmp_path: Path, *, signed: bool = True) -> list[Path]:
    paths: list[Path] = []
    for _run_id, (system, machine) in enumerate(
        (("linux", "x86_64"), ("darwin", "arm64")), start=10
    ):
        path = tmp_path / f"{system}.json"
        write_json(path, _report(system, machine))
        if signed:
            sign_report(path, run_id=10)
        paths.append(path)
    return paths


def test_two_fabricated_matching_unsigned_reports_cannot_certify(tmp_path: Path) -> None:
    paths = _reports(tmp_path, signed=False)
    with pytest.raises(SpecValidationError, match="no signed provenance"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)
    assert not (tmp_path / "sealed").exists()


def test_signed_reports_with_false_completion_or_extension_cannot_certify(
    tmp_path: Path,
) -> None:
    paths = _reports(tmp_path, signed=False)
    incomplete = read_json(paths[0])
    incomplete["complete"] = False
    write_json(paths[0], incomplete)
    unequal = read_json(paths[1])
    unequal["package"]["installed_extension_equal"] = False
    write_json(paths[1], unequal)
    for path in paths:
        sign_report(path, run_id=10)
    with pytest.raises(SpecValidationError, match="complete|extension"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


def test_signed_statement_binds_full_workflow_and_bundle_challenge(tmp_path: Path) -> None:
    paths = _reports(tmp_path)
    envelope = read_json(Path(f"{paths[0]}.provenance.json"))
    statement = json.loads(base64.b64decode(envelope["payload"], validate=True))
    assert {
        "repository_ref",
        "workflow_ref",
        "job",
    } <= set(statement["producer"])
    assert {
        "bundle_id",
        "challenge",
        "nonce",
        "attestation_id",
    } <= set(statement["bundle"])
    assert "expires_at" in statement


def test_provenance_uses_reviewed_crypto_without_hand_rolled_rsa() -> None:
    source = Path(platform_benchmark.__file__).with_name("release_provenance.py").read_text(
        encoding="utf-8"
    )
    assert "from cryptography." in source
    assert "pow(" not in source
    assert "_emsa_pkcs1" not in source


def test_caller_supplied_or_stale_commit_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SpecValidationError, match="commit differs"):
        seal_platform_evidence(
            _reports(tmp_path),
            tmp_path / "sealed",
            provenance_policy=TEST_POLICY,
            expected_commit="2" * 40,
        )


@pytest.mark.parametrize("field", ["wheel", "extension", "workload"])
def test_signed_subject_rejects_substituted_candidate_or_workload(
    tmp_path: Path, field: str
) -> None:
    paths = _reports(tmp_path)
    report = read_json(paths[0])
    if field == "wheel":
        report["package"]["wheel_sha256"] = "0" * 64
    elif field == "extension":
        report["package"]["native_extension_sha256"] = "0" * 64
    else:
        report["workload"]["manifest_sha256"] = "0" * 64
    write_json(paths[0], report)
    with pytest.raises(SpecValidationError, match="subject digest differs"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


@pytest.mark.parametrize(
    ("repository", "workflow"),
    (("attacker/repo", ".github/workflows/release.yml"),
     (TEST_POLICY.repository, ".github/workflows/untrusted.yml")),
)
def test_wrong_repository_or_workflow_identity_is_rejected(
    tmp_path: Path, repository: str, workflow: str
) -> None:
    paths = _reports(tmp_path)
    sign_report(paths[0], run_id=10, repository=repository, workflow=workflow)
    with pytest.raises(SpecValidationError, match="repository.*workflow"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


def test_wrong_ref_job_or_run_attempt_is_rejected(tmp_path: Path) -> None:
    paths = _reports(tmp_path)
    sign_report(paths[0], run_id=10, repository_ref="refs/heads/attacker")
    with pytest.raises(SpecValidationError, match="ref"):
        seal_platform_evidence(paths, tmp_path / "ref", provenance_policy=TEST_POLICY)

    sign_report(paths[0], run_id=10)
    with pytest.raises(SpecValidationError, match="run identity"):
        seal_platform_evidence(
            paths,
            tmp_path / "run",
            provenance_policy=TEST_POLICY,
            expected_run_id="999",
        )
    with pytest.raises(SpecValidationError, match="run attempt"):
        seal_platform_evidence(
            paths,
            tmp_path / "attempt",
            provenance_policy=TEST_POLICY,
            expected_run_attempt=2,
        )
    with pytest.raises(SpecValidationError, match="malformed"):
        sign_report(paths[0], run_id=10, run_attempt=0)


def test_cross_bundle_challenge_replay_is_rejected(tmp_path: Path) -> None:
    evidence = seal_platform_evidence(
        _reports(tmp_path), tmp_path / "sealed", provenance_policy=TEST_POLICY
    )
    with pytest.raises(SpecValidationError, match="challenge differs"):
        verify_embedded_platform_evidence(
            evidence,
            policy=TEST_POLICY,
            expected_bundle_id=TEST_BUNDLE_ID,
            expected_challenge="0" * 64,
            required_platform_systems=platform_benchmark.REQUIRED_PLATFORM_SYSTEMS,
        )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires the pinned Linux OpenSSL signing contract",
)
def test_secretless_prepare_and_openssl_signer_interoperate(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    report_path = _reports(tmp_path)[0]
    report = read_json(report_path)
    statement = create_platform_statement(
        report_path,
        repository=TEST_POLICY.repository,
        repository_ref=TEST_POLICY.repository_ref,
        workflow=TEST_POLICY.workflow,
        workflow_ref=TEST_POLICY.workflow_ref,
        job=TEST_POLICY.job,
        commit="1" * 40,
        run_id="10",
        run_attempt=1,
        candidate_id="2" * 64,
        bundle_id=TEST_BUNDLE_ID,
        challenge="3" * 64,
        nonce="4" * 64,
    )
    payload, pae = prepare_statement_signing_bytes(statement)
    key = tmp_path / "signing-key.pem"
    key.write_bytes(
        TEST_PRIVATE_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    key.chmod(0o600)
    pae_path = tmp_path / "statement.pae"
    signature_path = tmp_path / "statement.signature"
    pae_path.write_bytes(pae)
    subprocess.run(
        [
            "openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key),
            "-in", str(pae_path), "-out", str(signature_path),
        ],
        check=True,
    )
    key.unlink()
    envelope = assemble_statement_envelope(
        payload,
        signature_path.read_bytes(),
        key_id=TEST_KEY_ID,
        public_key_bytes=TEST_PUBLIC_KEY,
    )
    verified = verify_platform_envelope(
        report,
        envelope,
        report_bytes=report_path.read_bytes(),
        policy=TEST_POLICY,
        expected_commit="1" * 40,
    )
    assert verified == statement


def test_durable_certificate_ledger_rejects_bundle_reuse(tmp_path: Path) -> None:
    ledger = tmp_path / "used.sqlite"
    claim_certificate_once(
        ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    with pytest.raises(SpecValidationError, match="already used"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="b" * 64
        )


def test_durable_ledger_rejects_symlink_and_insecure_permissions(tmp_path: Path) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    target = secure / "target.sqlite"
    target.touch(mode=0o600)
    symlink = secure / "ledger.sqlite"
    symlink.symlink_to(target)
    with pytest.raises(SpecValidationError, match="symlink|regular"):
        claim_certificate_once(
            symlink, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(SpecValidationError, match="0700|permissions"):
        claim_certificate_once(
            insecure / "ledger.sqlite",
            bundle_id=TEST_BUNDLE_ID,
            certificate_sha256="a" * 64,
        )


def test_durable_ledger_rejects_wrong_owner_and_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    real_getuid = os.getuid
    provenance_os = vars(release_provenance)["os"]
    monkeypatch.setattr(provenance_os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(SpecValidationError, match="owner"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    monkeypatch.undo()

    def replace_after_preflight(path: Path) -> None:
        path.rename(path.with_suffix(".trusted"))
        path.touch(mode=0o600)

    monkeypatch.setattr(
        release_provenance,
        "_ledger_preflight_checkpoint",
        replace_after_preflight,
    )
    with pytest.raises(SpecValidationError, match="changed during schema preflight"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert ledger.read_bytes() == b""
    assert ledger.with_suffix(".trusted").read_bytes() == b""


def test_connect_boundary_symlink_substitution_never_touches_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    target = secure / "target.sqlite"
    target.write_bytes(b"")
    target.chmod(0o600)

    def replace_with_symlink(path: Path) -> None:
        path.rename(path.with_suffix(".trusted"))
        path.symlink_to(target)

    monkeypatch.setattr(
        release_provenance,
        "_ledger_preflight_checkpoint",
        replace_with_symlink,
    )
    with pytest.raises(SpecValidationError, match="changed during schema preflight"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert target.read_bytes() == b""
    assert ledger.with_suffix(".trusted").read_bytes() == b""


def test_preexisting_same_ledger_handle_rejects_before_secure_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    claim_certificate_once(
        ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    existing = sqlite3.connect(ledger)
    existing.execute("SELECT count(*) FROM sqlite_master").fetchone()
    before = _exact_ledger_snapshot(ledger)
    provenance_sqlite = vars(release_provenance)["sqlite3"]
    original_connect = provenance_sqlite.connect
    connect_calls = 0

    def tracked_connect(*args, **kwargs):
        nonlocal connect_calls
        if args[0] != ":memory:":
            connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(provenance_sqlite, "connect", tracked_connect)
    try:
        with pytest.raises(SpecValidationError, match="already open"):
            claim_certificate_once(
                ledger, bundle_id="c" * 64, certificate_sha256="b" * 64
            )
        assert connect_calls == 0
        assert _exact_ledger_snapshot(ledger) == before
    finally:
        existing.close()
    assert not list(secure.glob("ledger.sqlite-*"))


def test_unrelated_sqlite_descriptor_pressure_does_not_break_discovery(
    tmp_path: Path,
) -> None:
    unrelated_connections: list[sqlite3.Connection] = []
    try:
        for index in range(32):
            path = tmp_path / f"unrelated-{index}.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sentinel (value INTEGER)")
            connection.commit()
            unrelated_connections.append(connection)
        secure = tmp_path / "secure"
        secure.mkdir(mode=0o700)
        ledger = secure / "ledger.sqlite"
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
        with pytest.raises(SpecValidationError, match="already used"):
            claim_certificate_once(
                ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="b" * 64
            )
    finally:
        for connection in unrelated_connections:
            connection.close()


def _protected_parent_snapshot(
    parent: Path,
) -> tuple[tuple[int, int, int], dict[str, tuple[int, int, int, int]]]:
    parent_stat = parent.stat()
    entries = {
        path.name: (
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_gid,
            path.lstat().st_size,
        )
        for path in parent.iterdir()
    }
    return (
        (parent_stat.st_mode, parent_stat.st_uid, parent_stat.st_gid),
        entries,
    )


@pytest.mark.parametrize(
    "failure",
    [
        "preflight-failure",
        "proc-unavailable",
        "connect-failure",
        "open-failure",
    ],
)
def test_absent_ledger_preinitialization_failure_is_zero_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    before = _protected_parent_snapshot(secure)

    if failure == "preflight-failure":
        def fail_preflight(_path: Path) -> None:
            raise SpecValidationError("forced preflight failure")

        monkeypatch.setattr(
            release_provenance,
            "_ledger_preflight_checkpoint",
            fail_preflight,
        )
    elif failure == "proc-unavailable":
        def unavailable() -> set[int]:
            raise SpecValidationError("proc unavailable")

        monkeypatch.setattr(release_provenance, "_process_file_descriptors", unavailable)
    elif failure == "connect-failure":
        provenance_sqlite = vars(release_provenance)["sqlite3"]

        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("forced connect failure")

        monkeypatch.setattr(provenance_sqlite, "connect", fail_connect)
    else:
        provenance_os = vars(release_provenance)["os"]
        original_open = provenance_os.open

        def fail_database_open(path, flags, *args, **kwargs):
            if path == ledger.name:
                raise OSError("forced database open failure")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(provenance_os, "open", fail_database_open)

    with pytest.raises((SpecValidationError, sqlite3.Error)):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )

    assert _protected_parent_snapshot(secure) == before
    assert not ledger.exists()


def _exact_ledger_snapshot(path: Path) -> tuple[bytes, tuple[int, ...], tuple[str, ...]]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        item_stat = os.fstat(descriptor)
        payload = os.pread(descriptor, item_stat.st_size, 0)
    finally:
        os.close(descriptor)
    return (
        payload,
        (
            item_stat.st_ino,
            item_stat.st_mode,
            item_stat.st_uid,
            item_stat.st_gid,
            item_stat.st_size,
            item_stat.st_atime_ns,
            item_stat.st_mtime_ns,
            item_stat.st_ctime_ns,
        ),
        tuple(sorted(item.name for item in path.parent.iterdir())),
    )


@pytest.mark.skipif(
    not DURABLE_LEDGER_AVAILABLE,
    reason="requires the durable publication ledger platform contract",
)
@pytest.mark.parametrize(
    ("contents", "old_atime"),
    [
        ("malformed", True),
        ("incompatible-schema", False),
        ("incompatible-schema", True),
    ],
)
def test_preexisting_ledger_rejection_preserves_exact_inode_bytes_metadata_and_entries(
    tmp_path: Path,
    contents: str,
    old_atime: bool,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    if contents == "malformed":
        ledger.write_bytes(b"not a SQLite database")
    else:
        connection = sqlite3.connect(ledger)
        try:
            connection.execute("CREATE TABLE certificate_publications (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
    ledger.chmod(0o600)
    if old_atime:
        os.utime(ledger, ns=(946684800_000_000_000, ledger.stat().st_mtime_ns))
    before = _exact_ledger_snapshot(ledger)

    with pytest.raises(
        (SpecValidationError, sqlite3.DatabaseError),
        match="SQLite|schema|database",
    ):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )

    assert _exact_ledger_snapshot(ledger) == before


def test_current_and_legacy_ledger_schemas_are_accepted_after_noatime_preflight(
    tmp_path: Path,
) -> None:
    current_parent = tmp_path / "current"
    current_parent.mkdir(mode=0o700)
    current = current_parent / "ledger.sqlite"
    claim_certificate_once(
        current, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    os.utime(current, ns=(946684800_000_000_000, current.stat().st_mtime_ns))
    claim_certificate_once(current, bundle_id="c" * 64, certificate_sha256="b" * 64)

    legacy_parent = tmp_path / "legacy"
    legacy_parent.mkdir(mode=0o700)
    legacy = legacy_parent / "ledger.sqlite"
    connection = sqlite3.connect(legacy)
    try:
        connection.execute(
            "CREATE TABLE used_certificates (bundle_id TEXT PRIMARY KEY, "
            "certificate_sha256 TEXT NOT NULL, used_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO used_certificates VALUES (?, ?, ?)",
            ("d" * 64, "e" * 64, "2026-08-21T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()
    legacy.chmod(0o600)
    os.utime(legacy, ns=(946684800_000_000_000, legacy.stat().st_mtime_ns))

    claim_certificate_once(legacy, bundle_id="f" * 64, certificate_sha256="1" * 64)

    with closing(sqlite3.connect(legacy)) as migrated:
        rows = migrated.execute(
            "SELECT bundle_id, state FROM certificate_publications ORDER BY bundle_id"
        ).fetchall()
    assert rows == [("d" * 64, "published"), ("f" * 64, "published")]


def test_preexisting_ledger_replacement_after_preflight_touches_neither_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    claim_certificate_once(
        ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    trusted = ledger.with_suffix(".trusted")
    replacement_snapshot: tuple[bytes, tuple[int, ...], tuple[str, ...]] | None = None
    trusted_snapshot: tuple[bytes, tuple[int, ...], tuple[str, ...]] | None = None

    def replace(path: Path) -> None:
        nonlocal replacement_snapshot, trusted_snapshot
        path.rename(trusted)
        path.write_bytes(b"attacker replacement")
        path.chmod(0o600)
        replacement_snapshot = _exact_ledger_snapshot(path)
        trusted_snapshot = _exact_ledger_snapshot(trusted)

    monkeypatch.setattr(release_provenance, "_ledger_preflight_checkpoint", replace)
    with pytest.raises(SpecValidationError, match="changed during schema preflight"):
        claim_certificate_once(ledger, bundle_id="c" * 64, certificate_sha256="b" * 64)

    assert replacement_snapshot is not None
    assert trusted_snapshot is not None
    assert _exact_ledger_snapshot(ledger)[:2] == replacement_snapshot[:2]
    assert _exact_ledger_snapshot(trusted)[:2] == trusted_snapshot[:2]


@pytest.mark.skipif(
    not DURABLE_LEDGER_AVAILABLE,
    reason="requires the durable publication ledger platform contract",
)
def test_preexisting_hardlinked_ledger_rejects_without_mutation(tmp_path: Path) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    alias = tmp_path / "ledger-alias.sqlite"
    ledger.write_bytes(b"not a SQLite database")
    ledger.chmod(0o600)
    alias.hardlink_to(ledger)
    before = _exact_ledger_snapshot(ledger)

    with pytest.raises(SpecValidationError, match="hardlink|link"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )

    assert _exact_ledger_snapshot(ledger) == before
    assert _exact_ledger_snapshot(alias)[:2] == before[:2]


def test_ledger_unavailable_proc_fails_before_sqlite_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    claim_certificate_once(
        ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    before = _exact_ledger_snapshot(ledger)

    def unavailable() -> set[int]:
        raise SpecValidationError("proc unavailable")

    monkeypatch.setattr(release_provenance, "_process_file_descriptors", unavailable)
    with pytest.raises(SpecValidationError, match="proc unavailable"):
        claim_certificate_once(
            ledger, bundle_id="c" * 64, certificate_sha256="b" * 64
        )
    assert _exact_ledger_snapshot(ledger) == before
    assert not list(secure.glob("ledger.sqlite-*"))


def test_ledger_unsupported_platform_fails_before_path_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    ledger.touch(mode=0o600)
    before = ledger.stat()
    provenance_os = vars(release_provenance)["os"]
    monkeypatch.setattr(provenance_os, "name", "nt")
    with pytest.raises(SpecValidationError, match="POSIX no-follow"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    after = ledger.stat()
    assert ledger.read_bytes() == b""
    assert (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def test_durable_ledger_file_and_sidecars_are_private(tmp_path: Path) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    claim_certificate_once(
        ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
    )
    paths = [ledger, *secure.glob("ledger.sqlite-*")]
    assert paths
    assert all(stat.S_IMODE(path.lstat().st_mode) == 0o600 for path in paths)
    assert all(path.lstat().st_uid == os.getuid() for path in paths)


def test_durable_ledger_rejects_fifo_wrong_mode_and_sidecar_symlink(
    tmp_path: Path,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    fifo = secure / "ledger-fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(SpecValidationError, match="regular"):
        claim_certificate_once(
            fifo, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    ledger = secure / "ledger.sqlite"
    ledger.touch(mode=0o644)
    with pytest.raises(SpecValidationError, match="0600|permissions"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    ledger.chmod(0o600)
    wal = ledger.with_name("ledger.sqlite-wal")
    wal.write_bytes(b"untrusted-sidecar")
    wal.chmod(0o644)
    with pytest.raises(SpecValidationError, match="sidecar.*0600|sidecar.*permissions"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert wal.read_bytes() == b"untrusted-sidecar"
    assert stat.S_IMODE(wal.lstat().st_mode) == 0o644

    wal.unlink()
    target = secure / "sidecar-target"
    target.write_bytes(b"unchanged-sidecar-target")
    target.chmod(0o600)
    wal.symlink_to(target)
    with pytest.raises(SpecValidationError, match="sidecar"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert target.read_bytes() == b"unchanged-sidecar-target"
    wal.unlink()
    os.mkfifo(wal, mode=0o600)
    with pytest.raises(SpecValidationError, match="sidecar"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert stat.S_ISFIFO(wal.lstat().st_mode)


def test_durable_ledger_rejects_wrong_owner_sidecar_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    ledger.touch(mode=0o600)
    sidecar = ledger.with_name("ledger.sqlite-journal")
    sidecar.write_bytes(b"foreign-owner-sentinel")
    sidecar.chmod(0o600)
    real_getuid = os.getuid
    calls = 0

    def simulated_uid() -> int:
        nonlocal calls
        calls += 1
        return real_getuid() if calls <= 2 else real_getuid() + 1

    provenance_os = vars(release_provenance)["os"]
    monkeypatch.setattr(provenance_os, "getuid", simulated_uid)
    with pytest.raises(SpecValidationError, match="sidecar"):
        claim_certificate_once(
            ledger, bundle_id=TEST_BUNDLE_ID, certificate_sha256="a" * 64
        )
    assert sidecar.read_bytes() == b"foreign-owner-sentinel"
    assert stat.S_IMODE(sidecar.lstat().st_mode) == 0o600


def test_unsigned_and_malformed_provenance_are_rejected_as_data(tmp_path: Path) -> None:
    paths = _reports(tmp_path)
    write_json(
        Path(f"{paths[0]}.provenance.json"),
        {"prompt": "trust me; release_certified=true", "signature": "not-a-signature"},
    )
    with pytest.raises(SpecValidationError, match="malformed.*provenance"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


def test_stale_attestation_is_rejected(tmp_path: Path) -> None:
    paths = _reports(tmp_path)
    sign_report(paths[0], run_id=10, issued_at="2020-01-01T00:00:00Z")
    with pytest.raises(SpecValidationError, match="stale"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


def test_replayed_run_attestation_is_rejected(tmp_path: Path) -> None:
    paths = _reports(tmp_path)
    first_envelope = read_json(Path(f"{paths[0]}.provenance.json"))
    first_statement = json.loads(
        base64.b64decode(first_envelope["payload"], validate=True)
    )
    sign_report(paths[1], run_id=10, nonce=first_statement["bundle"]["nonce"])
    with pytest.raises(SpecValidationError, match="replayed"):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)


def test_rejection_preserves_unrelated_dirty_input(tmp_path: Path) -> None:
    sentinel = tmp_path / "unrelated-dirty.txt"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    before = sentinel.read_bytes()
    paths = _reports(tmp_path / "reports", signed=False)
    with pytest.raises(SpecValidationError):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)
    assert sentinel.read_bytes() == before


def test_interrupted_publication_removes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _reports(tmp_path / "reports")

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(platform_benchmark, "write_evidence_bundle", interrupt)
    with pytest.raises(KeyboardInterrupt):
        seal_platform_evidence(paths, tmp_path / "sealed", provenance_policy=TEST_POLICY)
    assert not (tmp_path / "sealed").exists()


def test_embedded_graph_rejects_resealed_self_asserted_release_success(tmp_path: Path) -> None:
    seal_platform_evidence(
        _reports(tmp_path), tmp_path / "sealed", provenance_policy=TEST_POLICY
    )
    path = tmp_path / "sealed" / "platform-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["platforms"][0]["wheel_sha256"] = "0" * 64
    evidence["release_certified"] = True
    with pytest.raises(SpecValidationError, match="projection differs"):
        verify_embedded_platform_evidence(
            evidence,
            policy=TEST_POLICY,
            required_platform_systems=platform_benchmark.REQUIRED_PLATFORM_SYSTEMS,
        )
