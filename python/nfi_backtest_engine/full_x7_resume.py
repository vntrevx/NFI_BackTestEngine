"""Checkpoint and oracle-import support for long Full X7 certifications."""

from __future__ import annotations

import copy
import json
import shutil
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .config_loader import config_sha256
from .errors import BenchmarkError
from .fixture import sha256_file
from .reference_runtime import REFERENCE_PLATFORM, REFERENCE_PLATFORM_DIGEST
from .timerange import parse_timerange_milliseconds

Measurement = dict[str, Any]
MeasurementValidator = Callable[[Measurement], bool]
CHECKPOINT_FILENAME = "certification-measurement.json"


def write_measurement_checkpoint(
    output: Path,
    measurement: Measurement,
) -> None:
    """Persist process-tree values that the inner run report cannot rebuild."""
    write_json(
        output / CHECKPOINT_FILENAME,
        {
            "schema_version": "1.0.0",
            "wall_time_seconds": float(measurement["wall_time_seconds"]),
            "peak_rss_bytes": int(measurement["peak_rss_bytes"]),
            "exit_code": int(measurement["exit_code"]),
            "timed_out": bool(measurement["timed_out"]),
        },
    )


def load_engine_measurement(
    output: Path,
    *,
    validator: MeasurementValidator,
    allow_report_fallback: bool,
) -> Measurement | None:
    """Restore one native stage without discarding process-tree RSS evidence."""
    report_path = output / "run.json"
    if not report_path.is_file():
        return None
    report = read_json(report_path)
    checkpoint_path = output / CHECKPOINT_FILENAME
    checkpoint = read_json(checkpoint_path) if checkpoint_path.is_file() else None
    if checkpoint is None and not allow_report_fallback:
        raise BenchmarkError(
            "native certification stage has a run report but no process measurement "
            f"checkpoint: {output}"
        )
    measurement: Measurement = {
        "wall_time_seconds": (
            float(checkpoint["wall_time_seconds"])
            if checkpoint is not None
            else float(report["timings"]["pipeline_wall_time_seconds"])
        ),
        "peak_rss_bytes": (
            int(checkpoint["peak_rss_bytes"])
            if checkpoint is not None
            else _persisted_engine_peak(report)
        ),
        "exit_code": int(checkpoint["exit_code"]) if checkpoint is not None else 0,
        "timed_out": bool(checkpoint["timed_out"]) if checkpoint is not None else False,
        "stdout": _existing_stream(output.parent / f"{output.name}.stdout.log"),
        "stderr": _existing_stream(output.parent / f"{output.name}.stderr.log"),
        "report": report,
        "output_directory": output,
        "result_sha256": _engine_surface_sha(report),
    }
    if not validator(measurement):
        raise BenchmarkError(f"native certification stage is incomplete: {output}")
    return measurement


def load_reference_measurement(output: Path) -> Measurement | None:
    """Restore one official reference from its self-contained run report."""
    report_path = output / "run.json"
    if not report_path.is_file():
        return None
    report = read_json(report_path)
    checkpoint_path = output / CHECKPOINT_FILENAME
    checkpoint = read_json(checkpoint_path) if checkpoint_path.is_file() else None
    memory = report.get("container_memory")
    container_peak = memory.get("peak_bytes") if isinstance(memory, dict) else None
    return {
        "wall_time_seconds": (
            float(checkpoint["wall_time_seconds"])
            if checkpoint is not None
            else float(report["wall_time_seconds"])
        ),
        "peak_rss_bytes": (
            int(checkpoint["peak_rss_bytes"])
            if checkpoint is not None
            else int(container_peak or 0)
        ),
        "exit_code": (
            int(checkpoint["exit_code"]) if checkpoint is not None else int(report["exit_code"])
        ),
        "timed_out": (
            bool(checkpoint["timed_out"]) if checkpoint is not None else bool(report["timed_out"])
        ),
        "stdout": _existing_stream(output / "stdout.log"),
        "stderr": _existing_stream(output / "stderr.log"),
        "report": report,
        "output_directory": output,
        "result_sha256": _reference_surface_sha(report),
    }


def import_reference_oracle(
    source_directory: str | Path,
    output: Path,
    *,
    baseline: Measurement,
    inputs: dict[str, Any],
    validator: MeasurementValidator,
) -> Measurement:
    """Copy one official oracle after binding it to the current native run.

    A long official run can finish before a native parity defect is corrected.
    In that case its immutable Freqtrade ZIP and normalized official surface
    remain valid evidence.  Reconciliation is permitted only when every
    immutable identity matches and the official surface already equals the new
    cold native baseline.
    """
    source = Path(source_directory).resolve()
    source_measurement = load_reference_measurement(source)
    if source_measurement is None:
        raise BenchmarkError(f"official oracle has no run report: {source}")
    validate_reconcilable_reference_oracle(
        source_measurement,
        source_directory=source,
        baseline=baseline,
        inputs=inputs,
    )
    source_report_sha = sha256_file(source / "run.json")
    shutil.copytree(source, output)
    _materialize_external_identity_artifacts(
        output,
        source_report_sha=source_report_sha,
    )
    imported = load_reference_measurement(output)
    if imported is None:
        raise BenchmarkError("copied official oracle lost its run report")
    if not validator(imported):
        _reconcile_reference_parity(
            output,
            imported,
            baseline=baseline,
        )
        imported = load_reference_measurement(output)
        if imported is None:
            raise BenchmarkError("reconciled official oracle lost its run report")
    validate_reference_oracle(
        imported,
        baseline=baseline,
        inputs=inputs,
        validator=validator,
    )
    return imported


def validate_reconcilable_reference_oracle(
    measurement: Measurement,
    *,
    source_directory: Path,
    baseline: Measurement,
    inputs: dict[str, Any],
) -> None:
    """Validate immutable oracle bytes before parity metadata may be rebound."""
    report = measurement.get("report")
    if not isinstance(report, dict):
        raise BenchmarkError("imported official oracle has no structured run report")
    memory = report.get("container_memory")
    storage = report.get("reference_storage")
    runtime_complete = bool(
        measurement.get("exit_code") == 0
        and report.get("exit_code") == 0
        and report.get("timed_out") is False
        and isinstance(memory, dict)
        and memory.get("verdict") not in {"oom_killed", "possible_oom"}
        and isinstance(storage, dict)
        and storage.get("complete") is True
    )
    if not runtime_complete:
        raise BenchmarkError("imported official oracle did not finish safely")
    _validate_reference_identity(
        measurement,
        baseline=baseline,
        inputs=inputs,
        require_engine_surface=False,
    )
    report_inputs = report.get("inputs")
    for label, record in (
        (
            "strategy input",
            report_inputs.get("strategy") if isinstance(report_inputs, dict) else None,
        ),
        (
            "market snapshot",
            (report_inputs.get("market_snapshot") if isinstance(report_inputs, dict) else None),
        ),
        ("Freqtrade result", report.get("result")),
        ("official trade surface", report.get("official_trade_surface")),
    ):
        if not _artifact_matches_directory(source_directory, record):
            raise BenchmarkError(f"imported official oracle {label} bytes differ")
    official_sha = measurement.get("result_sha256")
    if official_sha != baseline.get("result_sha256"):
        raise BenchmarkError("imported official surface differs from the current native baseline")


def validate_reference_oracle(
    measurement: Measurement,
    *,
    baseline: Measurement,
    inputs: dict[str, Any],
    validator: MeasurementValidator,
) -> None:
    """Fail closed when imported official evidence belongs to another run."""
    if not validator(measurement):
        raise BenchmarkError("imported official oracle is incomplete or not exact")
    _validate_reference_identity(
        measurement,
        baseline=baseline,
        inputs=inputs,
        require_engine_surface=True,
    )


def _validate_reference_identity(
    measurement: Measurement,
    *,
    baseline: Measurement,
    inputs: dict[str, Any],
    require_engine_surface: bool,
) -> None:
    report = measurement["report"]
    baseline_sha = baseline["result_sha256"]
    reference = report.get("reference")
    report_inputs = report.get("inputs")
    engine_surface = (
        report_inputs.get("engine_trade_surface") if isinstance(report_inputs, dict) else None
    )
    strategy = report_inputs.get("strategy") if isinstance(report_inputs, dict) else None
    market = report_inputs.get("market_snapshot") if isinstance(report_inputs, dict) else None
    official_surface = report.get("official_trade_surface")
    output = Path(str(measurement.get("output_directory", ""))).resolve()
    expected_market_sha = inputs["public"]["reference_market_snapshot_sha256"]
    checks = {
        "strategy": isinstance(strategy, dict)
        and strategy.get("sha256") == inputs["public"]["strategy_sha256"],
        "official config": _official_config_matches(
            output,
            report_inputs,
            inputs=inputs,
        ),
        "candle data seal": _data_seal_matches(
            output,
            report_inputs,
            expected_aggregate_sha=inputs["public"]["data_aggregate_sha256"],
            expected_schema_version=inputs["lock"]["data"]["seal_version"],
        ),
        "official timerange": _official_result_scope_matches(
            output,
            report,
            inputs=inputs,
        ),
        "official surface": isinstance(official_surface, dict)
        and official_surface.get("sha256") == baseline_sha,
        "reference image": isinstance(reference, dict)
        and reference.get("image_platform_digest") == REFERENCE_PLATFORM_DIGEST,
        "reference platform": isinstance(reference, dict)
        and reference.get("platform") == REFERENCE_PLATFORM,
        "market snapshot": expected_market_sha is None
        or (isinstance(market, dict) and market.get("sha256") == expected_market_sha),
    }
    if require_engine_surface:
        checks["engine surface"] = (
            isinstance(engine_surface, dict) and engine_surface.get("sha256") == baseline_sha
        )
    failed = [name for name, equal in checks.items() if not equal]
    if failed:
        raise BenchmarkError(
            "imported official oracle differs from the current certification: " + ", ".join(failed)
        )


def _reconcile_reference_parity(
    output: Path,
    measurement: Measurement,
    *,
    baseline: Measurement,
) -> None:
    """Rebind only comparison metadata; retain every official artifact byte."""
    report_path = output / "run.json"
    source_report_sha = sha256_file(report_path)
    report = copy.deepcopy(measurement["report"])
    baseline_report = baseline["report"]
    baseline_result = baseline_report.get("result")
    baseline_surface = (
        baseline_result.get("trade_surface") if isinstance(baseline_result, dict) else None
    )
    if not isinstance(baseline_surface, dict):
        raise BenchmarkError("native baseline has no trade-surface artifact record")
    baseline_surface_path = Path(str(baseline_surface.get("path", "")))
    if not baseline_surface_path.is_file() or baseline_surface.get("sha256") != sha256_file(
        baseline_surface_path
    ):
        raise BenchmarkError("native baseline trade-surface bytes differ")
    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise BenchmarkError("official oracle has no input identity record")
    prior_engine = inputs.get("engine_trade_surface")
    inputs["engine_trade_surface"] = dict(baseline_surface)
    report["complete"] = True
    report["exact_parity"] = True
    report["difference"] = None
    report["parity_reconciliation"] = {
        "schema_version": "1.0.0",
        "reconciled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_run_report_sha256": (
            report.get("oracle_import", {}).get("source_run_report_sha256")
            if isinstance(report.get("oracle_import"), dict)
            else source_report_sha
        ),
        "official_run_id": report.get("run_id"),
        "native_run_id": baseline_report.get("run_id"),
        "prior_engine_surface_sha256": (
            prior_engine.get("sha256") if isinstance(prior_engine, dict) else None
        ),
        "engine_surface_sha256": baseline["result_sha256"],
        "official_surface_sha256": measurement["result_sha256"],
        "official_result_sha256": report["result"]["sha256"],
    }
    write_json(report_path, report)


def _materialize_external_identity_artifacts(
    output: Path,
    *,
    source_report_sha: str,
) -> None:
    """Make a copied oracle independent from the machine that produced it."""
    report_path = output / "run.json"
    report = read_json(report_path)
    inputs = report.get("inputs")
    data_seal = inputs.get("data_seal") if isinstance(inputs, dict) else None
    source = _verified_artifact_path(output, data_seal)
    if source is None:
        raise BenchmarkError("imported official oracle candle data seal bytes differ")
    destination = output / "inputs" / "data-seal.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_file(destination) != sha256_file(source):
        raise BenchmarkError("imported official oracle data-seal destination differs")
    if not destination.exists():
        shutil.copyfile(source, destination)
    assert isinstance(inputs, dict)
    inputs["data_seal"] = {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }
    report["oracle_import"] = {
        "schema_version": "1.0.0",
        "source_run_report_sha256": source_report_sha,
    }
    write_json(report_path, report)


def _official_config_matches(
    directory: Path,
    report_inputs: Any,
    *,
    inputs: dict[str, Any],
) -> bool:
    if not isinstance(report_inputs, dict):
        return False
    config_path = _verified_artifact_path(directory, report_inputs.get("config"))
    if config_path is None:
        return False
    try:
        config = read_json(config_path)
    except (OSError, ValueError):
        return False
    if not isinstance(config, dict):
        return False
    expected_sha = inputs["public"].get("official_reference_config_sha256")
    exchange = config.get("exchange")
    pairs = exchange.get("pair_whitelist") if isinstance(exchange, dict) else None
    return bool(
        isinstance(expected_sha, str)
        and config_sha256(config) == expected_sha
        and pairs == inputs["lock"]["pairlist"]["pairs"]
    )


def _data_seal_matches(
    directory: Path,
    report_inputs: Any,
    *,
    expected_aggregate_sha: Any,
    expected_schema_version: Any,
) -> bool:
    if not isinstance(report_inputs, dict):
        return False
    seal_path = _verified_artifact_path(directory, report_inputs.get("data_seal"))
    if seal_path is None:
        return False
    try:
        seal = read_json(seal_path)
    except (OSError, ValueError):
        return False
    return bool(
        isinstance(seal, dict)
        and isinstance(expected_aggregate_sha, str)
        and isinstance(expected_schema_version, str)
        and seal.get("schema_version") == expected_schema_version
        and seal.get("aggregate_sha256") == expected_aggregate_sha
    )


def _official_result_scope_matches(
    directory: Path,
    report: dict[str, Any],
    *,
    inputs: dict[str, Any],
) -> bool:
    result_path = _verified_artifact_path(directory, report.get("result"))
    if result_path is None:
        return False
    try:
        with zipfile.ZipFile(result_path) as archive:
            meta_members = [name for name in archive.namelist() if name.endswith(".meta.json")]
            if len(meta_members) == 1:
                metadata = json.loads(archive.read(meta_members[0]))
            elif not meta_members:
                result_members = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".json") and not name.endswith("_config.json")
                ]
                if len(result_members) != 1:
                    return False
                result = json.loads(archive.read(result_members[0]))
                metadata = result.get("strategy") if isinstance(result, dict) else None
            else:
                return False
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    class_name = inputs["lock"]["strategy"]["class_name"]
    strategy = metadata.get(class_name) if isinstance(metadata, dict) else None
    if not isinstance(strategy, dict):
        return False
    start_ms, end_ms = parse_timerange_milliseconds(inputs["lock"]["scope"]["timerange"])
    timeframes = inputs["lock"]["scope"]["timeframes"]
    return bool(
        _timestamp_matches(strategy.get("backtest_start_ts"), start_ms)
        and _timestamp_matches(strategy.get("backtest_end_ts"), end_ms)
        and isinstance(timeframes, list)
        and timeframes
        and strategy.get("timeframe") == timeframes[0]
    )


def _timestamp_matches(value: Any, expected_ms: int) -> bool:
    return bool(
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value == expected_ms or value * 1000 == expected_ms)
    )


def _verified_artifact_path(directory: Path, record: Any) -> Path | None:
    if not isinstance(record, dict):
        return None
    raw_path = record.get("path")
    expected_sha = record.get("sha256")
    expected_bytes = record.get("bytes")
    if (
        not isinstance(raw_path, str)
        or not isinstance(expected_sha, str)
        or not isinstance(expected_bytes, int)
    ):
        return None
    filename = Path(raw_path).name
    local_matching = [
        path
        for path in directory.rglob(filename)
        if path.is_file()
        if path.stat().st_size == expected_bytes and sha256_file(path) == expected_sha
    ]
    if len(local_matching) == 1:
        return local_matching[0]
    if local_matching:
        return None
    raw = Path(raw_path)
    if raw.is_file() and raw.stat().st_size == expected_bytes and sha256_file(raw) == expected_sha:
        return raw
    return None


def _artifact_matches_directory(directory: Path, record: Any) -> bool:
    candidate = _verified_artifact_path(directory, record)
    return candidate is not None and candidate.is_relative_to(directory)


def require_stage_available(output: Path, *, stage: str) -> None:
    """Protect complete or partial stage artifacts from accidental overwrite."""
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(
            f"{stage} output is partial; resume it or choose a new output directory: {output}"
        )


def _persisted_engine_peak(report: dict[str, Any]) -> int:
    peaks: list[int] = []
    result = report.get("result")
    execution = result.get("execution") if isinstance(result, dict) else None
    if isinstance(execution, dict) and isinstance(execution.get("peak_rss_bytes"), int):
        peaks.append(execution["peak_rss_bytes"])
    vectors = report.get("vectors")
    outputs = vectors.get("outputs") if isinstance(vectors, dict) else None
    if isinstance(outputs, list):
        peaks.extend(
            record["peak_rss_bytes"]
            for record in outputs
            if isinstance(record, dict) and isinstance(record.get("peak_rss_bytes"), int)
        )
    if not peaks:
        raise BenchmarkError("native run report has no persisted RSS measurement")
    return max(peaks)


def _engine_surface_sha(report: dict[str, Any]) -> str | None:
    result = report.get("result")
    surface = result.get("trade_surface") if isinstance(result, dict) else None
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _reference_surface_sha(report: dict[str, Any]) -> str | None:
    surface = report.get("official_trade_surface")
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _existing_stream(path: Path) -> dict[str, Any] | None:
    return {"path": str(path)} if path.is_file() else None
