"""Fresh same-fixture performance and parity gate for reference and engine."""

from __future__ import annotations

import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import psutil

from .canonical import read_json, write_json
from .engine_runtime import build_engine
from .errors import BenchmarkError
from .fixture import sha256_file, validate_fixture
from .hardware import GIB, inspect_hardware, load_execution_profile

PerformanceLevel = Literal["quick", "full"]


def run_performance_gate(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path | None = None,
    verification_level: PerformanceLevel = "full",
    repetitions: int = 1,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Measure fresh CLI processes and retain complete proof artifacts."""
    if verification_level not in {"quick", "full"}:
        raise BenchmarkError(f"unsupported verification level: {verification_level!r}")
    if repetitions < 1:
        raise BenchmarkError("performance repetitions must be at least 1")
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"performance output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    profile_file = Path(profile_path).resolve() if profile_path is not None else None
    profile = load_execution_profile(profile_file) if profile_file is not None else None
    build = build_engine()

    engine_runs = []
    reference_runs = []
    for index in range(repetitions):
        run_number = index + 1
        engine_output = output / f"engine-{run_number:02d}"
        engine_measurement = _measure_cli(
            [
                "engine",
                "fixture",
                str(manifest_file),
                "--output-dir",
                str(engine_output),
                "--level",
                verification_level,
                "--timeout",
                str(timeout_seconds),
                *(
                    ["--profile", str(profile_file)]
                    if profile_file is not None
                    else []
                ),
            ],
            output / f"engine-{run_number:02d}.stdout.log",
            output / f"engine-{run_number:02d}.stderr.log",
            timeout_seconds=timeout_seconds,
        )
        engine_report_path = engine_output / "run.json"
        engine_measurement["report"] = (
            read_json(engine_report_path) if engine_report_path.is_file() else None
        )
        engine_runs.append(engine_measurement)

        reference_output = output / f"reference-{run_number:02d}"
        reference_measurement = _measure_cli(
            [
                "reference",
                "run",
                str(manifest_file),
                "--output-dir",
                str(reference_output),
                "--trace",
                "full" if verification_level == "full" else "off",
                "--timeout",
                str(timeout_seconds),
            ],
            output / f"reference-{run_number:02d}.stdout.log",
            output / f"reference-{run_number:02d}.stderr.log",
            timeout_seconds=timeout_seconds,
        )
        reference_report_path = reference_output / "run.json"
        reference_measurement["report"] = (
            read_json(reference_report_path) if reference_report_path.is_file() else None
        )
        reference_runs.append(reference_measurement)

    engine_summary = _measurement_summary(engine_runs, engine=True)
    reference_summary = _measurement_summary(reference_runs, engine=False)
    speedup = (
        reference_summary["wall_time_seconds"]["median"]
        / engine_summary["wall_time_seconds"]["median"]
    )
    representative = _representative_scope(manifest_file, manifest)
    memory_limit = (
        profile["tuning"]["working_memory_bytes"]
        if profile is not None
        else 8 * GIB
    )
    parity_complete = all(
        run["exit_code"] == 0
        and run["report"] is not None
        and run["report"]["complete"]
        for run in [*engine_runs, *reference_runs]
    )
    speed_target_met = speedup >= 10.0
    memory_target_met = (
        engine_summary["peak_rss_bytes"]["maximum"] <= memory_limit
    )
    report = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fixture_id": manifest["fixture_id"],
        "manifest_path": str(manifest_file),
        "manifest_sha256": sha256_file(manifest_file),
        "verification_level": verification_level,
        "repetitions": repetitions,
        "measurement_order": "engine_then_reference",
        "hardware": inspect_hardware(),
        "execution_profile": (
            {
                "path": str(profile_file),
                "hardware_fingerprint": profile["hardware_fingerprint"],
                "working_memory_bytes": memory_limit,
            }
            if profile is not None
            else None
        ),
        "engine_build": build,
        "engine": {
            "runs": engine_runs,
            "summary": engine_summary,
        },
        "reference": {
            "runs": reference_runs,
            "summary": reference_summary,
        },
        "gates": {
            "parity": {
                "met": parity_complete,
                "rule": "all engine and official reference runs must complete exact parity",
            },
            "speed": {
                "eligible": representative["eligible"],
                "target_speedup": 10.0,
                "observed_speedup": speedup,
                "met": speed_target_met,
                "verdict": (
                    "pass" if speed_target_met else "fail"
                )
                if representative["eligible"]
                else "diagnostic-only",
            },
            "memory": {
                "limit_bytes": memory_limit,
                "observed_peak_bytes": engine_summary["peak_rss_bytes"]["maximum"],
                "met": memory_target_met,
            },
        },
        "claim_scope": representative,
        "complete": parity_complete,
    }
    write_json(output / "performance.json", report)
    return report


def _measure_cli(
    arguments: list[str],
    stdout_path: Path,
    stderr_path: Path,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [sys.executable, "-m", "nfi_backtest_engine.cli", *arguments]
    project_root = Path(__file__).resolve().parents[2]
    started_ns = time.perf_counter_ns()
    peak_rss_bytes = 0
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            stdout=stdout,
            stderr=stderr,
            shell=False,
        )
        root_process = psutil.Process(process.pid)
        while process.poll() is None:
            peak_rss_bytes = max(
                peak_rss_bytes,
                _process_tree_rss(root_process),
            )
            if (time.perf_counter_ns() - started_ns) / 1_000_000_000 > timeout_seconds:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(0.01)
        peak_rss_bytes = max(peak_rss_bytes, _process_tree_rss(root_process))
        exit_code = process.wait()
    return {
        "command": command,
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "peak_rss_bytes": peak_rss_bytes,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": _artifact(stdout_path),
        "stderr": _artifact(stderr_path),
    }


def _process_tree_rss(root_process: psutil.Process) -> int:
    try:
        processes = [root_process, *root_process.children(recursive=True)]
    except psutil.Error:
        processes = [root_process]
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except psutil.Error:
            continue
    return total


def _measurement_summary(
    runs: list[dict[str, Any]],
    *,
    engine: bool,
) -> dict[str, Any]:
    wall_times = [run["wall_time_seconds"] for run in runs]
    pipeline_peaks = [run["peak_rss_bytes"] for run in runs]
    core_peaks = []
    if engine:
        core_peaks = [
            run["report"]["execution"]["peak_rss_bytes"]
            for run in runs
            if run["report"] is not None
            and run["report"]["execution"]["peak_rss_bytes"] is not None
        ]
    else:
        core_peaks = [
            run["report"]["container_peak_memory_bytes"]
            for run in runs
            if run["report"] is not None
            and run["report"]["container_peak_memory_bytes"] is not None
        ]
    combined_peaks = [
        max(pipeline_peak, core_peaks[index] if index < len(core_peaks) else 0)
        for index, pipeline_peak in enumerate(pipeline_peaks)
    ]
    return {
        "wall_time_seconds": {
            "minimum": min(wall_times),
            "median": statistics.median(wall_times),
            "maximum": max(wall_times),
        },
        "pipeline_peak_rss_bytes": {
            "minimum": min(pipeline_peaks),
            "maximum": max(pipeline_peaks),
        },
        "core_peak_rss_bytes": {
            "minimum": min(core_peaks) if core_peaks else None,
            "maximum": max(core_peaks) if core_peaks else None,
        },
        "peak_rss_bytes": {
            "minimum": min(combined_peaks),
            "maximum": max(combined_peaks),
        },
    }


def _representative_scope(
    manifest_file: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    config_reference = next(
        item for item in manifest["inputs"] if item["role"] == "config"
    )
    config = read_json(manifest_file.parent / config_reference["path"])
    pair_count = len(config["exchange"]["pair_whitelist"])
    start, end = manifest["freqtrade"]["timerange"].split("-", 1)
    start_date = datetime.strptime(start, "%Y%m%d")
    end_date = datetime.strptime(end, "%Y%m%d")
    days = (end_date - start_date).days
    eligible = pair_count >= 80 and days >= 365
    return {
        "eligible": eligible,
        "required_pair_count": 80,
        "required_days": 365,
        "actual_pair_count": pair_count,
        "actual_days": days,
        "label": "representative" if eligible else "fixture-diagnostic-only",
    }


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
