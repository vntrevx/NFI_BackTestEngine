"""Stable filesystem snapshots for consumed changed-signal roles."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .errors import SpecValidationError


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Stable device/inode identity of one trusted regular file."""

    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Mutation-sensitive metadata captured from one open descriptor."""

    identity: FileIdentity
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class TrustedFileSnapshot:
    """Exact bytes and descriptor identity accepted at a filesystem boundary."""

    path: Path
    metadata: FileMetadata
    payload: bytes


class _SnapshotOperation:
    """Own open read-only descriptors until one proof decision completes."""

    __slots__ = ("descriptors", "snapshots")

    def __init__(self) -> None:
        self.descriptors: dict[Path, int] = {}
        self.snapshots: dict[Path, TrustedFileSnapshot] = {}

    def read(self, path: Path, expected_path: Path) -> TrustedFileSnapshot:
        lexical = path.absolute()
        if lexical != expected_path.absolute():
            raise SpecValidationError("changed signal trusted role path differs")
        existing = self.snapshots.get(lexical)
        if existing is not None:
            _validate_descriptor(lexical, existing.metadata)
            return existing
        descriptor = _open_descriptor(lexical)
        try:
            snapshot = _read_open_descriptor(lexical, descriptor)
        except (OSError, SpecValidationError):
            os.close(descriptor)
            raise
        self.descriptors[lexical] = descriptor
        self.snapshots[lexical] = snapshot
        return snapshot

    def close(self) -> None:
        failure: SpecValidationError | None = None
        for path, descriptor in self.descriptors.items():
            try:
                snapshot = self.snapshots[path]
                current = _metadata(os.fstat(descriptor))
                if current != snapshot.metadata:
                    raise SpecValidationError(
                        "changed signal trusted role changed after snapshot"
                    )
                _validate_descriptor(path, current)
            except (OSError, SpecValidationError) as exc:
                if failure is None:
                    failure = SpecValidationError(
                        "changed signal trusted role snapshot boundary changed"
                    )
                    failure.__cause__ = exc
            finally:
                os.close(descriptor)
        if failure is not None:
            raise failure


_OPERATION: ContextVar[_SnapshotOperation | None] = ContextVar(
    "changed_signal_snapshot_operation",
    default=None,
)


@contextmanager
def trusted_file_operation() -> Generator[None, None, None]:
    """Keep every consumed descriptor stable for one complete proof decision."""
    operation = _SnapshotOperation()
    token = _OPERATION.set(operation)
    try:
        yield
    finally:
        try:
            operation.close()
        finally:
            _OPERATION.reset(token)


def read_stable_file(path: Path, expected_path: Path) -> TrustedFileSnapshot:
    """Read once through a no-follow descriptor and reject identity changes."""
    operation = _OPERATION.get()
    if operation is not None:
        return operation.read(path, expected_path)
    lexical = path.absolute()
    if lexical != expected_path.absolute():
        raise SpecValidationError("changed signal trusted role path differs")
    descriptor = _open_descriptor(lexical)
    try:
        return _read_open_descriptor(lexical, descriptor)
    finally:
        os.close(descriptor)


def validate_distinct_files(identities: tuple[FileIdentity, ...]) -> None:
    """Reject two consumed roles backed by the same device/inode pair."""
    if len(set(identities)) != len(identities):
        raise SpecValidationError("changed signal consumed role files share an inode")


def _open_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SpecValidationError(
            "changed signal trusted role path is linked or cannot open"
        ) from exc


def _read_open_descriptor(path: Path, descriptor: int) -> TrustedFileSnapshot:
    before = _metadata(os.fstat(descriptor))
    _validate_descriptor(path, before)
    payload = _read_descriptor(descriptor)
    after = _metadata(os.fstat(descriptor))
    if before != after or len(payload) != after.size:
        raise SpecValidationError("changed signal trusted role changed during snapshot")
    _validate_descriptor(path, after)
    return TrustedFileSnapshot(path=path, metadata=after, payload=payload)


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_descriptor(path: Path, descriptor: FileMetadata) -> None:
    try:
        path_metadata = _metadata(path.lstat())
    except OSError as exc:
        raise SpecValidationError("changed signal trusted role path changed") from exc
    if (
        path.resolve() != path
        or not stat.S_ISREG(descriptor.mode)
        or descriptor.links != 1
        or path_metadata != descriptor
    ):
        raise SpecValidationError(
            "changed signal trusted role snapshot is linked, aliased, or changed"
        )


def _metadata(value: os.stat_result) -> FileMetadata:
    return FileMetadata(
        identity=FileIdentity(device=value.st_dev, inode=value.st_ino),
        mode=value.st_mode,
        links=value.st_nlink,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )
