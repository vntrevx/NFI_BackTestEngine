"""Evidence-aware, read-only classification of the managed ``.nfi`` tree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .specs import (
    CERTIFICATION_REPORT_SCHEMA,
    FULL_X7_CERTIFICATION_SCHEMA,
    FULL_X7_CERTIFICATION_V2_SCHEMA,
    validate_clean_audit,
    validate_schema,
)

CLEAN_AUDIT_VERSION = "1.0.0"

CATEGORIES = (
    "active_run_checkpoint",
    "release_certificate_bundle",
    "official_oracle_freqtrade_zip",
    "user_preserved_run",
    "regenerable_vector_cache",
    "interrupted_failed_run",
    "temporary_arrow_docker_spool",
    "old_build_calibration",
    "unclassified_protected",
)

ActivityProbe = Callable[[], Mapping[str, Any]]

_EVIDENCE_NAME_PARTS = (
    "certification",
    "certificate",
    "bundle",
    "platform-evidence",
    "release-input-lock",
)
_CACHE_SEGMENTS = {
    "cache",
    "vector-cache",
    "vectors",
    "cold-vector-cache",
}
_TEMP_SEGMENTS = {
    "spool",
    "docker-spool",
    "tmp",
    "temp",
}
_BUILD_SEGMENTS = {
    "build",
    "builds",
    "calibration",
    "calibrations",
}
_FAILED_STATUSES = {
    "failed",
    "blocked",
    "blocked_unsupported_semantics",
    "interrupted",
    "cancelled",
}


@dataclass
class _UnitScan:
    path: Path
    relative_path: str
    file_count: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0
    names: set[str] = field(default_factory=set)
    segments: set[str] = field(default_factory=set)
    run_files: list[Path] = field(default_factory=list)
    evidence_files: list[Path] = field(default_factory=list)
    oracle_files: list[Path] = field(default_factory=list)
    preserve_markers: list[Path] = field(default_factory=list)
    pid_files: list[dict[str, Any]] = field(default_factory=list)
    lock_files: list[dict[str, Any]] = field(default_factory=list)
    internal_symlinks: list[str] = field(default_factory=list)
    special_files: list[str] = field(default_factory=list)
    arrow_files: int = 0


def create_clean_audit(
    root: str | Path = ".nfi",
    *,
    output_path: str | Path | None = None,
    preserve: Sequence[str | Path] = (),
    activity_probe: ActivityProbe | None = None,
    inspect_runtime: bool = True,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Classify managed disk usage and write an audit without deleting anything."""
    managed_root = _managed_root(root)
    destination = _audit_destination(managed_root, output_path)
    preserved_units = _preserved_units(managed_root, preserve)
    runtime = (
        dict(activity_probe())
        if activity_probe is not None
        else _probe_runtime_activity()
        if inspect_runtime
        else _unprobed_runtime_activity()
    )
    _validate_runtime_probe(runtime)

    scanned_units = [
        _scan_unit(managed_root, child, destination)
        for child in sorted(managed_root.iterdir(), key=lambda path: path.name)
        if child != destination
    ]
    scans = [
        scan
        for scan in scanned_units
        if scan.file_count > 0 or scan.internal_symlinks or scan.special_files
    ]
    global_runtime_blocker = _runtime_blocker(runtime)
    entries = [
        _classify_unit(
            managed_root,
            scan,
            explicitly_preserved=scan.relative_path in preserved_units,
            global_runtime_blocker=global_runtime_blocker,
        )
        for scan in scans
    ]
    global_runtime_active = bool(
        runtime["services"]["active"] or runtime["containers"]["active"]
    )
    categories = _category_totals(entries)
    active_pids = [
        item
        for scan in scans
        for item in scan.pid_files
        if item["status"] == "active"
    ]
    active_locks = [
        item
        for scan in scans
        for item in scan.lock_files
        if item["status"] in {"active", "unknown"}
    ]
    issues = [
        {
            "code": "CERTIFICATION_IDENTITY_INCOMPLETE",
            "path": entry["path"],
            "message": (
                "certification-like evidence is protected because its identity is incomplete"
            ),
        }
        for entry in entries
        if entry["category"] == "release_certificate_bundle"
        and entry["identity_complete"] is False
    ]
    total_logical = sum(entry["logical_bytes"] for entry in entries)
    total_allocated = sum(entry["allocated_bytes"] for entry in entries)
    reclaimable_logical = sum(
        entry["logical_bytes"] for entry in entries if entry["deletable"]
    )
    reclaimable_allocated = sum(
        entry["allocated_bytes"] for entry in entries if entry["deletable"]
    )
    audit = {
        "schema_version": CLEAN_AUDIT_VERSION,
        "created_at": created_at
        or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": "dry-run",
        "root": {
            "path": str(managed_root),
            "device": managed_root.stat().st_dev,
            "inode": managed_root.stat().st_ino,
        },
        "audit_path": str(destination),
        "activity": {
            "pid_files": [item for scan in scans for item in scan.pid_files],
            "locks": [item for scan in scans for item in scan.lock_files],
            "services": runtime["services"],
            "containers": runtime["containers"],
        },
        "summary": {
            "unit_count": len(entries),
            "file_count": sum(entry["file_count"] for entry in entries),
            "logical_bytes": total_logical,
            "allocated_bytes": total_allocated,
            "reclaimable_logical_bytes": reclaimable_logical,
            "reclaimable_allocated_bytes": reclaimable_allocated,
            "protected_logical_bytes": total_logical - reclaimable_logical,
            "protected_allocated_bytes": total_allocated - reclaimable_allocated,
        },
        "safety": {
            "deletion_performed": False,
            "active_pid_count": len(active_pids),
            "active_lock_count": len(active_locks),
            "active_service_count": len(runtime["services"]["active"]),
            "active_container_count": len(runtime["containers"]["active"]),
            "fail_closed": bool(
                active_pids
                or active_locks
                or global_runtime_active
                or global_runtime_blocker
                or issues
            ),
        },
        "categories": categories,
        "entries": entries,
        "issues": issues,
    }
    validate_clean_audit(audit)
    write_json(destination, audit)
    return audit


def format_clean_audit(audit: Mapping[str, Any]) -> str:
    """Render a compact, stable terminal projection of a clean dry-run."""
    summary = audit["summary"]
    lines = [
        "Clean dry-run           no files deleted",
        f"Managed root            {audit['root']['path']}",
        f"Scanned                 {summary['file_count']} files in {summary['unit_count']} units",
        (
            "Expected reclaim        "
            f"{summary['reclaimable_logical_bytes']} logical / "
            f"{summary['reclaimable_allocated_bytes']} allocated bytes"
        ),
        (
            "Protected               "
            f"{summary['protected_logical_bytes']} logical / "
            f"{summary['protected_allocated_bytes']} allocated bytes"
        ),
    ]
    for category in audit["categories"]:
        lines.append(
            f"{category['category']:<24} "
            f"{category['file_count']:>8} files "
            f"{category['logical_bytes']:>12} logical "
            f"{category['reclaimable_logical_bytes']:>12} reclaimable"
        )
    safety = audit["safety"]
    lines.extend(
        [
            (
                "Active guards          "
                f"pids={safety['active_pid_count']}, "
                f"locks={safety['active_lock_count']}, "
                f"services={safety['active_service_count']}, "
                f"containers={safety['active_container_count']}"
            ),
            f"Audit JSON              {audit['audit_path']}",
        ]
    )
    return "\n".join(lines)


def _managed_root(source: str | Path) -> Path:
    raw = Path(source).absolute()
    if raw.name != ".nfi":
        raise SpecValidationError(f"clean root must be a directory named .nfi: {raw}")
    if raw.is_symlink():
        raise SpecValidationError(f"clean root must not be a symlink: {raw}")
    try:
        root = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SpecValidationError(f"clean root does not exist: {raw}") from exc
    if not root.is_dir():
        raise SpecValidationError(f"clean root is not a directory: {root}")
    return root


def _audit_destination(root: Path, output_path: str | Path | None) -> Path:
    candidate = (
        root / "clean-audit.json"
        if output_path is None
        else Path(output_path).absolute()
    )
    if candidate.is_symlink():
        raise SpecValidationError(f"clean audit path must not be a symlink: {candidate}")
    parent = candidate.parent.resolve(strict=False)
    destination = parent / candidate.name
    if not destination.is_relative_to(root):
        raise SpecValidationError(
            f"clean audit path must stay inside the managed .nfi root: {destination}"
        )
    return destination


def _preserved_units(root: Path, paths: Sequence[str | Path]) -> set[str]:
    units: set[str] = set()
    for value in paths:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else root / raw
        if candidate.is_symlink():
            raise SpecValidationError(f"preserved path must not be a symlink: {candidate}")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root) or resolved == root:
            raise SpecValidationError(
                f"preserved path must identify one entry inside the managed root: {candidate}"
            )
        if not resolved.exists():
            raise SpecValidationError(f"preserved path does not exist: {candidate}")
        units.add(resolved.relative_to(root).parts[0])
    return units


def _scan_unit(root: Path, unit: Path, audit_path: Path) -> _UnitScan:
    scan = _UnitScan(path=unit, relative_path=unit.relative_to(root).as_posix())
    pending = [unit]
    while pending:
        path = pending.pop()
        if path == audit_path:
            continue
        if path.is_symlink():
            _record_symlink(root, scan, path)
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise BenchmarkError(f"cannot inspect managed path: {path}: {exc}") from exc
        if path.is_dir():
            scan.segments.add(path.name.lower())
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name, reverse=True)
            except OSError as exc:
                raise BenchmarkError(f"cannot list managed directory: {path}: {exc}") from exc
            pending.extend(children)
            continue
        _record_file(root, scan, path, metadata)
    return scan


def _record_symlink(root: Path, scan: _UnitScan, path: Path) -> None:
    try:
        target = path.resolve(strict=False)
    except OSError as exc:
        raise SpecValidationError(f"cannot resolve symlink inside managed root: {path}") from exc
    if not target.is_relative_to(root):
        raise SpecValidationError(
            f"managed .nfi symlink escapes its root: {path} -> {target}"
        )
    metadata = path.lstat()
    scan.file_count += 1
    scan.logical_bytes += metadata.st_size
    scan.allocated_bytes += _allocated_bytes(metadata)
    scan.internal_symlinks.append(path.relative_to(root).as_posix())


def _record_file(
    root: Path,
    scan: _UnitScan,
    path: Path,
    metadata: os.stat_result,
) -> None:
    lower_name = path.name.lower()
    scan.file_count += 1
    scan.logical_bytes += metadata.st_size
    scan.allocated_bytes += _allocated_bytes(metadata)
    scan.names.add(lower_name)
    scan.segments.update(part.lower() for part in path.relative_to(root).parts[:-1])
    if not stat.S_ISREG(metadata.st_mode):
        scan.special_files.append(path.relative_to(root).as_posix())
        return
    if lower_name == "run.json":
        scan.run_files.append(path)
    if lower_name in {".nfi-preserve", ".nfi-keep"}:
        scan.preserve_markers.append(path)
    if lower_name.endswith(".pid"):
        scan.pid_files.append(_inspect_pid_file(root, path))
    if lower_name.endswith(".lock") or lower_name == ".lock":
        scan.lock_files.append(_inspect_lock_file(root, path))
    if path.suffix.lower() == ".arrow":
        scan.arrow_files += 1
    if _is_evidence_name(lower_name):
        scan.evidence_files.append(path)
    if _is_oracle_name(lower_name):
        scan.oracle_files.append(path)


def _classify_unit(
    root: Path,
    scan: _UnitScan,
    *,
    explicitly_preserved: bool,
    global_runtime_blocker: str | None,
) -> dict[str, Any]:
    active_pids = [item for item in scan.pid_files if item["status"] == "active"]
    active_locks = [
        item for item in scan.lock_files if item["status"] in {"active", "unknown"}
    ]
    identities: list[dict[str, Any]] = []
    identity_complete: bool | None = None

    if active_pids or active_locks:
        category = "active_run_checkpoint"
        deletable = False
        reason = "active PID or lock protects this run and its checkpoints"
    elif scan.special_files:
        category = "unclassified_protected"
        deletable = False
        reason = "special filesystem entries are never deletion candidates"
    elif scan.evidence_files:
        identities, identity_complete = _evidence_identities(root, scan)
        category = "release_certificate_bundle"
        deletable = False
        reason = (
            "release or certification evidence is immutable"
            if identity_complete
            else "certification identity is incomplete; protected fail-closed"
        )
    elif scan.oracle_files or _contains_official_reference_run(scan.run_files):
        identities = _identity_records(root, [*scan.oracle_files, *scan.run_files])
        identity_complete = bool(identities)
        category = "official_oracle_freqtrade_zip"
        deletable = False
        reason = "official Oracle and Freqtrade ZIP evidence is immutable"
    elif explicitly_preserved or scan.preserve_markers:
        identities = _identity_records(root, scan.preserve_markers)
        identity_complete = True
        category = "user_preserved_run"
        deletable = False
        reason = "user preservation marker or --preserve selection"
    else:
        run_status = _run_status(scan.run_files)
        if run_status["complete"]:
            identities = _identity_records(root, scan.run_files)
            identity_complete = True
            category = "user_preserved_run"
            deletable = False
            reason = "completed run evidence is preserved by default"
        elif run_status["interrupted_or_failed"]:
            category = "interrupted_failed_run"
            deletable = True
            reason = "run is incomplete, interrupted, or failed"
        elif _is_regenerable_cache(scan):
            category = "regenerable_vector_cache"
            deletable = True
            reason = "content is a regenerable vector or cache artifact"
        elif _is_temporary_spool(scan):
            category = "temporary_arrow_docker_spool"
            deletable = True
            reason = "content is temporary Arrow or Docker spool data"
        elif _is_old_build_or_calibration(scan):
            category = "old_build_calibration"
            deletable = True
            reason = "content is a rebuildable build or calibration artifact"
        else:
            category = "unclassified_protected"
            deletable = False
            reason = "identity is not sufficient to classify this entry as reclaimable"

    if global_runtime_blocker is not None and deletable:
        deletable = False
        reason = global_runtime_blocker
    if scan.internal_symlinks:
        deletable = False
        reason = "internal symlinks are never deletion candidates"
    return {
        "path": scan.relative_path,
        "category": category,
        "deletable": deletable,
        "protection_reason": reason,
        "file_count": scan.file_count,
        "logical_bytes": scan.logical_bytes,
        "allocated_bytes": scan.allocated_bytes,
        "reclaimable_logical_bytes": scan.logical_bytes if deletable else 0,
        "reclaimable_allocated_bytes": scan.allocated_bytes if deletable else 0,
        "identity_complete": identity_complete,
        "evidence_identity": identities,
        "active_pids": active_pids,
        "active_locks": active_locks,
        "internal_symlinks": scan.internal_symlinks,
        "special_files": scan.special_files,
    }


def _evidence_identities(
    root: Path,
    scan: _UnitScan,
) -> tuple[list[dict[str, Any]], bool]:
    identities = _identity_records(root, scan.evidence_files)
    complete = True
    names = {path.name.lower(): path for path in scan.evidence_files}
    for path in scan.evidence_files:
        lower = path.name.lower()
        if path.suffix.lower() != ".json":
            continue
        try:
            document = read_json(path)
        except (OSError, ValueError):
            complete = False
            continue
        if not isinstance(document, dict) or not isinstance(
            document.get("schema_version"), str
        ):
            complete = False
            continue
        try:
            if lower == "full-x7-certification.json":
                schema = (
                    FULL_X7_CERTIFICATION_V2_SCHEMA
                    if document["schema_version"] == "2.0.0"
                    else FULL_X7_CERTIFICATION_SCHEMA
                )
                validate_schema(document, schema)
            elif lower == "certification-report.json":
                validate_schema(document, CERTIFICATION_REPORT_SCHEMA)
            elif lower == "bundle.json":
                _validate_bundle_identity(path.parent, document)
        except (BenchmarkError, SpecValidationError):
            complete = False
    if "bundle-manifest.json" in names and "bundle.json" not in names:
        complete = False
    if "bundle.json" in names and "bundle-manifest.json" not in names:
        complete = False
    if any(path.suffix.lower() == ".zip" for path in scan.evidence_files) and not any(
        path.suffix.lower() == ".json" for path in scan.evidence_files
    ):
        complete = False
    return identities, bool(identities) and complete


def _validate_bundle_identity(directory: Path, document: Mapping[str, Any]) -> None:
    if (
        document.get("schema_version") != "1.0.0"
        or not isinstance(document.get("evidence_id"), str)
    ):
        raise SpecValidationError("evidence bundle identity is incomplete")
    for key in ("archive", "manifest"):
        record = document.get(key)
        if not isinstance(record, Mapping):
            raise SpecValidationError(f"evidence bundle is missing {key}")
        relative = record.get("path")
        expected_bytes = record.get("bytes")
        expected_sha = record.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_bytes, int)
            or not isinstance(expected_sha, str)
        ):
            raise SpecValidationError(f"evidence bundle {key} identity is incomplete")
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise SpecValidationError(f"evidence bundle {key} path is invalid")
        if target.stat().st_size != expected_bytes or sha256_file(target) != expected_sha:
            raise SpecValidationError(f"evidence bundle {key} identity differs")


def _identity_records(root: Path, paths: Sequence[Path]) -> list[dict[str, Any]]:
    records = []
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def _contains_official_reference_run(paths: Sequence[Path]) -> bool:
    for path in paths:
        try:
            document = read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        reference = document.get("reference")
        if (
            isinstance(reference, dict)
            and isinstance(reference.get("image_index_digest"), str)
            and isinstance(reference.get("image_platform_digest"), str)
        ):
            return True
    return False


def _run_status(paths: Sequence[Path]) -> dict[str, bool]:
    complete = False
    interrupted_or_failed = False
    for path in paths:
        try:
            document = read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        complete = complete or document.get("complete") is True
        status = document.get("status")
        interrupted_or_failed = interrupted_or_failed or (
            document.get("complete") is False
            or isinstance(status, str)
            and status.lower() in _FAILED_STATUSES
        )
    return {
        "complete": complete,
        "interrupted_or_failed": interrupted_or_failed and not complete,
    }


def _is_regenerable_cache(scan: _UnitScan) -> bool:
    return bool(scan.segments & _CACHE_SEGMENTS) and not scan.run_files


def _is_temporary_spool(scan: _UnitScan) -> bool:
    return scan.arrow_files > 0 or bool(scan.segments & _TEMP_SEGMENTS)


def _is_old_build_or_calibration(scan: _UnitScan) -> bool:
    return any(
        segment in _BUILD_SEGMENTS
        or segment.startswith("build-")
        or segment.startswith("calibration-")
        for segment in scan.segments
    )


def _is_evidence_name(name: str) -> bool:
    return (
        name.endswith((".json", ".zip"))
        and any(part in name for part in _EVIDENCE_NAME_PARTS)
    )


def _is_oracle_name(name: str) -> bool:
    return name.endswith(".zip") and (
        "freqtrade" in name or "official-oracle" in name or "oracle-evidence" in name
    )


def _inspect_pid_file(root: Path, path: Path) -> dict[str, Any]:
    try:
        value = path.read_text(encoding="utf-8").strip()
        pid = int(value)
    except (OSError, UnicodeError, ValueError):
        return {
            "path": path.relative_to(root).as_posix(),
            "pid": None,
            "status": "invalid",
        }
    return {
        "path": path.relative_to(root).as_posix(),
        "pid": pid,
        "status": "active" if _pid_active(pid) else "stale",
    }


def _pid_active(pid: int) -> bool:
    return pid > 0 and psutil.pid_exists(pid)


def _inspect_lock_file(root: Path, path: Path) -> dict[str, Any]:
    status = "unknown"
    if os.name == "posix":
        try:
            import fcntl

            with path.open("rb") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    status = "active"
                else:
                    status = "stale"
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            status = "unknown"
    return {
        "path": path.relative_to(root).as_posix(),
        "status": status,
    }


def _probe_runtime_activity() -> dict[str, Any]:
    return {
        "services": _probe_services(),
        "containers": _probe_containers(),
    }


def _unprobed_runtime_activity() -> dict[str, Any]:
    return {
        "services": {
            "status": "not_requested",
            "active": [],
            "detail": None,
        },
        "containers": {
            "status": "not_requested",
            "active": [],
            "detail": None,
        },
    }


def _probe_services() -> dict[str, Any]:
    executable = shutil.which("systemctl")
    if executable is None:
        return {"status": "unavailable", "active": [], "detail": "systemctl not found"}
    return _run_activity_command(
        [
            executable,
            "--user",
            "list-units",
            "--type=service",
            "--state=running",
            "--no-legend",
            "--no-pager",
            "nfi-*",
        ],
        item_kind="service",
    )


def _probe_containers() -> dict[str, Any]:
    executable = shutil.which("docker")
    if executable is None:
        return {"status": "unavailable", "active": [], "detail": "docker not found"}
    return _run_activity_command(
        [
            executable,
            "ps",
            "--filter",
            "label=io.nfi-backtest-engine.managed=true",
            "--format",
            "{{.ID}}\t{{.Names}}",
        ],
        item_kind="container",
    )


def _run_activity_command(command: list[str], *, item_kind: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "active": [], "detail": str(exc)}
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "probe failed"
        return {"status": "unavailable", "active": [], "detail": detail[:500]}
    active = []
    for line in completed.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        active.append({"kind": item_kind, "identity": value[:500]})
    return {"status": "available", "active": active, "detail": None}


def _validate_runtime_probe(document: Mapping[str, Any]) -> None:
    for key in ("services", "containers"):
        value = document.get(key)
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("status"), str)
            or not isinstance(value.get("active"), list)
        ):
            raise SpecValidationError(f"activity probe is missing valid {key}")


def _runtime_blocker(document: Mapping[str, Any]) -> str | None:
    services = document["services"]
    containers = document["containers"]
    if services["active"] or containers["active"]:
        return "an active NFI service or managed Docker container blocks reclamation"
    if services["status"] != "available" or containers["status"] != "available":
        return "runtime activity probes are unavailable or were skipped; protected fail-closed"
    return None


def _category_totals(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for name in CATEGORIES:
        selected = [entry for entry in entries if entry["category"] == name]
        result.append(
            {
                "category": name,
                "unit_count": len(selected),
                "file_count": sum(int(entry["file_count"]) for entry in selected),
                "logical_bytes": sum(int(entry["logical_bytes"]) for entry in selected),
                "allocated_bytes": sum(int(entry["allocated_bytes"]) for entry in selected),
                "reclaimable_logical_bytes": sum(
                    int(entry["reclaimable_logical_bytes"]) for entry in selected
                ),
                "reclaimable_allocated_bytes": sum(
                    int(entry["reclaimable_allocated_bytes"]) for entry in selected
                ),
            }
        )
    return result


def _allocated_bytes(metadata: os.stat_result) -> int:
    blocks = getattr(metadata, "st_blocks", None)
    return int(blocks) * 512 if isinstance(blocks, int) else metadata.st_size
