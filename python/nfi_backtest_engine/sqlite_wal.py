"""Structural validation for untrusted SQLite WAL and SHM bytes."""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass

from .errors import SpecValidationError

_WAL_MAGIC_LITTLE_CHECKSUM = 0x377F0682
_WAL_MAGIC_BIG_CHECKSUM = 0x377F0683
_WAL_VERSION = 3_007_000
_MAX_PAGE_NUMBER = 0xFFFF_FFFE
_SHM_REGION_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class _WalState:
    page_size: int
    checksum_big_endian: bool
    maximum_frame: int
    database_size: int
    frame_checksum: tuple[int, int]
    salt: bytes


def validate_sqlite_sidecar_bytes(wal: bytes | None, shm: bytes | None) -> None:
    """Reject malformed, truncated, or inconsistent WAL/SHM state."""
    if shm is not None and wal is None:
        raise SpecValidationError("publication ledger SHM sidecar has no WAL")
    if wal is None:
        return
    selector = _parse_shm(shm) if shm is not None else None
    state = _validate_wal(
        wal,
        selector.maximum_frame if selector is not None else None,
    )
    if selector is not None and selector != state:
        raise SpecValidationError("publication ledger SHM header is malformed")


def _validate_wal(payload: bytes, selected_frames: int | None) -> _WalState:
    if len(payload) < 32:
        raise SpecValidationError("publication ledger WAL is truncated")
    magic, version, encoded_page_size, _sequence = struct.unpack(">4I", payload[:16])
    if magic not in {_WAL_MAGIC_LITTLE_CHECKSUM, _WAL_MAGIC_BIG_CHECKSUM}:
        raise SpecValidationError("publication ledger WAL header is malformed")
    if version != _WAL_VERSION or not (
        encoded_page_size == 1
        or 512 <= encoded_page_size <= 65_536
        and encoded_page_size & (encoded_page_size - 1) == 0
    ):
        raise SpecValidationError("publication ledger WAL header is malformed")
    page_size = 65_536 if encoded_page_size == 1 else encoded_page_size
    frame_bytes = page_size + 24
    if (len(payload) - 32) % frame_bytes != 0:
        raise SpecValidationError("publication ledger WAL frame is truncated")
    checksum_big_endian = magic & 1 == 1
    checksum_order = ">" if checksum_big_endian else "<"
    checksum = _wal_checksum(payload[:24], checksum_order)
    if checksum != struct.unpack(">2I", payload[24:32]):
        raise SpecValidationError("publication ledger WAL header checksum is invalid")
    committed_frame = 0
    database_size = 0
    committed_checksum = (0, 0)
    salt = payload[16:24]
    physical_frames = (len(payload) - 32) // frame_bytes
    if selected_frames is not None and selected_frames > physical_frames:
        raise SpecValidationError("publication ledger WAL is truncated")
    frame_limit = physical_frames if selected_frames is None else selected_frames
    for index in range(frame_limit):
        offset = 32 + index * frame_bytes
        frame = payload[offset : offset + frame_bytes]
        if frame[8:16] != salt:
            if selected_frames is None:
                break
            raise SpecValidationError("publication ledger WAL frame is malformed")
        next_checksum = _wal_checksum(frame[:8] + frame[24:], checksum_order, checksum)
        if next_checksum != struct.unpack(">2I", frame[16:24]):
            if selected_frames is None:
                break
            raise SpecValidationError("publication ledger WAL frame checksum is invalid")
        checksum = next_checksum
        page_number, frame_database_size = struct.unpack(">2I", frame[:8])
        if (
            page_number == 0
            or page_number > _MAX_PAGE_NUMBER
            or frame_database_size > _MAX_PAGE_NUMBER
            or frame_database_size != 0
            and page_number > frame_database_size
        ):
            raise SpecValidationError("publication ledger WAL frame is malformed")
        if frame_database_size != 0:
            committed_frame = index + 1
            database_size = frame_database_size
            committed_checksum = checksum
    if selected_frames is not None and committed_frame != selected_frames:
        raise SpecValidationError("publication ledger WAL frame is malformed")
    return _WalState(
        page_size=page_size,
        checksum_big_endian=checksum_big_endian,
        maximum_frame=committed_frame,
        database_size=database_size,
        frame_checksum=committed_checksum,
        salt=salt,
    )


def _wal_checksum(
    payload: bytes,
    byte_order: str,
    initial: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    first, second = initial
    words = struct.unpack(f"{byte_order}{len(payload) // 4}I", payload)
    for index in range(0, len(words), 2):
        first = (first + words[index] + second) & 0xFFFF_FFFF
        second = (second + words[index + 1] + first) & 0xFFFF_FFFF
    return first, second


def _parse_shm(payload: bytes) -> _WalState:
    if len(payload) < _SHM_REGION_BYTES or len(payload) % _SHM_REGION_BYTES != 0:
        raise SpecValidationError("publication ledger SHM is truncated")
    if payload[:48] != payload[48:96]:
        raise SpecValidationError("publication ledger SHM header is inconsistent")
    byte_order = "<" if sys.byteorder == "little" else ">"
    version = struct.unpack(f"{byte_order}I", payload[:4])[0]
    initialized = payload[12]
    checksum_big_endian = payload[13]
    encoded_page_size = struct.unpack(f"{byte_order}H", payload[14:16])[0]
    page_size = 65_536 if encoded_page_size == 1 else encoded_page_size
    maximum_frame, database_size = struct.unpack(f"{byte_order}2I", payload[16:24])
    frame_checksum = struct.unpack(f"{byte_order}2I", payload[24:32])
    checksum = _wal_checksum(payload[:40], byte_order)
    stored_checksum = struct.unpack(f"{byte_order}2I", payload[40:48])
    if (
        version != _WAL_VERSION
        or initialized != 1
        or checksum_big_endian not in {0, 1}
        or not (
            encoded_page_size == 1
            or 512 <= encoded_page_size <= 32_768
            and encoded_page_size & (encoded_page_size - 1) == 0
        )
        or database_size > _MAX_PAGE_NUMBER
        or checksum != stored_checksum
    ):
        raise SpecValidationError("publication ledger SHM header is malformed")
    return _WalState(
        page_size=page_size,
        checksum_big_endian=bool(checksum_big_endian),
        maximum_frame=maximum_frame,
        database_size=database_size,
        frame_checksum=frame_checksum,
        salt=payload[32:40],
    )
