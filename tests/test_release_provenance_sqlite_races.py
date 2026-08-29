from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

import pytest
from nfi_backtest_engine import release_provenance
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_provenance import claim_certificate_once
from test_release_provenance_sqlite import (
    CERTIFICATE_SHA256,
    _current_wal_ledger,
    _file_snapshot,
)
from test_sqlite_wal_reset import reused_wal_snapshot


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    payload: bytes
    identity: tuple[int, ...]


def _parent_snapshot(parent: Path) -> dict[str, EntrySnapshot]:
    snapshot: dict[str, EntrySnapshot] = {}
    for path in parent.iterdir():
        payload = (
            _file_snapshot(path).payload
            if path.is_file() and not path.is_symlink()
            else os.readlink(path).encode()
            if path.is_symlink()
            else b""
        )
        item = path.lstat()
        snapshot[path.name] = EntrySnapshot(
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
    return snapshot


@pytest.mark.parametrize("replacement", ["regular", "symlink", "missing"])
def test_sidecar_replacement_after_preflight_never_crosses_original_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: Literal["regular", "symlink", "missing"],
) -> None:
    # Given: an admitted WAL inode replaced at the last pre-SQLite checkpoint.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('wal-row', 'certificate', 'attempt', 'published', 'created', 'updated')",
    )
    wal = ledger.with_name(f"{ledger.name}-wal")
    retained = secure / "retained-wal"
    target = secure / "symlink-target"
    checkpoint_snapshot: dict[str, EntrySnapshot] | None = None

    def replace_sidecar(_path: Path) -> None:
        nonlocal checkpoint_snapshot
        wal.rename(retained)
        match replacement:
            case "regular":
                wal.write_bytes(b"replacement")
                wal.chmod(0o600)
            case "symlink":
                target.write_bytes(b"target")
                target.chmod(0o600)
                wal.symlink_to(target)
            case "missing":
                pass
            case unreachable:
                assert_never(unreachable)
        checkpoint_snapshot = _parent_snapshot(secure)

    monkeypatch.setattr(
        release_provenance,
        "_ledger_preflight_checkpoint",
        replace_sidecar,
    )

    # When: complete-triplet identity is revalidated immediately before SQLite use.
    with pytest.raises(SpecValidationError, match="schema preflight|secure open"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: neither retained nor attacker-established entries are altered or restored.
    assert checkpoint_snapshot is not None
    assert _parent_snapshot(secure) == checkpoint_snapshot


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_original_triplet_substitution_immediately_before_private_connect_is_not_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
    suffix: Literal["", "-wal", "-shm"],
) -> None:
    # Given: an admitted reset/reuse state and a coherent same-byte pathname substitution.
    secure = tmp_path / f"secure-{page_size}-{suffix or 'main'}"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    reused_wal_snapshot(ledger, page_size)
    target = ledger.with_name(f"{ledger.name}{suffix}")
    original_connect = sqlite3.connect
    checkpoint_snapshot: dict[str, EntrySnapshot] | None = None

    def substitute_before_private_connect(database, *args, **kwargs):
        nonlocal checkpoint_snapshot
        database_path = Path(os.fspath(database))
        if database_path != ledger and checkpoint_snapshot is None:
            retained = secure / f"retained{suffix or '-main'}"
            target.rename(retained)
            target.write_bytes(retained.read_bytes())
            target.chmod(0o600)
            checkpoint_snapshot = _parent_snapshot(secure)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", substitute_before_private_connect)

    # When: trusted SQLite starts from the admitted effective-state copy.
    with pytest.raises(SpecValidationError, match="schema preflight"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: SQLite never opens the public name and the substituted generation is exact.
    assert checkpoint_snapshot is not None
    assert _parent_snapshot(secure) == checkpoint_snapshot


def test_sqlite_never_reopens_an_accepted_public_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a pre-existing admitted WAL ledger.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('wal-row', 'certificate', 'attempt', 'published', 'created', 'updated')",
    )
    original_connect = sqlite3.connect
    opened: list[Path] = []

    def track_connect(database, *args, **kwargs):
        database_path = Path(os.fspath(database))
        opened.append(database_path)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", track_connect)

    # When: a new durable claim is written.
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256=CERTIFICATE_SHA256,
    )

    # Then: all SQLite opens are transaction-private, never the public main or fd alias.
    assert opened
    assert ledger not in opened
    assert Path("/proc/self/fd") not in {path.parent for path in opened}
    with closing(original_connect(ledger)) as connection:
        assert connection.execute(
            "SELECT state FROM certificate_publications WHERE bundle_id = ?",
            ("e" * 64,),
        ).fetchone() == ("published",)


@pytest.mark.parametrize(
    "checkpoint",
    [
        "prepared",
        "old-wal-staged",
        "old-shm-staged",
        "old-main-staged",
        "new-main-installed",
    ],
)
def test_public_replacement_failure_restores_the_complete_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    # Given: an admitted WAL triplet and one deterministic publication interruption.
    secure = tmp_path / checkpoint
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _current_wal_ledger(
        ledger,
        "INSERT INTO certificate_publications VALUES "
        "('wal-row', 'certificate', 'attempt', 'published', 'created', 'updated')",
    )
    before = _parent_snapshot(secure)

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        release_provenance,
        "_ledger_publication_checkpoint",
        interrupt,
        raising=False,
    )

    # When: publication is interrupted before its durable commit point.
    with pytest.raises(KeyboardInterrupt):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: the old main/WAL/SHM bytes, inodes, metadata, and public names are restored.
    after = _parent_snapshot(secure)
    assert set(after) == set(before)
    for name, expected in before.items():
        assert after[name].payload == expected.payload
        assert after[name].identity[:-1] == expected.identity[:-1]
