"""Full X7 measurement, cache, and result summaries."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from ..cache import cache_key
from ..canonical import read_json, write_json
from ..errors import BenchmarkError
from ..evidence_bundle import artifact_record
from ..fixture import sha256_file
from ..full_x7_resume import (
    _valid_swap_peak,
    write_measurement_checkpoint,
)
from ..performance_gate import measure_cli_process
from ..release_inputs import (
    release_data_history_coverage_policy,
)
from ..result_report import write_result_presentation
from ..vector_runtime import VECTOR_PIPELINE_VERSION


def _measure_engine(
    inputs: dict[str, Any],
    output: Path,
    *,
    profile_path: Path,
    timeout_seconds: int,
    resume: bool = False,
    vector_cache: Path | None = None,
    recalibrate: bool = True,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    pairs = inputs["lock"]["pairlist"]["pairs"]
    cache_directory = (
        vector_cache.resolve()
        if vector_cache is not None
        else (output / "cold-vector-cache").resolve()
    )
    arguments = [
        "backtest",
        str(inputs["strategy_path"]),
        "--class",
        inputs["lock"]["strategy"]["class_name"],
        "--config",
        str(inputs["config_path"]),
        "--datadir",
        str(inputs["data_directory"]),
        "--timerange",
        inputs["lock"]["scope"]["timerange"],
        "--output-dir",
        str(output),
        "--cache-dir",
        str(cache_directory),
        "--markets",
        str(inputs["engine_market_snapshot"]),
        "--no-market-download",
        "--registry",
        str(output / "runs.sqlite"),
        "--profile",
        str(profile_path),
        "--no-download",
        "--history-coverage",
        release_data_history_coverage_policy(inputs["lock"]),
    ]
    if recalibrate:
        arguments.append("--recalibrate")
    for pair in pairs:
        arguments.extend(["--pair", pair])
    if resume:
        arguments.append("--resume")
    measurement = measure_cli_process(
        arguments,
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    measurement["report"] = read_json(report_path) if report_path.is_file() else None
    measurement["output_directory"] = output
    measurement["result_sha256"] = _engine_surface_sha(measurement)
    write_measurement_checkpoint(output, measurement)
    if report_path.is_file():
        write_result_presentation(output)
    return measurement


def _measure_reference(
    baseline_directory: Path,
    market_snapshot: Path | None,
    output: Path,
    *,
    timeout_seconds: int,
    swap_cap_bytes: int | None,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        "reference",
        "research",
        str(baseline_directory),
        "--output-dir",
        str(output),
        "--timeout",
        str(timeout_seconds),
        "--memory-mode",
        "certification-swap",
        "--storage-mode",
        "spooled",
    ]
    if market_snapshot is not None:
        arguments.extend(
            [
                "--markets",
                str(market_snapshot),
                "--no-market-capture",
            ]
        )
    if swap_cap_bytes is not None:
        arguments.extend(["--swap-cap-gib", str(swap_cap_bytes / 1024**3)])
    measurement = measure_cli_process(
        arguments,
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    measurement["report"] = read_json(report_path) if report_path.is_file() else None
    report = measurement["report"]
    swap = report.get("container_swap") if isinstance(report, dict) else None
    measurement["peak_swap_bytes"] = _valid_swap_peak(
        swap.get("peak_bytes") if isinstance(swap, dict) else None
    )
    measurement["output_directory"] = output
    measurement["result_sha256"] = _reference_surface_sha(measurement)
    write_measurement_checkpoint(output, measurement)
    return measurement


def _require_complete_baseline(
    measurement: dict[str, Any],
    lock: dict[str, Any],
) -> None:
    if not _engine_complete(measurement, lock):
        raise BenchmarkError(
            "Full X7 warmup/baseline did not complete its locked native history "
            "contract; "
            "inspect warmups/engine/run.json"
        )


def _engine_complete(
    measurement: dict[str, Any],
    lock: dict[str, Any],
    *,
    require_cold: bool = True,
) -> bool:
    report = measurement.get("report")
    return bool(
        measurement.get("exit_code") == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and (not require_cold or report.get("pipeline_evidence", {}).get("cold") is True)
        and report.get("data", {}).get("history_coverage_policy")
        == release_data_history_coverage_policy(lock)
        and report.get("data", {}).get("coverage_shortfall_count")
        == lock["data"].get("coverage_shortfall_count", 0)
        and report.get("data", {}).get("aggregate_sha256") == lock["data"]["aggregate_sha256"]
        and not report.get("capability", {}).get("blockers")
        and isinstance(measurement.get("result_sha256"), str)
    )


def _engine_reuse_complete(
    measurement: dict[str, Any],
    lock: dict[str, Any],
) -> bool:
    """Require a fresh simulation over every content-addressed vector cache hit."""
    if not _engine_complete(measurement, lock, require_cold=False):
        return False
    report = measurement["report"]
    evidence = report.get("pipeline_evidence")
    vectors = report.get("vectors")
    pairs = lock["pairlist"]["pairs"]
    outputs = vectors.get("outputs") if isinstance(vectors, dict) else None
    if (
        not isinstance(evidence, dict)
        or evidence.get("cold") is not False
        or evidence.get("vector_cache_hits") != len(pairs)
        or any(
            evidence.get(field) is not False
            for field in (
                "data_checkpoint_reused",
                "vector_checkpoint_reused",
                "manifest_checkpoint_reused",
                "engine_checkpoint_reused",
                "surface_checkpoint_reused",
            )
        )
        or report.get("resumed_stages") != []
        or not isinstance(vectors, dict)
        or vectors.get("pair_count") != len(pairs)
        or vectors.get("cache_hits") != len(pairs)
        or not isinstance(outputs, list)
        or [item.get("pair") for item in outputs if isinstance(item, dict)] != pairs
    ):
        return False
    return all(
        isinstance(item, dict)
        and item.get("cache_hit") is True
        and isinstance(item.get("cache_key"), str)
        and isinstance(item.get("sha256"), str)
        for item in outputs
    )


def _seal_preserved_vector_cache(
    baseline: dict[str, Any],
    *,
    pairs: list[str],
    destination: Path,
) -> dict[str, Any]:
    """Bind the reusable cache to the cold baseline's exact 80 vector artifacts."""
    report = baseline.get("report")
    output = baseline.get("output_directory")
    if not isinstance(report, dict):
        raise BenchmarkError("cold seed has no complete run report")
    vectors = report.get("vectors")
    if not isinstance(vectors, dict):
        raise BenchmarkError("cold seed has no vector report")
    records = vectors.get("outputs")
    if (
        not isinstance(output, Path)
        or not isinstance(records, list)
        or [item.get("pair") for item in records if isinstance(item, dict)] != pairs
    ):
        raise BenchmarkError("cold seed has no complete ordered vector cache records")
    cache_root = (output / "cold-vector-cache").resolve()
    if not cache_root.is_dir():
        raise BenchmarkError("cold seed vector cache does not exist")

    entries: list[dict[str, Any]] = []
    for pair, record in zip(pairs, records, strict=True):
        if not isinstance(record, dict):
            raise BenchmarkError(f"cold seed vector record is invalid: {pair}")
        vector_key = record.get("cache_key")
        vector_sha = record.get("sha256")
        vector_bytes = record.get("bytes")
        if (
            not isinstance(vector_key, str)
            or not vector_key.startswith("vectors-")
            or not isinstance(vector_sha, str)
            or not isinstance(vector_bytes, int)
        ):
            raise BenchmarkError(f"cold seed vector identity is invalid: {pair}")
        _validate_preserved_cache_entry(
            cache_root,
            vector_key,
            expected_bytes=vector_bytes,
            expected_sha256=vector_sha,
        )
        record_key = cache_key(
            "vector-records",
            {
                "vector_cache_key": vector_key,
                "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            },
        )
        record_payload = _validate_preserved_cache_entry(cache_root, record_key)
        try:
            cached_record = json.loads(record_payload.read_bytes())
        except (OSError, ValueError) as exc:
            raise BenchmarkError(f"cold seed cached vector metadata is invalid: {pair}") from exc
        if not isinstance(cached_record, dict) or any(
            cached_record.get(field) != record.get(field)
            for field in (
                "pair",
                "base_timeframe",
                "bytes",
                "sha256",
                "input_sha256",
                "strategy_sha256",
                "config_sha256",
            )
        ):
            raise BenchmarkError(f"cold seed cached vector metadata differs: {pair}")
        entries.append(
            {
                "pair": pair,
                "vector_key": vector_key,
                "vector_bytes": vector_bytes,
                "vector_sha256": vector_sha,
                "record_key": record_key,
                "record_bytes": record_payload.stat().st_size,
                "record_sha256": sha256_file(record_payload),
            }
        )

    encoded_entries = json.dumps(
        entries,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest = {
        "schema_version": "1.0.0",
        "source_run_id": report.get("run_id"),
        "vector_pipeline_version": vectors.get("pipeline_version"),
        "strategy_sha256": vectors.get("strategy_sha256"),
        "config_sha256": vectors.get("config_sha256"),
        "pair_count": len(pairs),
        "entries_sha256": hashlib.sha256(encoded_entries).hexdigest(),
        "entries": entries,
    }
    if destination.is_file():
        if read_json(destination) != manifest:
            raise BenchmarkError("preserved vector cache seal differs from the cold seed")
    else:
        if destination.exists():
            raise BenchmarkError("preserved vector cache seal path is not a file")
        write_json(destination, manifest)
    return {
        "root": cache_root,
        "manifest": destination,
        "record": artifact_record(destination, relative_to=destination.parent),
    }


def _validate_preserved_cache_entry(
    cache_root: Path,
    key: str,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> Path:
    entry = (cache_root / key).resolve()
    if not entry.is_relative_to(cache_root):
        raise BenchmarkError("preserved vector cache key escapes its root")
    payload = entry / "payload"
    metadata_path = entry / "metadata.json"
    if not payload.is_file() or not metadata_path.is_file():
        raise BenchmarkError(f"preserved vector cache entry is incomplete: {key}")
    try:
        metadata = read_json(metadata_path)
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"preserved vector cache metadata is invalid: {key}") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("key") != key
        or metadata.get("bytes") != payload.stat().st_size
        or metadata.get("sha256") != sha256_file(payload)
    ):
        raise BenchmarkError(f"preserved vector cache entry failed its metadata seal: {key}")
    if (
        expected_bytes is not None
        and metadata["bytes"] != expected_bytes
        or expected_sha256 is not None
        and metadata["sha256"] != expected_sha256
    ):
        raise BenchmarkError(f"preserved vector cache entry differs from the cold seed: {key}")
    return payload


def _reference_complete(measurement: dict[str, Any]) -> bool:
    report = measurement.get("report")
    memory = report.get("container_memory") if isinstance(report, dict) else None
    return bool(
        measurement.get("exit_code") == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and report.get("exact_parity") is True
        and isinstance(measurement.get("result_sha256"), str)
        and isinstance(memory, dict)
        and memory.get("verdict") not in {"oom_killed", "possible_oom"}
    )


def _engine_surface_sha(measurement: dict[str, Any]) -> str | None:
    report = measurement.get("report")
    result = report.get("result") if isinstance(report, dict) else None
    surface = result.get("trade_surface") if isinstance(result, dict) else None
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _reference_surface_sha(measurement: dict[str, Any]) -> str | None:
    report = measurement.get("report")
    surface = report.get("official_trade_surface") if isinstance(report, dict) else None
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _determinism(
    baseline_hash: str | None,
    engine_runs: list[dict[str, Any]],
    reference_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    hashes = [
        baseline_hash,
        *(run.get("result_sha256") for run in engine_runs),
        *(run.get("result_sha256") for run in reference_runs),
    ]
    valid = all(isinstance(value, str) for value in hashes)
    unique = sorted({value for value in hashes if isinstance(value, str)})
    return {
        "met": valid and len(unique) == 1,
        "result_sha256": unique,
        "rule": "warmup, native, and official surfaces must have one identical SHA-256",
    }


def _run_summary(runs: list[dict[str, Any]], *, lane: str) -> dict[str, Any]:
    wall = [float(run["wall_time_seconds"]) for run in runs]
    peaks = []
    swap_peaks = [
        peak
        for run in runs
        if (peak := _valid_swap_peak(run.get("peak_swap_bytes"))) is not None
    ]
    for run in runs:
        peak = int(run["peak_rss_bytes"])
        report = run.get("report")
        if lane == "engine" and isinstance(report, dict):
            result = report.get("result")
            execution = result.get("execution") if isinstance(result, dict) else None
            native_peak = execution.get("peak_rss_bytes") if isinstance(execution, dict) else None
            if isinstance(native_peak, int):
                peak = max(peak, native_peak)
        elif lane == "reference" and isinstance(report, dict):
            memory = report.get("container_memory")
            container_peak = memory.get("peak_bytes") if isinstance(memory, dict) else None
            if isinstance(container_peak, int):
                peak = max(peak, container_peak)
        peaks.append(peak)
    return {
        "wall_time_seconds": {
            "minimum": min(wall),
            "median": statistics.median(wall),
            "maximum": max(wall),
        },
        "peak_rss_bytes": {
            "minimum": min(peaks),
            "maximum": max(peaks),
        },
        "peak_swap_bytes": {
            "maximum": max(swap_peaks, default=None),
            "measurements_complete": len(swap_peaks) == len(runs),
        },
    }


def _public_run_record(
    measurement: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    """Project one raw run to the small, path-safe release evidence surface."""
    output = measurement.get("output_directory")
    report_path = Path(output) / "run.json" if isinstance(output, Path) else None
    record: dict[str, Any] = {
        "wall_time_seconds": measurement["wall_time_seconds"],
        "peak_rss_bytes": measurement["peak_rss_bytes"],
        "peak_swap_bytes": measurement.get("peak_swap_bytes"),
        "exit_code": measurement["exit_code"],
        "timed_out": measurement["timed_out"],
        "result_sha256": measurement.get("result_sha256"),
    }
    if report_path is not None and report_path.is_file():
        record["run_report"] = artifact_record(report_path, relative_to=root)
    else:
        record["run_report"] = None
    for stream in ("stdout", "stderr"):
        raw = measurement.get(stream)
        stream_path = Path(raw["path"]) if isinstance(raw, dict) else None
        record[stream] = (
            artifact_record(stream_path, relative_to=root)
            if stream_path is not None and stream_path.is_file()
            else None
        )
    return record


def _relative_spread(runs: list[dict[str, Any]]) -> float:
    values = [float(run["wall_time_seconds"]) for run in runs]
    median = statistics.median(values)
    return (max(values) - min(values)) / median if median > 0 else 0.0
