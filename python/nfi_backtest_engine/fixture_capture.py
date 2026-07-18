"""Two-stage creation of immutable benchmark fixture v2 directories."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import SpecValidationError
from .fixture import fixture_input_sha256, sha256_file, validate_fixture
from .specs import validate_fixture_manifest, validate_trade_surface
from .state_trace import trace_summary
from .trace_projection import project_reference_trace

STAGE_FILE = ".capture-stage.json"
INPUT_ROLES = {
    "candles",
    "detail_candles",
    "informative_candles",
    "funding_candles",
    "mark_candles",
    "pairlist",
    "market_metadata",
    "auxiliary",
}


def stage_fixture_v2(
    root: str | Path,
    *,
    fixture_id: str,
    description: str,
    fixture_kind: str,
    strategy: str | Path,
    config: str | Path,
    inputs: list[tuple[str, str | Path]],
) -> dict[str, Any]:
    """Copy immutable inputs first so the tracer can bind to their aggregate identity."""
    destination = Path(root).resolve()
    stage_path = destination / STAGE_FILE
    manifest_path = destination / "manifest.json"
    if stage_path.exists() or manifest_path.exists():
        raise SpecValidationError(f"fixture destination is already initialized: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    references = [
        _copy_input(Path(strategy), destination, "strategy", "inputs/strategy.py"),
        _copy_input(Path(config), destination, "config", "inputs/config.json"),
    ]
    role_counts: dict[str, int] = {}
    for role, source_value in inputs:
        if role not in INPUT_ROLES:
            raise SpecValidationError(f"unsupported capture input role: {role}")
        source = Path(source_value)
        index = role_counts.get(role, 0)
        role_counts[role] = index + 1
        filename = source.name if index == 0 else f"{index:03d}-{source.name}"
        relative = f"inputs/{role}/{filename}"
        references.append(_copy_input(source, destination, role, relative))

    if not any(item["role"] == "candles" for item in references):
        raise SpecValidationError("fixture capture requires at least one candles input")
    stage = {
        "schema_version": "capture-stage-v1",
        "fixture_id": fixture_id,
        "description": description,
        "fixture_kind": fixture_kind,
        "inputs": references,
        "input_sha256": fixture_input_sha256(references),
        "strategy_sha256": next(
            item["sha256"] for item in references if item["role"] == "strategy"
        ),
        "profile_sha256": next(item["sha256"] for item in references if item["role"] == "config"),
    }
    write_json(stage_path, stage)
    return stage


def finalize_fixture_v2(
    root: str | Path,
    *,
    freqtrade_result: str | Path,
    trade_surface: str | Path,
    state_trace: str | Path,
    freqtrade: dict[str, Any],
    measurement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy artifacts, write manifest v2, and validate every byte and trace binding."""
    destination = Path(root).resolve()
    stage_path = destination / STAGE_FILE
    if not stage_path.is_file():
        raise SpecValidationError(f"capture stage does not exist: {stage_path}")
    stage = read_json(stage_path)
    if stage.get("schema_version") != "capture-stage-v1":
        raise SpecValidationError(f"unsupported capture stage: {stage.get('schema_version')!r}")

    artifacts = {
        "freqtrade_result": _copy_artifact(
            Path(freqtrade_result), destination, "artifacts/freqtrade-result.zip"
        ),
        "trade_surface": _copy_artifact(
            Path(trade_surface), destination, "artifacts/trade-surface.json"
        ),
        "state_trace": _copy_artifact(
            Path(state_trace), destination, "artifacts/state-trace.nfitrace"
        ),
    }
    surface_document = read_json(destination / artifacts["trade_surface"]["path"])
    validate_trade_surface(surface_document)
    if surface_document["schema_version"] != "2.0.0":
        raise SpecValidationError("captured fixture v2 requires trade-surface v2")

    trace = trace_summary(destination / artifacts["state_trace"]["path"])
    expected_trace_header = {
        "input_sha256": stage["input_sha256"],
        "strategy_sha256": stage["strategy_sha256"],
        "profile_sha256": stage["profile_sha256"],
        "trading_mode": freqtrade["trading_mode"],
    }
    for field, expected in expected_trace_header.items():
        if trace[field] != expected:
            raise SpecValidationError(
                f"captured trace {field} mismatch: expected {expected}, "
                f"actual {trace[field]}"
            )

    manifest = {
        "schema_version": "2.0.0",
        "fixture_id": stage["fixture_id"],
        "description": stage["description"],
        "fixture_kind": stage["fixture_kind"],
        "evidence_status": "captured",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "freqtrade": freqtrade,
        "inputs": stage["inputs"],
        "artifacts": artifacts,
        "measurement": measurement or _default_measurement(),
    }
    validate_fixture_manifest(manifest)
    manifest_path = destination / "manifest.json"
    write_json(manifest_path, manifest)
    projection_path = destination / "artifacts/state-projection.nfitrace"
    project_reference_trace(
        manifest_path,
        projection_path,
        manifest=manifest,
    )
    artifacts["state_projection"] = {
        "path": "artifacts/state-projection.nfitrace",
        "sha256": sha256_file(projection_path),
        "bytes": projection_path.stat().st_size,
    }
    validate_fixture_manifest(manifest)
    write_json(manifest_path, manifest)
    stage_path.unlink()
    return validate_fixture(manifest_path)


def _copy_input(source: Path, root: Path, role: str, relative: str) -> dict[str, Any]:
    reference = _copy_file(source, root, relative)
    return {"role": role, **reference}


def _copy_artifact(source: Path, root: Path, relative: str) -> dict[str, Any]:
    return _copy_file(source, root, relative)


def _copy_file(source: Path, root: Path, relative: str) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file():
        raise SpecValidationError(f"capture source does not exist: {source}")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SpecValidationError(f"capture target already exists: {target}")
    shutil.copyfile(source, target)
    return {"path": relative, "sha256": sha256_file(target), "bytes": target.stat().st_size}


def _default_measurement() -> dict[str, Any]:
    return {
        "warmup_runs": 1,
        "measured_runs": 3,
        "poll_interval_ms": 100,
        "required_profile_phases": [
            "indicators",
            "callbacks",
            "trade_scans",
            "event_simulation",
        ],
    }
