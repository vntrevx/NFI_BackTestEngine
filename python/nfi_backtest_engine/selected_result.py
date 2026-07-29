"""Hash-bound selection of an official result after Native fail-closed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .reference_runtime import (
    REFERENCE_IMAGE,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
)
from .specs import validate_trade_surface

SELECTED_RESULT_VERSION = "1.0.0"
SELECTED_RESULT_FILENAME = "selected-result.json"


def write_official_selection(
    run_directory: str | Path,
    official_report_path: str | Path,
) -> dict[str, Any]:
    """Select one complete fallback result without mutating Native evidence."""

    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    report_path = Path(official_report_path).resolve()
    run = _read_object(run_path, "Native run")
    report = _read_object(report_path, "official fallback report")
    _validate_native_blocked_run(root, run)
    surface_path = _validate_official_fallback(root, run, report, report_path)
    blockers = _blockers(run)
    selection = {
        "schema_version": SELECTED_RESULT_VERSION,
        "run_id": run["run_id"],
        "selected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "native_status": run["status"],
        "selected_status": "official_complete",
        "selected_lane": "official",
        "exact_parity": None,
        "blocker_fingerprint": _canonical_sha256(blockers),
        "blockers": blockers,
        "source": {
            "run": _artifact_record(run_path, root),
            "identity": _artifact_record(root / "identity.json", root),
            "data_seal": _artifact_record(root / "data-seal.json", root),
        },
        "official": {
            "report": _artifact_record(report_path, root),
            "trade_surface": _artifact_record(surface_path, root),
        },
    }
    destination = root / SELECTED_RESULT_FILENAME
    if destination.is_file():
        existing = _read_object(destination, "selected result")
        comparable = dict(existing)
        comparable["selected_at"] = selection["selected_at"]
        if comparable != selection:
            raise BenchmarkError("selected-result.json already binds a different official result")
        return existing
    write_json(destination, selection)
    return selection


def load_selected_run_view(
    root: Path,
    run_report: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a derived official view when a valid selection exists."""

    selection_path = root / SELECTED_RESULT_FILENAME
    if not selection_path.is_file():
        return dict(run_report), None
    selection = _read_object(selection_path, "selected result")
    if selection.get("schema_version") != SELECTED_RESULT_VERSION:
        raise BenchmarkError("selected result has an unsupported schema version")
    if (
        selection.get("run_id") != run_report.get("run_id")
        or selection.get("native_status") != run_report.get("status")
        or selection.get("selected_status") != "official_complete"
        or selection.get("selected_lane") != "official"
        or selection.get("exact_parity") is not None
    ):
        raise BenchmarkError("selected result is not bound to this Native run")
    blockers = _blockers(run_report)
    if selection.get("blockers") != blockers or selection.get(
        "blocker_fingerprint"
    ) != _canonical_sha256(blockers):
        raise BenchmarkError("selected result blocker identity differs from Native")
    source = _mapping(selection, "source")
    _validate_artifact_record(
        _mapping(source, "run"),
        expected=root / "run.json",
        root=root,
    )
    _validate_artifact_record(
        _mapping(source, "identity"),
        expected=root / "identity.json",
        root=root,
    )
    _validate_artifact_record(
        _mapping(source, "data_seal"),
        expected=root / "data-seal.json",
        root=root,
    )
    official = _mapping(selection, "official")
    report_path = _validate_artifact_record(
        _mapping(official, "report"),
        expected=None,
        root=root,
    )
    surface_path = _validate_artifact_record(
        _mapping(official, "trade_surface"),
        expected=None,
        root=root,
    )
    report = _read_object(report_path, "official fallback report")
    _validate_official_fallback(root, dict(run_report), report, report_path)
    surface = _read_object(surface_path, "official fallback trade surface")
    validate_trade_surface(surface)
    trades = surface.get("trades")
    if not isinstance(trades, list):
        raise BenchmarkError("official fallback trade surface trades are invalid")

    view = dict(run_report)
    view["status"] = "official_complete"
    view["complete"] = True
    view["native_status"] = run_report.get("status")
    view["selected_status"] = "official_complete"
    view["selected_lane"] = "official"
    execution = dict(_mapping(run_report, "execution"))
    execution["lane"] = "official"
    execution["official_report"] = str(report_path)
    execution["official_wall_time_seconds"] = report.get("wall_time_seconds")
    view["execution"] = execution
    view["result"] = {
        "trade_count": len(trades),
        "execution": {
            "lane": "official",
            "reference": report.get("reference"),
        },
        "trade_surface": _absolute_artifact_record(surface_path),
    }
    view["official_confirmation"] = {
        "required_for_finalist": True,
        "status": "official_only",
        "exact_parity": None,
        "report": str(report_path),
        "report_sha256": sha256_file(report_path),
    }
    return view, selection


def validate_official_fallback(
    run_directory: str | Path,
    official_report_path: str | Path,
) -> dict[str, Any]:
    """Validate one fallback proof and return its normalized report."""

    root = Path(run_directory).resolve()
    run = _read_object(root / "run.json", "Native run")
    report_path = Path(official_report_path).resolve()
    report = _read_object(report_path, "official fallback report")
    _validate_native_blocked_run(root, run)
    _validate_official_fallback(root, run, report, report_path)
    return report


def _validate_native_blocked_run(root: Path, run: Mapping[str, Any]) -> None:
    if (
        run.get("status") != "blocked_unsupported_semantics"
        or run.get("complete") is not False
        or not isinstance(run.get("run_id"), str)
        or not _blockers(run)
    ):
        raise BenchmarkError("official result selection requires unsupported Native semantics")
    identity = _read_object(root / "identity.json", "sealed run identity")
    inputs = run.get("inputs")
    if (
        not isinstance(inputs, dict)
        or identity.get("identity") != inputs
        or identity.get("run_id") != run["run_id"]
        or _canonical_sha256(inputs) != run["run_id"]
    ):
        raise BenchmarkError("Native run identity failed its hash binding")


def _validate_official_fallback(
    root: Path,
    run: Mapping[str, Any],
    report: Mapping[str, Any],
    report_path: Path,
) -> Path:
    if (
        report.get("purpose") != "fallback"
        or report.get("run_id") != run.get("run_id")
        or report.get("complete") is not True
        or report.get("exact_parity") is not None
        or report.get("exit_code") != 0
        or report.get("timed_out") is not False
    ):
        raise BenchmarkError("official fallback did not complete successfully")
    reference = report.get("reference")
    expected_reference = {
        "version": REFERENCE_VERSION,
        "image": REFERENCE_IMAGE,
        "image_index_digest": REFERENCE_INDEX_DIGEST,
        "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
        "platform": REFERENCE_PLATFORM,
        "network": "none",
    }
    if not isinstance(reference, Mapping) or any(
        reference.get(key) != value for key, value in expected_reference.items()
    ):
        raise BenchmarkError("official fallback reference identity is not pinned")
    inputs = report.get("inputs")
    data_seal = inputs.get("data_seal") if isinstance(inputs, Mapping) else None
    if not isinstance(data_seal, Mapping):
        raise BenchmarkError("official fallback data seal identity is missing")
    _validate_absolute_record(
        data_seal,
        expected=root / "data-seal.json",
        label="official fallback data seal",
    )
    storage = report.get("reference_storage")
    if (
        not isinstance(storage, Mapping)
        or storage.get("mode") != "spooled"
        or storage.get("complete") is not True
    ):
        raise BenchmarkError("official fallback spooled storage is incomplete")
    if not report_path.is_relative_to(root):
        raise BenchmarkError("official fallback report is outside the run directory")
    record = report.get("official_trade_surface")
    if not isinstance(record, Mapping):
        raise BenchmarkError("official fallback has no trade surface")
    path_value = record.get("path")
    if not isinstance(path_value, str):
        raise BenchmarkError("official fallback trade surface path is invalid")
    path = Path(path_value).resolve()
    if (
        path != report_path.parent / "official-trade-surface.json"
        or not path.is_file()
        or record.get("sha256") != sha256_file(path)
        or record.get("bytes") != path.stat().st_size
    ):
        raise BenchmarkError("official fallback trade surface failed its hash binding")
    surface = _read_object(path, "official fallback trade surface")
    validate_trade_surface(surface)
    return path


def _validate_absolute_record(
    record: Mapping[str, Any],
    *,
    expected: Path,
    label: str,
) -> None:
    target = expected.resolve()
    if (
        not isinstance(record.get("path"), str)
        or Path(str(record["path"])).resolve() != target
        or not target.is_file()
        or record.get("bytes") != target.stat().st_size
        or record.get("sha256") != sha256_file(target)
    ):
        raise BenchmarkError(f"{label} failed its hash binding")


def _blockers(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    capability = run.get("capability")
    values = capability.get("blockers") if isinstance(capability, Mapping) else None
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _artifact_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise BenchmarkError(f"selected result artifact is unavailable: {resolved}")
    return {
        "path": resolved.relative_to(root).as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _absolute_artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _validate_artifact_record(
    record: Mapping[str, Any],
    *,
    expected: Path | None,
    root: Path,
) -> Path:
    path_value = record.get("path")
    if not isinstance(path_value, str):
        raise BenchmarkError("selected result artifact path is invalid")
    path = (root / path_value).resolve()
    if (
        not path.is_relative_to(root)
        or (expected is not None and path != expected.resolve())
        or not path.is_file()
        or record.get("bytes") != path.stat().st_size
        or record.get("sha256") != sha256_file(path)
    ):
        raise BenchmarkError("selected result artifact failed its hash binding")
    return path


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkError(f"{label} does not exist: {path}")
    value = read_json(path)
    if not isinstance(value, dict):
        raise BenchmarkError(f"{label} must be an object: {path}")
    return value


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    candidate = value.get(key)
    return dict(candidate) if isinstance(candidate, Mapping) else {}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
