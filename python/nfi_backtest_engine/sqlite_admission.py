"""Descriptor-bound admission of untrusted pre-existing SQLite WAL state."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import SpecValidationError
from .sqlite_wal import validate_sqlite_sidecar_bytes


@dataclass(frozen=True, slots=True)
class SqliteAdmissionPolicy:
    """Exact effective schemas accepted by a SQLite persistence boundary."""

    expected_schemas: Mapping[str, str]
    accepted_tables: frozenset[frozenset[str]]


@dataclass(frozen=True, slots=True)
class SqliteAdmissionTarget:
    """Locked main-database identity and parent state awaiting admission."""

    path: Path
    directory_fd: int
    database_fd: int
    expected: os.stat_result
    parent_entries: frozenset[str]


@dataclass(frozen=True, slots=True)
class _OpenedFile:
    name: str
    descriptor: int
    expected: os.stat_result
    payload: bytes


@dataclass(frozen=True, slots=True)
class AdmittedSqliteState:
    """Retained descriptors and the privately admitted effective SQLite state."""

    target: SqliteAdmissionTarget
    sidecars: tuple[_OpenedFile, ...]
    private_path: Path

    def revalidate(self, *, atime_must_match: bool = True) -> None:
        identity = _stat_identity if atime_must_match else _stat_identity_without_atime
        _revalidate_file(
            self.target.path.name,
            self.target.database_fd,
            self.target.expected,
            self.target.directory_fd,
            identity,
        )
        for sidecar in self.sidecars:
            _revalidate_file(
                sidecar.name,
                sidecar.descriptor,
                sidecar.expected,
                self.target.directory_fd,
                identity,
            )
        if _parent_entries(self.target.path) != self.target.parent_entries:
            raise SpecValidationError("publication ledger changed during schema preflight")


@contextmanager
def admit_sqlite_state(
    target: SqliteAdmissionTarget,
    policy: SqliteAdmissionPolicy,
) -> Generator[AdmittedSqliteState]:
    """Validate main/WAL/SHM bytes privately while retaining all original descriptors."""
    import fcntl

    sidecars: list[_OpenedFile] = []
    try:
        unexpected = target.parent_entries.intersection(
            {f"{target.path.name}-journal"}
        )
        if unexpected:
            raise SpecValidationError("publication ledger has an unsupported SQLite sidecar")
        for suffix in ("-wal", "-shm"):
            name = f"{target.path.name}{suffix}"
            if name not in target.parent_entries:
                continue
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME,
                dir_fd=target.directory_fd,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                item = os.fstat(descriptor)
                _validate_owned_file(item, sidecar=True)
                payload = _read_exact(descriptor, item)
            except (OSError, SpecValidationError):
                os.close(descriptor)
                raise
            sidecars.append(_OpenedFile(name, descriptor, item, payload))
        identities = {
            (target.expected.st_dev, target.expected.st_ino),
            *((item.expected.st_dev, item.expected.st_ino) for item in sidecars),
        }
        if len(identities) != len(sidecars) + 1:
            raise SpecValidationError("publication ledger files must not alias one another")
        main_payload = _read_exact(target.database_fd, target.expected)
        wal = next((item.payload for item in sidecars if item.name.endswith("-wal")), None)
        shm = next((item.payload for item in sidecars if item.name.endswith("-shm")), None)
        validate_sqlite_sidecar_bytes(wal, shm)
        with tempfile.TemporaryDirectory(prefix="nfi-ledger-admission-") as directory:
            private_path = _write_private_snapshot(
                Path(directory), target.path.name, main_payload, sidecars
            )
            admitted = AdmittedSqliteState(
                target=target,
                sidecars=tuple(sidecars),
                private_path=private_path,
            )
            admitted.revalidate()
            _validate_private_snapshot(private_path, policy)
            admitted.revalidate()
            yield admitted
    except OSError as exc:
        raise SpecValidationError("publication ledger sidecar secure open failed") from exc
    finally:
        for sidecar in sidecars:
            os.close(sidecar.descriptor)


def _validate_owned_file(item: os.stat_result, *, sidecar: bool) -> None:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
    ):
        label = "sidecar" if sidecar else "file"
        raise SpecValidationError(
            f"publication ledger {label} must be an owned, single-link 0600 regular file"
        )


def _read_exact(descriptor: int, expected: os.stat_result) -> bytes:
    payload = bytearray()
    while len(payload) < expected.st_size:
        chunk = os.pread(
            descriptor,
            min(1024 * 1024, expected.st_size - len(payload)),
            len(payload),
        )
        if not chunk:
            raise SpecValidationError("publication ledger changed during schema preflight")
        payload.extend(chunk)
    if _stat_identity(os.fstat(descriptor)) != _stat_identity(expected):
        raise SpecValidationError("publication ledger changed during schema preflight")
    return bytes(payload)


def _write_private_snapshot(
    root: Path,
    name: str,
    main_payload: bytes,
    sidecars: list[_OpenedFile],
) -> Path:
    main = root / name
    main.write_bytes(main_payload)
    main.chmod(0o600)
    for sidecar in sidecars:
        scratch = root / sidecar.name
        scratch.write_bytes(sidecar.payload)
        scratch.chmod(0o600)
    return main


def _validate_private_snapshot(main: Path, policy: SqliteAdmissionPolicy) -> None:
    try:
        with closing(sqlite3.connect(main)) as snapshot:
            records = snapshot.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            integrity = snapshot.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise SpecValidationError("publication ledger SQLite format is invalid") from exc
    if integrity != [("ok",)]:
        raise SpecValidationError("publication ledger SQLite format is invalid")
    schemas = {str(table): str(schema) for table, schema in records}
    if frozenset(schemas) not in policy.accepted_tables or any(
        schemas[table] != policy.expected_schemas[table] for table in schemas
    ):
        raise SpecValidationError("publication ledger schema is incompatible")


def _revalidate_file(
    name: str,
    descriptor: int,
    expected: os.stat_result,
    directory_fd: int,
    identity: Callable[[os.stat_result], tuple[int, ...]],
) -> None:
    opened = os.fstat(descriptor)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if identity(opened) != identity(expected) or identity(current) != identity(expected):
        raise SpecValidationError("publication ledger changed during schema preflight")


def _parent_entries(path: Path) -> frozenset[str]:
    return frozenset(item.name for item in path.parent.iterdir())


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
