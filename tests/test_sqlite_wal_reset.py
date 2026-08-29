from __future__ import annotations

import os
import sqlite3
import struct
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_provenance import claim_certificate_once
from nfi_backtest_engine.sqlite_wal import validate_sqlite_sidecar_bytes
from test_release_provenance_sqlite import OLD_ATIME_NS, _ledger_snapshot
from test_sqlite_wal import (
    _rewrite_shm_header as rewrite_shm_header,
)
from test_sqlite_wal import (
    _rewrite_wal_frame as rewrite_wal_frame,
)


@dataclass(frozen=True, slots=True)
class ReusedWalSnapshot:
    main: bytes
    wal: bytes
    shm: bytes
    maximum_frame: int
    physical_frames: int


def reused_wal_snapshot(path: Path, page_size: int) -> ReusedWalSnapshot:
    claim_certificate_once(
        path,
        bundle_id="a" * 64,
        certificate_sha256="b" * 64,
    )
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute(f"PRAGMA page_size={page_size}")
    connection.execute("VACUUM")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    for index in range(180):
        connection.execute(
            "INSERT INTO certificate_publications VALUES "
            "(?, ?, ?, 'published', 'created', 'updated')",
            (f"old-{index:03}", f"certificate-{index:03}", f"attempt-{index:03}-" + "x" * 4096),
        )
    connection.commit()
    checkpoint = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    assert checkpoint is not None and checkpoint[0] == 0 and checkpoint[1] == checkpoint[2]
    connection.execute(
        "INSERT INTO certificate_publications VALUES "
        "('current-generation', 'certificate', 'attempt', 'published', 'created', 'updated')"
    )
    connection.commit()
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    main = path.read_bytes()
    wal = wal_path.read_bytes()
    shm = shm_path.read_bytes()
    byte_order = "<" if sys.byteorder == "little" else ">"
    maximum_frame = struct.unpack(f"{byte_order}I", shm[16:20])[0]
    physical_frames = (len(wal) - 32) // (page_size + 24)
    connection.close()
    for target, payload in ((path, main), (wal_path, wal), (shm_path, shm)):
        target.write_bytes(payload)
        target.chmod(0o600)
    return ReusedWalSnapshot(
        main=main,
        wal=wal,
        shm=shm,
        maximum_frame=maximum_frame,
        physical_frames=physical_frames,
    )


def _restore_rebuilt_shm(path: Path, snapshot: ReusedWalSnapshot) -> bytes:
    shm_path = path.with_name(f"{path.name}-shm")
    shm_path.unlink()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM certificate_publications").fetchone() == (
            182,
        )
        rebuilt = shm_path.read_bytes()
        main = path.read_bytes()
        wal = path.with_name(f"{path.name}-wal").read_bytes()
    for target, payload in (
        (path, main),
        (path.with_name(f"{path.name}-wal"), wal),
        (shm_path, rebuilt),
    ):
        target.write_bytes(payload)
        target.chmod(0o600)
    assert len(wal) == len(snapshot.wal)
    return rebuilt


@pytest.mark.parametrize("page_size", [4096, 65_536])
def test_checkpoint_reused_wal_validates_only_shm_selected_generation(
    tmp_path: Path,
    page_size: int,
) -> None:
    # Given: SQLite reset a checkpointed WAL and reused its old physical allocation.
    snapshot = reused_wal_snapshot(tmp_path / "ledger.sqlite", page_size)
    frame_bytes = page_size + 24
    old_salt_offset = 32 + snapshot.maximum_frame * frame_bytes + 8
    assert snapshot.physical_frames > snapshot.maximum_frame
    assert snapshot.wal[old_salt_offset : old_salt_offset + 8] != snapshot.wal[16:24]

    # When: the coherent wal-index selects the short current generation.
    validate_sqlite_sidecar_bytes(snapshot.wal, snapshot.shm)

    # Then: prior-generation complete frames beyond mxFrame remain inactive.


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("selector", ["present", "absent", "rebuilt"])
def test_checkpoint_reused_ledger_admits_with_sqlite_recovery_selector(
    tmp_path: Path,
    page_size: int,
    selector: Literal["present", "absent", "rebuilt"],
) -> None:
    # Given: a real current-schema reset/reuse ledger in one SQLite recovery shape.
    secure = tmp_path / f"{page_size}-{selector}"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    snapshot = reused_wal_snapshot(ledger, page_size)
    shm = ledger.with_name(f"{ledger.name}-shm")
    match selector:
        case "present":
            pass
        case "absent":
            shm.unlink()
        case "rebuilt":
            rebuilt = _restore_rebuilt_shm(ledger, snapshot)
            validate_sqlite_sidecar_bytes(snapshot.wal, rebuilt)
        case unreachable:
            assert_never(unreachable)

    # When: descriptor-bound admission replays the effective state before a new claim.
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256="f" * 64,
    )

    # Then: both the current WAL generation and the new claim are visible.
    with closing(sqlite3.connect(ledger)) as connection:
        rows = connection.execute(
            "SELECT bundle_id FROM certificate_publications "
            "WHERE bundle_id IN ('current-generation', ?) ORDER BY bundle_id",
            ("e" * 64,),
        ).fetchall()
    assert rows == [("current-generation",), ("e" * 64,)]


@pytest.mark.parametrize("page_size", [4096, 65_536])
def test_checkpoint_reused_wal_rejects_mxframe_beyond_physical_frames(
    tmp_path: Path,
    page_size: int,
) -> None:
    # Given: coherent SHM checksums claim one frame beyond the physical WAL.
    snapshot = reused_wal_snapshot(tmp_path / "ledger.sqlite", page_size)
    byte_order = "<" if sys.byteorder == "little" else ">"
    malformed = rewrite_shm_header(
        snapshot.shm,
        16,
        struct.pack(f"{byte_order}I", snapshot.physical_frames + 1),
    )

    # When/Then: the impossible logical prefix is rejected.
    with pytest.raises(SpecValidationError, match="WAL|SHM"):
        validate_sqlite_sidecar_bytes(snapshot.wal, malformed)


@pytest.mark.parametrize("page_size", [4096, 65_536])
@pytest.mark.parametrize("damage", ["salt", "checksum", "commit"])
def test_checkpoint_reused_wal_rejects_inconsistent_active_prefix(
    tmp_path: Path,
    page_size: int,
    damage: Literal["salt", "checksum", "commit"],
) -> None:
    # Given: only the SHM-selected active generation is replaced or made inconsistent.
    snapshot = reused_wal_snapshot(tmp_path / "ledger.sqlite", page_size)
    frame_bytes = page_size + 24
    active_offset = 32 + (snapshot.maximum_frame - 1) * frame_bytes
    match damage:
        case "salt":
            malformed = bytearray(snapshot.wal)
            malformed[active_offset + 8] ^= 1
        case "checksum":
            malformed = bytearray(snapshot.wal)
            malformed[active_offset + 24] ^= 1
        case "commit":
            page_number = struct.unpack(">I", snapshot.wal[active_offset : active_offset + 4])[0]
            malformed = bytearray(
                rewrite_wal_frame(
                    snapshot.wal,
                    page_size,
                    snapshot.maximum_frame - 1,
                    page_number=page_number,
                    database_size=0,
                )
            )
        case unreachable:
            assert_never(unreachable)

    # When/Then: stale-tail tolerance does not weaken active-prefix validation.
    with pytest.raises(SpecValidationError, match="WAL|SHM"):
        validate_sqlite_sidecar_bytes(bytes(malformed), snapshot.shm)


@pytest.mark.parametrize("page_size", [4096, 65_536])
def test_rejected_active_reuse_corruption_preserves_complete_triplet(
    tmp_path: Path,
    page_size: int,
) -> None:
    # Given: one selected active frame is corrupt in an otherwise real reset/reuse triplet.
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    snapshot = reused_wal_snapshot(ledger, page_size)
    wal = ledger.with_name(f"{ledger.name}-wal")
    malformed = bytearray(snapshot.wal)
    active_offset = 32 + (snapshot.maximum_frame - 1) * (page_size + 24)
    malformed[active_offset + 24] ^= 1
    wal.write_bytes(malformed)
    for item in secure.iterdir():
        os.utime(item, ns=(OLD_ATIME_NS, item.stat().st_mtime_ns))
    before = _ledger_snapshot(ledger)

    # When: descriptor-bound admission rejects the active checksum chain.
    with pytest.raises(SpecValidationError, match="WAL"):
        claim_certificate_once(
            ledger,
            bundle_id="e" * 64,
            certificate_sha256="f" * 64,
        )

    # Then: main, WAL, SHM, metadata, and parent entries remain exact.
    assert _ledger_snapshot(ledger) == before


@pytest.mark.parametrize("page_size", [4096, 65_536])
def test_checkpoint_reused_wal_rejects_truncated_active_prefix(
    tmp_path: Path,
    page_size: int,
) -> None:
    # Given: the physical WAL ends inside the SHM-selected active frame.
    snapshot = reused_wal_snapshot(tmp_path / "ledger.sqlite", page_size)
    active_bytes = 32 + snapshot.maximum_frame * (page_size + 24)

    # When/Then: incomplete active storage is rejected rather than treated as stale tail.
    with pytest.raises(SpecValidationError, match="WAL"):
        validate_sqlite_sidecar_bytes(snapshot.wal[: active_bytes - 1], snapshot.shm)
