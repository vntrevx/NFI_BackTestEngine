"""Two-stage creation of immutable benchmark fixture v2 directories."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .branch_coverage import (
    COVERAGE_REPORT_VERSION,
    configured_protection_methods,
    derive_fixture_observed,
)
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
    "reference_market_metadata",
    "auxiliary",
}
CaptureInput = tuple[str, str | Path] | tuple[str, str | Path, str]


def stage_fixture_v2(
    root: str | Path,
    *,
    fixture_id: str,
    description: str,
    fixture_kind: str,
    strategy: str | Path,
    config: str | Path,
    inputs: list[CaptureInput],
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
    for input_record in inputs:
        role, source_value = input_record[:2]
        if role not in INPUT_ROLES:
            raise SpecValidationError(f"unsupported capture input role: {role}")
        source = Path(source_value)
        index = role_counts.get(role, 0)
        role_counts[role] = index + 1
        if len(input_record) == 3:
            relative = _capture_relative_path(input_record[2])
        else:
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


def stage_fixture_v3(
    root: str | Path,
    *,
    fixture_id: str,
    description: str,
    probe_kind: str,
    strategy_provenance: dict[str, Any],
    required_coverage: dict[str, Any],
    strategy: str | Path,
    config: str | Path,
    inputs: list[CaptureInput],
) -> dict[str, Any]:
    """Stage immutable X7 probe inputs and bind source provenance before execution."""
    stage = stage_fixture_v2(
        root,
        fixture_id=fixture_id,
        description=description,
        fixture_kind="x7-branch-probe",
        strategy=strategy,
        config=config,
        inputs=inputs,
    )
    stage.update(
        {
            "schema_version": "capture-stage-v2",
            "probe_kind": probe_kind,
            "strategy_provenance": strategy_provenance,
            "required_coverage": required_coverage,
        }
    )
    write_json(Path(root).resolve() / STAGE_FILE, stage)
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
    return _finalize_fixture(
        root,
        freqtrade_result=freqtrade_result,
        trade_surface=trade_surface,
        state_trace=state_trace,
        freqtrade=freqtrade,
        measurement=measurement,
        manifest_version="2.0.0",
    )


def finalize_fixture_v3(
    root: str | Path,
    *,
    freqtrade_result: str | Path,
    trade_surface: str | Path,
    state_trace: str | Path,
    freqtrade: dict[str, Any],
    measurement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize a v3 probe and compute branch coverage from official artifacts."""
    return _finalize_fixture(
        root,
        freqtrade_result=freqtrade_result,
        trade_surface=trade_surface,
        state_trace=state_trace,
        freqtrade=freqtrade,
        measurement=measurement,
        manifest_version="3.0.0",
    )


def _finalize_fixture(
    root: str | Path,
    *,
    freqtrade_result: str | Path,
    trade_surface: str | Path,
    state_trace: str | Path,
    freqtrade: dict[str, Any],
    measurement: dict[str, Any] | None,
    manifest_version: str,
) -> dict[str, Any]:
    destination = Path(root).resolve()
    stage_path = destination / STAGE_FILE
    if not stage_path.is_file():
        raise SpecValidationError(f"capture stage does not exist: {stage_path}")
    stage = read_json(stage_path)
    expected_stage_version = (
        "capture-stage-v2" if manifest_version == "3.0.0" else "capture-stage-v1"
    )
    if stage.get("schema_version") != expected_stage_version:
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
                f"captured trace {field} mismatch: expected {expected}, actual {trace[field]}"
            )

    manifest: dict[str, Any] = {
        "schema_version": manifest_version,
        "fixture_id": stage["fixture_id"],
        "description": stage["description"],
        "fixture_kind": stage["fixture_kind"],
        "evidence_status": "captured",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "freqtrade": freqtrade,
        "inputs": stage["inputs"],
        "artifacts": artifacts,
        "measurement": measurement or _default_measurement(),
    }
    if manifest_version == "3.0.0":
        coverage_path = destination / "artifacts/coverage-report.json"
        protection_methods = configured_protection_methods(
            destination
            / next(
                item["path"] for item in stage["inputs"] if item["role"] == "strategy"
            ),
            destination
            / next(
                item["path"] for item in stage["inputs"] if item["role"] == "config"
            ),
            class_name=freqtrade["strategy"],
        )
        coverage = {
            "schema_version": COVERAGE_REPORT_VERSION,
            "source": "official-freqtrade-observer",
            "bindings": {
                "trade_surface_sha256": artifacts["trade_surface"]["sha256"],
                "state_trace_sha256": artifacts["state_trace"]["sha256"],
            },
            "observed": derive_fixture_observed(
                surface_document,
                destination / artifacts["state_trace"]["path"],
                configured_protection_methods=protection_methods,
            ),
        }
        write_json(coverage_path, coverage)
        artifacts["coverage_report"] = {
            "path": "artifacts/coverage-report.json",
            "sha256": sha256_file(coverage_path),
            "bytes": coverage_path.stat().st_size,
        }
        manifest.update(
            {
                "probe_kind": stage["probe_kind"],
                "strategy_provenance": stage["strategy_provenance"],
                "required_coverage": stage["required_coverage"],
            }
        )
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


def _capture_relative_path(value: str) -> str:
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("inputs", "data")
    ):
        raise SpecValidationError(
            "explicit capture paths must remain below inputs/data"
        )
    return relative.as_posix()


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
