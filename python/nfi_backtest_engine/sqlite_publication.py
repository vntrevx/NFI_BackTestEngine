"""Recoverable publication of a checkpointed private SQLite database."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .errors import SpecValidationError

_PHASE_FILE = "phase"
_NEW_MAIN = "new-main"
_OLD_NAMES = {
    "": "old-main",
    "-wal": "old-wal",
    "-shm": "old-shm",
}


def recover_sqlite_publication(path: Path) -> None:
    """Restore an interrupted old generation or finish a committed new one."""
    transaction = _transaction_path(path)
    if not transaction.exists():
        return
    _validate_transaction_directory(transaction)
    try:
        phase = (transaction / _PHASE_FILE).read_text(encoding="ascii")
    except FileNotFoundError:
        if any((transaction / name).exists() for name in _OLD_NAMES.values()):
            raise SpecValidationError("publication ledger recovery record is malformed") from None
        _remove_file(transaction / _NEW_MAIN)
        _remove_file(transaction / f".{_PHASE_FILE}.tmp")
        transaction.rmdir()
        _fsync_directory(path.parent)
        return
    except (OSError, UnicodeError) as exc:
        raise SpecValidationError("publication ledger recovery record is malformed") from exc
    if phase == "committed":
        _finish_committed(path, transaction)
    elif phase in {"prepared", "staging-old", "old-staged"}:
        _restore_old(path, transaction)
    else:
        raise SpecValidationError("publication ledger recovery record is malformed")


def publish_sqlite_main(
    path: Path,
    private_path: Path,
    checkpoint: Callable[[str], None],
) -> None:
    """Replace the public triplet with one durable sidecar-free private result."""
    transaction = _transaction_path(path)
    if transaction.exists():
        raise SpecValidationError("publication ledger transaction already exists")
    transaction.mkdir(mode=0o700)
    _fsync_directory(path.parent)
    committed = False
    try:
        _write_phase(transaction, "prepared")
        _copy_durable(private_path, transaction / _NEW_MAIN)
        checkpoint("prepared")
        _write_phase(transaction, "staging-old")
        for suffix in ("-wal", "-shm", ""):
            public = path.with_name(f"{path.name}{suffix}")
            if public.exists():
                os.replace(public, transaction / _OLD_NAMES[suffix])
            checkpoint(f"old{suffix or '-main'}-staged")
        _fsync_directory(path.parent)
        _fsync_directory(transaction)
        _write_phase(transaction, "old-staged")
        os.replace(transaction / _NEW_MAIN, path)
        _fsync_directory(path.parent)
        checkpoint("new-main-installed")
        _write_phase(transaction, "committed")
        committed = True
        checkpoint("committed")
        _finish_committed(path, transaction, checkpoint)
    except BaseException:  # noqa: BLE001  # BROAD_EXCEPT_OK — recover before propagating.
        if committed:
            _finish_committed(path, transaction)
        else:
            _restore_old(path, transaction)
        raise


def _transaction_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.nfi-transaction")


def _validate_transaction_directory(transaction: Path) -> None:
    item = transaction.lstat()
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise SpecValidationError("publication ledger recovery record is not owner-controlled")


def _copy_durable(source: Path, target: Path) -> None:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb", closefd=False) as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_phase(transaction: Path, phase: str) -> None:
    temporary = transaction / f".{_PHASE_FILE}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, phase.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, transaction / _PHASE_FILE)
    _fsync_directory(transaction)


def _restore_old(path: Path, transaction: Path) -> None:
    old_main = transaction / _OLD_NAMES[""]
    if old_main.exists() and path.exists():
        path.unlink()
    for suffix in ("", "-wal", "-shm"):
        backup = transaction / _OLD_NAMES[suffix]
        if backup.exists():
            os.replace(backup, path.with_name(f"{path.name}{suffix}"))
    _remove_file(transaction / _NEW_MAIN)
    _remove_file(transaction / _PHASE_FILE)
    _remove_file(transaction / f".{_PHASE_FILE}.tmp")
    transaction.rmdir()
    _fsync_directory(path.parent)


def _finish_committed(
    path: Path,
    transaction: Path,
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    staged = transaction / _NEW_MAIN
    if staged.exists():
        os.replace(staged, path)
    for suffix in ("-wal", "-shm"):
        _remove_file(path.with_name(f"{path.name}{suffix}"))
    for suffix in ("", "-wal", "-shm"):
        _remove_file(transaction / _OLD_NAMES[suffix])
        if checkpoint is not None:
            checkpoint(f"old{suffix or '-main'}-retired")
    _remove_file(transaction / _PHASE_FILE)
    _remove_file(transaction / f".{_PHASE_FILE}.tmp")
    transaction.rmdir()
    _fsync_directory(path.parent)


def _remove_file(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
