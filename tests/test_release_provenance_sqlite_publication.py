from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal, assert_never

import pytest
from nfi_backtest_engine import release_provenance, sqlite_publication
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_provenance import claim_certificate_once
from test_release_provenance_sqlite import (
    CERTIFICATE_SHA256,
    _freeze_wal_transaction,
)
from test_release_provenance_sqlite_races import EntrySnapshot, _parent_snapshot
from test_sqlite_wal_reset import reused_wal_snapshot

State = Literal["current", "legacy", "reset-reuse"]
Boundary = Literal["private-connect", "private-sql"]


def _wal_state(path: Path, page_size: int, state: State) -> None:
    match state:
        case "current":
            claim_certificate_once(
                path,
                bundle_id="a" * 64,
                certificate_sha256="b" * 64,
            )
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute(f"PRAGMA page_size={page_size}")
                connection.execute("VACUUM")
            path.chmod(0o600)
            _freeze_wal_transaction(
                path,
                "INSERT INTO certificate_publications VALUES "
                "('old-current', 'certificate', 'attempt', 'published', 'created', 'updated')",
            )
        case "legacy":
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"PRAGMA page_size={page_size}")
                connection.execute(
                    "CREATE TABLE used_certificates (bundle_id TEXT PRIMARY KEY, "
                    "certificate_sha256 TEXT NOT NULL, used_at TEXT NOT NULL)"
                )
                connection.commit()
            path.chmod(0o600)
            _freeze_wal_transaction(
                path,
                "INSERT INTO used_certificates VALUES "
                "('old-legacy', 'certificate', 'used-at')",
            )
        case "reset-reuse":
            reused_wal_snapshot(path, page_size)
        case unreachable:
            assert_never(unreachable)


def _substitute(target: Path, retained: Path) -> None:
    target.rename(retained)
    target.write_bytes(retained.read_bytes())
    target.chmod(0o600)


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("state", ["current", "legacy", "reset-reuse"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("boundary", ["private-connect", "private-sql"])
def test_public_name_substitution_never_enters_private_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
    state: State,
    suffix: Literal["", "-wal", "-shm"],
    boundary: Boundary,
) -> None:
    # Given: a 4/64 KiB current, legacy, or reset/reuse triplet admitted by descriptor.
    secure = tmp_path / f"{page_size}-{state}-{suffix or 'main'}-{boundary}"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _wal_state(ledger, page_size, state)
    target = ledger.with_name(f"{ledger.name}{suffix}")
    original_connect = sqlite3.connect
    checkpoint_snapshot: dict[str, EntrySnapshot] | None = None

    def replace_public_name() -> None:
        nonlocal checkpoint_snapshot
        if checkpoint_snapshot is None:
            _substitute(target, secure / f"retained{suffix or '-main'}")
            checkpoint_snapshot = _parent_snapshot(secure)

    def connect(database, *args, **kwargs):
        if boundary == "private-connect" and Path(os.fspath(database)) != ledger:
            replace_public_name()
        return original_connect(database, *args, **kwargs)

    def private_checkpoint(name: str) -> None:
        if boundary == "private-sql" and name == "sql-complete":
            replace_public_name()

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(release_provenance, "_ledger_private_checkpoint", private_checkpoint)

    # When: private SQLite validates or updates only transaction-owned names.
    with pytest.raises(SpecValidationError, match="schema preflight"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: the attacker-established public generation is untouched and unconsumed.
    assert checkpoint_snapshot is not None
    assert _parent_snapshot(secure) == checkpoint_snapshot


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("state", ["current", "legacy", "reset-reuse"])
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
def test_every_public_replacement_stage_rolls_back_one_complete_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
    state: State,
    checkpoint: str,
) -> None:
    # Given: each accepted effective-state shape and one pre-commit replacement failure.
    secure = tmp_path / f"{page_size}-{state}-{checkpoint}"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _wal_state(ledger, page_size, state)
    before = _parent_snapshot(secure)

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    monkeypatch.setattr(release_provenance, "_ledger_publication_checkpoint", interrupt)

    # When: publication stops at this exact replacement stage.
    with pytest.raises(KeyboardInterrupt):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: no new-main/old-sidecar mix is public and the old bytes/inodes remain together.
    after = _parent_snapshot(secure)
    assert set(after) == set(before)
    for name, expected in before.items():
        assert after[name].payload == expected.payload
        assert after[name].identity[:-1] == expected.identity[:-1]
    with closing(sqlite3.connect(ledger)) as connection:
        old_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    assert old_rows
    assert not list(secure.glob(".*.nfi-transaction"))


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("state", ["current", "legacy", "reset-reuse"])
@pytest.mark.parametrize(
    "checkpoint",
    ["committed", "old-main-retired", "old-wal-retired", "old-shm-retired"],
)
def test_every_post_commit_interruption_finishes_one_sidecar_free_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_size: int,
    state: State,
    checkpoint: str,
) -> None:
    # Given: a committed private result interrupted during durable finalization.
    secure = tmp_path / f"{page_size}-{state}-{checkpoint}"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _wal_state(ledger, page_size, state)

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    monkeypatch.setattr(release_provenance, "_ledger_publication_checkpoint", interrupt)

    # When: control is interrupted after the durable commit point.
    with pytest.raises(KeyboardInterrupt):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )

    # Then: recovery finishes the new main and exposes no sidecar from the old generation.
    assert not ledger.with_name(f"{ledger.name}-wal").exists()
    assert not ledger.with_name(f"{ledger.name}-shm").exists()
    assert not list(secure.glob(".*.nfi-transaction"))
    monkeypatch.setattr(release_provenance, "_ledger_publication_checkpoint", lambda _name: None)
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256=CERTIFICATE_SHA256,
    )
    with closing(sqlite3.connect(ledger)) as connection:
        row = connection.execute(
            "SELECT state FROM certificate_publications WHERE bundle_id = ?",
            ("e" * 64,),
        ).fetchone()
    assert row == ("published",)


@pytest.mark.parametrize(
    "checkpoint",
    ["old-wal-staged", "old-shm-staged", "old-main-staged", "new-main-installed"],
)
def test_next_owner_recovers_a_process_interruption_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    # Given: process loss prevents the in-process rollback after a staged rename.
    secure = tmp_path / checkpoint
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    _wal_state(ledger, 4096, "current")
    real_restore = sqlite_publication._restore_old

    def interrupt(name: str) -> None:
        if name == checkpoint:
            raise KeyboardInterrupt

    def lose_process(_path: Path, _transaction: Path) -> None:
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(release_provenance, "_ledger_publication_checkpoint", interrupt)
    monkeypatch.setattr(sqlite_publication, "_restore_old", lose_process)
    with pytest.raises(SystemExit, match="process loss"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256=CERTIFICATE_SHA256,
        )
    assert list(secure.glob(".*.nfi-transaction"))

    # When: the next lock owner enters after the interruption.
    monkeypatch.setattr(sqlite_publication, "_restore_old", real_restore)
    monkeypatch.setattr(release_provenance, "_ledger_publication_checkpoint", lambda _name: None)
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256=CERTIFICATE_SHA256,
    )

    # Then: recovery first restores old complete state, then publishes the requested claim.
    assert not list(secure.glob(".*.nfi-transaction"))
    assert not ledger.with_name(f"{ledger.name}-wal").exists()
    assert not ledger.with_name(f"{ledger.name}-shm").exists()
    with closing(sqlite3.connect(ledger)) as connection:
        assert connection.execute(
            "SELECT state FROM certificate_publications WHERE bundle_id = ?",
            ("e" * 64,),
        ).fetchone() == ("published",)
