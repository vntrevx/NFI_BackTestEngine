"""Memory-aware parallel runner for independent strategy candidates."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError, SpecValidationError
from .hardware import ensure_execution_profile
from .research_runner import run_research_backtest

BATCH_MANIFEST_VERSION = "1.0.0"
BATCH_REPORT_VERSION = "1.1.0"
_JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def run_batch(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path = ".nfi/execution-profile.json",
    cache_directory: str | Path = ".nfi/cache",
    registry_path: str | Path = ".nfi/runs.sqlite",
    resume: bool = False,
    download_missing: bool = True,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    started_ns = time.perf_counter_ns()
    manifest_file = Path(manifest_path).resolve()
    manifest = read_json(manifest_file)
    jobs = _validate_manifest(manifest, root=manifest_file.parent)
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()) and not resume:
        raise BenchmarkError(f"batch output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    profile = ensure_execution_profile(profile_path, workspace=output)
    parallelism = _parallelism_plan(profile, job_count=len(jobs), max_jobs=max_jobs)
    parallel_jobs = parallelism["parallel_job_processes"]
    per_job_workers = parallelism["indicator_processes_per_job"]
    requests = [
        {
            **job,
            "output_directory": str(output / job["name"]),
            "profile_path": str(Path(profile_path).resolve()),
            "cache_directory": str(Path(cache_directory).resolve()),
            "registry_path": str(Path(registry_path).resolve()),
            "resume": resume,
            "download_missing": download_missing,
            "workers": per_job_workers,
            # The parent already validated current hardware and free-memory
            # headroom before spawning the batch. Revalidating inside sibling
            # jobs would mistake memory intentionally used by earlier siblings
            # for unrelated host pressure.
            "execution_profile": profile,
        }
        for job in jobs
    ]
    records: list[dict[str, Any]] = []
    if len(requests) == 1:
        records.append(_execute_job(requests[0]))
    else:
        with ProcessPoolExecutor(
            max_workers=parallel_jobs,
            mp_context=get_context("spawn"),
            max_tasks_per_child=1,
        ) as executor:
            futures = {
                executor.submit(_execute_job, request): str(request["name"]) for request in requests
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    records.append(future.result())
                except Exception as exc:
                    records.append(
                        {
                            "name": name,
                            "status": "failed",
                            "complete": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "run_id": None,
                            "output_directory": str(output / name),
                        }
                    )
    order = {job["name"]: index for index, job in enumerate(jobs)}
    records.sort(key=lambda item: order[item["name"]])
    wall_time_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    aggregate_job_seconds = sum(float(item["wall_time_seconds"]) for item in records)
    effective_parallelism = aggregate_job_seconds / wall_time_seconds
    report = {
        "schema_version": BATCH_REPORT_VERSION,
        "manifest": str(manifest_file),
        "hardware_fingerprint": profile["hardware_fingerprint"],
        "coordinator_process_id": os.getpid(),
        "parallel_jobs": parallel_jobs,
        "workers_per_job": per_job_workers,
        "parallelism": parallelism,
        "jobs": records,
        "complete": all(item["complete"] for item in records),
        "wall_time_seconds": wall_time_seconds,
        "aggregate_job_seconds": aggregate_job_seconds,
        "effective_parallelism": effective_parallelism,
        "parallel_efficiency": effective_parallelism / parallel_jobs,
    }
    write_json(output / "batch.json", report)
    return report


def _parallelism_plan(
    profile: dict[str, Any],
    *,
    job_count: int,
    max_jobs: int | None,
) -> dict[str, Any]:
    if job_count <= 0:
        raise SpecValidationError("batch job count must be positive")
    tuning = profile["tuning"]
    safe_jobs = int(tuning["independent_research_jobs"])
    requested_jobs = max_jobs or safe_jobs
    if requested_jobs <= 0:
        raise SpecValidationError("batch max_jobs must be positive")
    parallel_jobs = max(1, min(requested_jobs, safe_jobs, job_count))
    indicator_processes = int(tuning["indicator_processes"])
    per_job = max(1, indicator_processes // parallel_jobs)
    return {
        "process_start_method": "spawn",
        "parallel_job_processes": parallel_jobs,
        "indicator_processes_per_job": per_job,
        "maximum_indicator_processes": parallel_jobs * per_job,
        "nested_numeric_threads_per_process": int(tuning["nested_numeric_threads"]),
        "working_memory_bytes": int(tuning["working_memory_bytes"]),
        "assumed_indicator_worker_peak_bytes": int(
            tuning["assumed_indicator_worker_peak_bytes"]
        ),
    }


def _execute_job(request: dict[str, Any]) -> dict[str, Any]:
    started_ns = time.perf_counter_ns()
    name = request.pop("name")
    try:
        report = run_research_backtest(**request)
        return {
            "name": name,
            "status": report["status"],
            "complete": report["complete"] or report["prepared_only"],
            "error": None,
            "run_id": report["run_id"],
            "output_directory": request["output_directory"],
            "process_id": os.getpid(),
            "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "complete": False,
            "error": f"{type(exc).__name__}: {exc}",
            "run_id": None,
            "output_directory": request["output_directory"],
            "process_id": os.getpid(),
            "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        }


def _validate_manifest(document: Any, *, root: Path) -> list[dict[str, Any]]:
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != BATCH_MANIFEST_VERSION
        or set(document) != {"schema_version", "jobs"}
    ):
        raise SpecValidationError("batch manifest fields differ from v1")
    raw_jobs = document["jobs"]
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SpecValidationError("batch manifest requires at least one job")
    required = {
        "name",
        "strategy_path",
        "class_name",
        "config_path",
        "data_directory",
        "timerange",
    }
    optional = {"pairs", "market_metadata_path", "prepare_only"}
    result = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_jobs):
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise SpecValidationError(f"batch job {index} is missing required fields")
        if not set(raw).issubset(required | optional):
            raise SpecValidationError(f"batch job {index} has unknown fields")
        name = raw["name"]
        if not isinstance(name, str) or _JOB_NAME.fullmatch(name) is None or name in seen:
            raise SpecValidationError(f"batch job {index} has an invalid or duplicate name")
        seen.add(name)
        normalized = dict(raw)
        for field in (
            "strategy_path",
            "config_path",
            "data_directory",
            "market_metadata_path",
        ):
            value = normalized.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise SpecValidationError(f"batch job {name} {field} must be a path")
                normalized[field] = str((root / value).resolve())
        result.append(normalized)
    return result
