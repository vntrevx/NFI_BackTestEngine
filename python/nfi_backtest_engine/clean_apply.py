"""Fail-closed application of a freshly generated cleanup audit."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psutil

from .canonical import write_json
from .clean import (
    ActivityProbe,
    _audit_destination,
    _managed_root,
    create_clean_audit,
)
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .specs import validate_clean_result

CLEAN_RESULT_VERSION = "1.0.0"
_APPLY_CATEGORIES = {
    "regenerable_vector_cache",
    "interrupted_failed_run",
    "temporary_arrow_docker_spool",
    "old_build_calibration",
}
_COMPLETED_CATEGORY = "user_preserved_run"


def apply_clean(
    root: str | Path = ".nfi",
    *,
    audit_path: str | Path | None = None,
    result_path: str | Path | None = None,
    preserve: Sequence[str | Path] = (),
    include_completed: bool = False,
    activity_probe: ActivityProbe | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a fresh audit and delete only its explicitly reclaimable units."""
    managed_root = _managed_root(root)
    audit_destination = _audit_destination(managed_root, audit_path)
    result_destination = _result_destination(managed_root, result_path)
    lock_path = managed_root / ".clean-apply.lock"

    with _cleanup_lock(lock_path):
        audit = create_clean_audit(
            managed_root,
            output_path=audit_destination,
            preserve=preserve,
            activity_probe=activity_probe,
            inspect_runtime=True,
            include_completed=include_completed,
            created_at=created_at,
            _control_paths=(lock_path, result_destination),
        )
        if audit["safety"]["fail_closed"]:
            raise SpecValidationError(
                "cleanup apply refused because the fresh audit is fail-closed; "
                f"review {audit_destination}"
            )

        selected = [entry for entry in audit["entries"] if entry["deletable"]]
        _validate_selection(
            managed_root,
            selected,
            audit_destination=audit_destination,
            result_destination=result_destination,
            include_completed=include_completed,
        )
        receipt = _new_receipt(
            audit,
            audit_destination,
            result_destination,
            preserve=preserve,
            include_completed=include_completed,
            created_at=created_at,
        )
        _write_receipt(result_destination, receipt)

        try:
            for entry in selected:
                target = _revalidate_target(managed_root, audit, entry)
                _delete_target(target)
                receipt["deleted"].append(
                    {
                        "path": entry["path"],
                        "category": entry["category"],
                        "logical_bytes": entry["logical_bytes"],
                        "reclaimable_allocated_bytes": entry[
                            "reclaimable_allocated_bytes"
                        ],
                    }
                )
                _write_receipt(result_destination, receipt)
        except (OSError, BenchmarkError, SpecValidationError) as exc:
            receipt["status"] = "partial_failure"
            receipt["failure"] = str(exc)
            receipt["completed_at"] = _timestamp()
            _write_receipt(result_destination, receipt)
            raise BenchmarkError(
                f"cleanup stopped after a partial failure; review {result_destination}: {exc}"
            ) from exc

        receipt["status"] = "complete"
        receipt["completed_at"] = _timestamp()
        receipt["summary"] = {
            "deleted_unit_count": len(receipt["deleted"]),
            "deleted_logical_bytes": sum(
                int(entry["logical_bytes"]) for entry in receipt["deleted"]
            ),
            "estimated_reclaimed_allocated_bytes": sum(
                int(entry["reclaimable_allocated_bytes"])
                for entry in receipt["deleted"]
            ),
        }
        _write_receipt(result_destination, receipt)
        return receipt


def format_clean_result(result: Mapping[str, Any]) -> str:
    """Render a concise cleanup application result."""
    summary = result["summary"]
    return "\n".join(
        [
            f"Clean apply             {result['status']}",
            f"Deleted                 {summary['deleted_unit_count']} units",
            (
                "Estimated reclaimed     "
                f"{summary['estimated_reclaimed_allocated_bytes']} allocated bytes"
            ),
            f"Audit JSON              {result['audit']['path']}",
            f"Result JSON             {result['result_path']}",
        ]
    )


def _result_destination(root: Path, value: str | Path | None) -> Path:
    candidate = (
        root / "clean-result.json" if value is None else Path(value).absolute()
    )
    if candidate.is_symlink():
        raise SpecValidationError(f"clean result path must not be a symlink: {candidate}")
    destination = candidate.parent.resolve(strict=False) / candidate.name
    if not destination.is_relative_to(root):
        raise SpecValidationError(
            f"clean result path must stay inside the managed .nfi root: {destination}"
        )
    return destination


def _validate_selection(
    root: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    audit_destination: Path,
    result_destination: Path,
    include_completed: bool,
) -> None:
    for entry in entries:
        category = str(entry["category"])
        permitted = category in _APPLY_CATEGORIES or (
            include_completed and category == _COMPLETED_CATEGORY
        )
        if not permitted:
            raise SpecValidationError(
                f"fresh audit selected a protected cleanup category: {category}"
            )
        target = _entry_path(root, str(entry["path"]))
        for control_path in (audit_destination, result_destination):
            if control_path == target or control_path.is_relative_to(target):
                raise SpecValidationError(
                    f"cleanup control output is inside a deletion target: {control_path}"
                )


def _entry_path(root: Path, relative_value: str) -> Path:
    relative = PurePosixPath(relative_value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise SpecValidationError(f"cleanup audit contains an unsafe path: {relative_value}")
    target = root.joinpath(*relative.parts)
    if target == root or not target.is_relative_to(root):
        raise SpecValidationError(f"cleanup target escapes managed root: {relative_value}")
    return target


def _revalidate_target(
    root: Path,
    audit: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> Path:
    root_stat = root.stat(follow_symlinks=False)
    if (
        root_stat.st_dev != audit["root"]["device"]
        or root_stat.st_ino != audit["root"]["inode"]
    ):
        raise SpecValidationError("managed .nfi root identity changed after audit")

    target = _entry_path(root, str(entry["path"]))
    if not target.exists():
        raise SpecValidationError(f"cleanup target disappeared after audit: {target}")
    if target.is_symlink():
        raise SpecValidationError(f"cleanup target became a symlink after audit: {target}")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise SpecValidationError(f"cleanup target escaped managed root: {target}")
    _validate_tree_entries(target)
    return target


def _validate_tree_entries(target: Path) -> None:
    pending = [target]
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SpecValidationError(
                f"cleanup target contains a symlink after audit: {path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(metadata.st_mode):
            raise SpecValidationError(
                f"cleanup target contains a special filesystem entry: {path}"
            )


def _delete_target(target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _new_receipt(
    audit: Mapping[str, Any],
    audit_destination: Path,
    result_destination: Path,
    *,
    preserve: Sequence[str | Path],
    include_completed: bool,
    created_at: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": CLEAN_RESULT_VERSION,
        "created_at": created_at or _timestamp(),
        "completed_at": None,
        "mode": "apply",
        "status": "in_progress",
        "root": audit["root"],
        "audit": {
            "path": str(audit_destination),
            "sha256": sha256_file(audit_destination),
        },
        "result_path": str(result_destination),
        "selection": {
            "include_completed": include_completed,
            "preserve": [str(path) for path in preserve],
        },
        "deleted": [],
        "failure": None,
        "summary": {
            "deleted_unit_count": 0,
            "deleted_logical_bytes": 0,
            "estimated_reclaimed_allocated_bytes": 0,
        },
    }


def _write_receipt(destination: Path, receipt: Mapping[str, Any]) -> None:
    validate_clean_result(receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        write_json(temporary, receipt)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _cleanup_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise SpecValidationError(f"cleanup lock must not be a symlink: {path}")
    _discard_stale_lock(path)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SpecValidationError(f"another cleanup apply owns the lock: {path}") from exc
    try:
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            if path.read_text(encoding="ascii") == token:
                path.unlink()
        except (FileNotFoundError, PermissionError, UnicodeError):
            pass


def _discard_stale_lock(path: Path) -> None:
    try:
        value = path.read_text(encoding="ascii").split(":", 1)[0]
        pid = int(value)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError) as exc:
        raise SpecValidationError(f"cleanup lock identity is invalid: {path}") from exc
    if psutil.pid_exists(pid):
        raise SpecValidationError(f"another cleanup apply owns the lock: {path}")
    try:
        path.unlink()
    except OSError as exc:
        raise SpecValidationError(f"cannot remove stale cleanup lock: {path}") from exc


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
