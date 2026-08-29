from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_provenance import claim_certificate_once

BUNDLE_ID = "a" * 64
CERTIFICATE_SHA256 = "b" * 64
OLD_ATIME_NS = 946_684_800_000_000_000


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    payload: bytes
    identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    files: dict[str, FileSnapshot]
    parent_entries: tuple[str, ...]


def _file_snapshot(path: Path) -> FileSnapshot:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        item = os.fstat(descriptor)
        payload = os.pread(descriptor, item.st_size, 0)
    finally:
        os.close(descriptor)
    return FileSnapshot(
        payload=payload,
        identity=(
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
        ),
    )


def _ledger_snapshot(path: Path) -> LedgerSnapshot:
    return LedgerSnapshot(
        files={
            item.name: _file_snapshot(item)
            for item in path.parent.iterdir()
            if item.is_file() and (item == path or item.name.startswith(f"{path.name}-"))
        },
        parent_entries=tuple(sorted(item.name for item in path.parent.iterdir())),
    )


def _freeze_wal_transaction(
    path: Path,
    statement: str,
    parameters: tuple[str, ...] = (),
) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(statement, parameters)
    connection.commit()
    paths = (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm"))
    payloads = {item: item.read_bytes() for item in paths}
    connection.close()
    for item, payload in payloads.items():
        item.write_bytes(payload)
        item.chmod(0o600)


def _current_wal_ledger(path: Path, statement: str) -> None:
    claim_certificate_once(
        path,
        bundle_id=BUNDLE_ID,
        certificate_sha256=CERTIFICATE_SHA256,
    )
    _freeze_wal_transaction(path, statement)


def test_incompatible_effective_wal_schema_rejects_without_triplet_mutation(
    tmp_path: Path,
) -> None:
    # Given: an exact current main database whose WAL adds an incompatible column.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "ALTER TABLE certificate_publications ADD COLUMN attacker_value TEXT",
    )
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))
    before = _ledger_snapshot(ledger)

    # When: the effective pre-existing SQLite state crosses the admission boundary.
    with pytest.raises(SpecValidationError, match="schema"):
        claim_certificate_once(
            ledger,
            bundle_id="c" * 64,
            certificate_sha256="d" * 64,
        )

    # Then: main, WAL, SHM, metadata, links, and parent entries are exactly unchanged.
    assert _ledger_snapshot(ledger) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_hardlinked_sqlite_sidecar_rejects_without_alias_mutation(
    tmp_path: Path,
    suffix: str,
) -> None:
    # Given: a valid WAL ledger with one aliased sidecar inode.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('c', 'd', 'wal-attempt', 'published', 'created', 'updated')",
    )
    sidecar = ledger.with_name(f"{ledger.name}{suffix}")
    alias = secure / f"alias{suffix}"
    alias.hardlink_to(sidecar)
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))
    before = _ledger_snapshot(ledger)

    # When: admission validates the complete pre-existing state.
    with pytest.raises(SpecValidationError, match="link|sidecar"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256="f" * 64,
        )

    # Then: both names and the aliased inode remain exact.
    assert _ledger_snapshot(ledger) == before
    assert stat.S_ISREG(alias.lstat().st_mode)


@pytest.mark.parametrize(
    "damage",
    ["wal-header", "wal-truncated", "shm-header", "shm-truncated", "swapped"],
)
def test_malformed_sidecar_bytes_reject_without_triplet_mutation(
    tmp_path: Path,
    damage: Literal[
        "wal-header", "wal-truncated", "shm-header", "shm-truncated", "swapped"
    ],
) -> None:
    # Given: a current ledger with one malformed, truncated, or swapped sidecar.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('wal-row', 'certificate', 'attempt', 'published', 'created', 'updated')",
    )
    wal = ledger.with_name(f"{ledger.name}-wal")
    shm = ledger.with_name(f"{ledger.name}-shm")
    match damage:
        case "wal-header":
            wal.write_bytes(b"bad!" + wal.read_bytes()[4:])
        case "wal-truncated":
            wal.write_bytes(wal.read_bytes()[:-1])
        case "shm-header":
            shm.write_bytes(b"bad!" + shm.read_bytes()[4:])
        case "shm-truncated":
            shm.write_bytes(shm.read_bytes()[:-1])
        case "swapped":
            wal_payload, shm_payload = wal.read_bytes(), shm.read_bytes()
            wal.write_bytes(shm_payload)
            shm.write_bytes(wal_payload)
        case unreachable:
            assert_never(unreachable)
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))
    before = _ledger_snapshot(ledger)

    # When: sidecar bytes are admitted.
    with pytest.raises(SpecValidationError, match="WAL|SHM|sidecar"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256="f" * 64,
        )

    # Then: rejection does not mutate any original inode or parent entry.
    assert _ledger_snapshot(ledger) == before


def test_extra_rollback_journal_rejects_without_mutation(tmp_path: Path) -> None:
    # Given: a valid WAL triplet plus an unexpected rollback journal.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('wal-row', 'certificate', 'attempt', 'published', 'created', 'updated')",
    )
    journal = ledger.with_name(f"{ledger.name}-journal")
    journal.write_bytes(b"unexpected-journal")
    journal.chmod(0o600)
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))
    before = _ledger_snapshot(ledger)

    # When: the extra effective-state entry is admitted.
    with pytest.raises(SpecValidationError, match="unsupported|sidecar"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256="f" * 64,
        )

    # Then: all files and metadata remain exact.
    assert _ledger_snapshot(ledger) == before


@pytest.mark.parametrize("schema", ["current", "legacy"])
def test_valid_effective_wal_state_is_replayed_before_claim(
    tmp_path: Path,
    schema: str,
) -> None:
    # Given: a valid current or legacy row committed only in WAL state.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    if schema == "current":
        claim_certificate_once(
            ledger,
            bundle_id=BUNDLE_ID,
            certificate_sha256=CERTIFICATE_SHA256,
        )
        statement = (
            "INSERT INTO certificate_publications VALUES "
            "('wal-row', 'wal-certificate', 'wal-attempt', 'published', 'created', 'updated')"
        )
    else:
        with closing(sqlite3.connect(ledger)) as connection:
            connection.execute(
                "CREATE TABLE used_certificates (bundle_id TEXT PRIMARY KEY, "
                "certificate_sha256 TEXT NOT NULL, used_at TEXT NOT NULL)"
            )
            connection.commit()
        ledger.chmod(0o600)
        statement = (
            "INSERT INTO used_certificates VALUES "
            "('wal-row', 'wal-certificate', 'used-at')"
        )
    _freeze_wal_transaction(ledger, statement)
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))

    # When: a new certificate is claimed after private effective-schema admission.
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256="f" * 64,
    )

    # Then: the WAL row is retained and legacy state is migrated as published.
    with closing(sqlite3.connect(ledger)) as connection:
        rows = connection.execute(
            "SELECT bundle_id, state FROM certificate_publications ORDER BY bundle_id"
        ).fetchall()
    assert ("wal-row", "published") in rows
    assert ("e" * 64, "published") in rows
