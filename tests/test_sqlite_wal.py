from __future__ import annotations

import sqlite3
import struct
import sys
from contextlib import closing
from pathlib import Path
from typing import Literal, assert_never

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.release_provenance import claim_certificate_once
from nfi_backtest_engine.sqlite_wal import validate_sqlite_sidecar_bytes

BUNDLE_ID = "a" * 64
CERTIFICATE_SHA256 = "b" * 64
_MAX_PAGE_NUMBER = 0xFFFF_FFFE


def _checksum(
    payload: bytes | bytearray,
    byte_order: str,
    initial: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    first, second = initial
    words = struct.unpack(f"{byte_order}{len(payload) // 4}I", payload)
    for index in range(0, len(words), 2):
        first = (first + words[index] + second) & 0xFFFF_FFFF
        second = (second + words[index + 1] + first) & 0xFFFF_FFFF
    return first, second


def _real_sidecars(path: Path, page_size: int) -> tuple[bytes, bytes]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA page_size={page_size}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.commit()
        wal = path.with_name(f"{path.name}-wal").read_bytes()
        shm = path.with_name(f"{path.name}-shm").read_bytes()
    return wal, shm


def _rewrite_shm_header(shm: bytes, offset: int, replacement: bytes) -> bytes:
    byte_order = "<" if sys.byteorder == "little" else ">"
    header = bytearray(shm[:48])
    header[offset : offset + len(replacement)] = replacement
    header[40:48] = struct.pack(f"{byte_order}2I", *_checksum(header[:40], byte_order))
    return bytes(header) * 2 + shm[96:]


def _rewrite_wal_frame(
    wal: bytes,
    page_size: int,
    frame_index: int,
    *,
    page_number: int,
    database_size: int,
) -> bytes:
    rewritten = bytearray(wal)
    frame_bytes = page_size + 24
    offset = 32 + frame_index * frame_bytes
    rewritten[offset : offset + 8] = struct.pack(">2I", page_number, database_size)
    checksum_order = "<" if int.from_bytes(wal[:4], "big") & 1 == 0 else ">"
    checksum = _checksum(rewritten[:24], checksum_order)
    for frame_offset in range(32, len(rewritten), frame_bytes):
        frame = rewritten[frame_offset : frame_offset + frame_bytes]
        checksum = _checksum(frame[:8] + frame[24:], checksum_order, checksum)
        rewritten[frame_offset + 16 : frame_offset + 24] = struct.pack(">2I", *checksum)
    return bytes(rewritten)


@pytest.mark.parametrize("page_size", [4096, 65_536])
def test_real_sqlite_sidecars_validate_when_page_size_is_legal(
    tmp_path: Path,
    page_size: int,
) -> None:
    # Given: SQLite-produced WAL and wal-index bytes at a legal page size.
    wal, shm = _real_sidecars(tmp_path / "real.sqlite", page_size)

    # When: the complete sidecar state crosses the format boundary.
    validate_sqlite_sidecar_bytes(wal, shm)

    # Then: no format error is raised.


def test_shm_rejects_zero_page_size_encoding(tmp_path: Path) -> None:
    # Given: a real wal-index whose szPage is changed to the reserved zero encoding.
    wal, shm = _real_sidecars(tmp_path / "invalid-page-size.sqlite", 4096)
    byte_order = "<" if sys.byteorder == "little" else ">"
    malformed = _rewrite_shm_header(shm, 14, struct.pack(f"{byte_order}H", 0))

    # When/Then: the invalid encoding is rejected even though its header checksum is valid.
    with pytest.raises(SpecValidationError, match="SHM"):
        validate_sqlite_sidecar_bytes(wal, malformed)


@pytest.mark.parametrize(
    "field",
    ["checksum-order", "database-size", "maximum-frame", "frame-checksum"],
)
def test_shm_rejects_checksum_valid_metadata_for_another_commit(
    tmp_path: Path,
    field: Literal["checksum-order", "database-size", "maximum-frame", "frame-checksum"],
) -> None:
    # Given: one field in a real duplicated wal-index header describes another state.
    wal, shm = _real_sidecars(tmp_path / f"inconsistent-{field}.sqlite", 4096)
    byte_order = "<" if sys.byteorder == "little" else ">"
    match field:
        case "checksum-order":
            malformed = _rewrite_shm_header(shm, 13, bytes([1 - shm[13]]))
        case "database-size":
            malformed = _rewrite_shm_header(
                shm,
                20,
                struct.pack(f"{byte_order}I", _MAX_PAGE_NUMBER),
            )
        case "maximum-frame":
            malformed = _rewrite_shm_header(
                shm,
                16,
                struct.pack(f"{byte_order}I", 0),
            )
        case "frame-checksum":
            malformed = _rewrite_shm_header(
                shm,
                24,
                struct.pack(f"{byte_order}2I", 0, 0),
            )
        case unreachable:
            assert_never(unreachable)

    # When/Then: checksum-valid but commit-incoherent SHM is rejected explicitly.
    with pytest.raises(SpecValidationError, match="SHM"):
        validate_sqlite_sidecar_bytes(wal, malformed)


@pytest.mark.parametrize(
    ("page_number", "database_size"),
    [(3, 2), (0xFFFF_FFFF, 0xFFFF_FFFF)],
)
def test_wal_rejects_illegal_committed_page_bounds(
    tmp_path: Path,
    page_number: int,
    database_size: int,
) -> None:
    # Given: a real committed frame with independently recomputed rolling checksums.
    wal, _shm = _real_sidecars(tmp_path / "illegal-frame.sqlite", 4096)
    frame_count = (len(wal) - 32) // (4096 + 24)
    malformed = _rewrite_wal_frame(
        wal,
        4096,
        frame_count - 1,
        page_number=page_number,
        database_size=database_size,
    )

    # When/Then: impossible page metadata is rejected before private SQLite use.
    with pytest.raises(SpecValidationError, match="WAL"):
        validate_sqlite_sidecar_bytes(malformed, None)


def test_wal_accepts_precommit_page_above_truncated_commit_size(tmp_path: Path) -> None:
    # Given: a legal transaction whose earlier frame is truncated by its commit frame.
    wal, _shm = _real_sidecars(tmp_path / "truncate.sqlite", 4096)
    assert (len(wal) - 32) // (4096 + 24) >= 2
    rewritten = _rewrite_wal_frame(
        wal,
        4096,
        0,
        page_number=3,
        database_size=0,
    )

    # When: the structural WAL contract validates the transaction.
    validate_sqlite_sidecar_bytes(rewritten, None)

    # Then: precommit pages are not incorrectly bounded by a later truncating commit.


@pytest.mark.parametrize("schema", ["current", "legacy"])
def test_64k_effective_ledger_is_admitted_for_current_and_legacy_schema(
    tmp_path: Path,
    schema: Literal["current", "legacy"],
) -> None:
    # Given: an exact ledger schema converted to 64 KiB before a WAL-only commit.
    secure = tmp_path / schema
    secure.mkdir(mode=0o700)
    ledger = secure / "ledger.sqlite"
    match schema:
        case "current":
            claim_certificate_once(
                ledger,
                bundle_id=BUNDLE_ID,
                certificate_sha256=CERTIFICATE_SHA256,
            )
        case "legacy":
            with closing(sqlite3.connect(ledger)) as connection:
                connection.execute(
                    "CREATE TABLE used_certificates (bundle_id TEXT PRIMARY KEY, "
                    "certificate_sha256 TEXT NOT NULL, used_at TEXT NOT NULL)"
                )
                connection.commit()
            ledger.chmod(0o600)
        case unreachable:
            assert_never(unreachable)
    with closing(sqlite3.connect(ledger)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA page_size=65536")
        connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        match schema:
            case "current":
                connection.execute(
                    "INSERT INTO certificate_publications VALUES "
                    "('wal-row', 'wal-certificate', 'wal-attempt', "
                    "'published', 'created', 'updated')"
                )
            case "legacy":
                connection.execute(
                    "INSERT INTO used_certificates VALUES ('wal-row', 'wal-certificate', 'used-at')"
                )
            case unreachable:
                assert_never(unreachable)
        connection.commit()
        paths = (
            ledger,
            ledger.with_name("ledger.sqlite-wal"),
            ledger.with_name("ledger.sqlite-shm"),
        )
        payloads = {path: path.read_bytes() for path in paths}
    for path, payload in payloads.items():
        path.write_bytes(payload)
        path.chmod(0o600)

    # When: a new publication claim admits and replays the effective state.
    claim_certificate_once(
        ledger,
        bundle_id="e" * 64,
        certificate_sha256="f" * 64,
    )

    # Then: both the WAL-only row and new claim survive migration/publication.
    with closing(sqlite3.connect(ledger)) as connection:
        rows = connection.execute(
            "SELECT bundle_id FROM certificate_publications ORDER BY bundle_id"
        ).fetchall()
    assert ("wal-row",) in rows
    assert ("e" * 64,) in rows
