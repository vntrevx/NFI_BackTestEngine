"""Fresh same-fixture performance and parity gate for reference and engine."""

from __future__ import annotations

import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil

from .canonical import read_json, write_json
from .engine_runtime import build_engine
from .errors import BenchmarkError
from .fixture import sha256_file, validate_fixture
from .hardware import current_resource_limits, inspect_hardware, load_execution_profile
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_RELEASE_BACKTEST_DAYS,
    MIN_RELEASE_PAIR_COUNT,
    TARGET_SCREENING_SPEEDUP,
)

PerformanceLevel = Literal["quick", "full"]


def run_performance_gate(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    profile_path: str | Path | None = None,
    verification_level: PerformanceLevel = "full",
    repetitions: int = 1,
    timeout_seconds: int = 600,
    warmup_runs: int = 0,
    adaptive: bool = False,
    max_repetitions: int = MAX_CERTIFICATION_REPETITIONS,
    spread_threshold: float = CERTIFICATION_SPREAD_THRESHOLD,
    alternate_order: bool = False,
) -> dict[str, Any]:
    """Measure fresh CLI processes and retain complete proof artifacts."""
    if verification_level not in {"quick", "full"}:
        raise BenchmarkError(f"unsupported verification level: {verification_level!r}")
    if repetitions < 1:
        raise BenchmarkError("performance repetitions must be at least 1")
    if warmup_runs < 0:
        raise BenchmarkError("performance warmup count must be non-negative")
    if max_repetitions < repetitions:
        raise BenchmarkError("maximum repetitions cannot be less than initial repetitions")
    if not 0 <= spread_threshold <= 1:
        raise BenchmarkError("performance spread threshold must be between 0 and 1")
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"performance output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    profile_file = Path(profile_path).resolve() if profile_path is not None else None
    profile = load_execution_profile(profile_file) if profile_file is not None else None
    build = build_engine()

    warmups: dict[str, list[dict[str, Any]]] = {"engine": [], "reference": []}
    for index in range(warmup_runs):
        for lane in ("engine", "reference"):
            warmups[lane].append(
                _measure_fixture_lane(
                    lane,
                    manifest_file=manifest_file,
                    output=output / "warmups",
                    run_number=index + 1,
                    profile_file=profile_file,
                    verification_level=verification_level,
                    timeout_seconds=timeout_seconds,
                )
            )

    engine_runs: list[dict[str, Any]] = []
    reference_runs: list[dict[str, Any]] = []
    measured_orders: list[list[str]] = []
    target_repetitions = repetitions
    while len(engine_runs) < target_repetitions:
        run_number = len(engine_runs) + 1
        order = (
            ["reference", "engine"]
            if alternate_order and run_number % 2 == 0
            else ["engine", "reference"]
        )
        measured_orders.append(order)
        measurements: dict[str, dict[str, Any]] = {}
        for lane in order:
            measurements[lane] = _measure_fixture_lane(
                lane,
                manifest_file=manifest_file,
                output=output,
                run_number=run_number,
                profile_file=profile_file,
                verification_level=verification_level,
                timeout_seconds=timeout_seconds,
            )
        engine_runs.append(measurements["engine"])
        reference_runs.append(measurements["reference"])
        if (
            adaptive
            and len(engine_runs) == repetitions
            and repetitions < max_repetitions
            and max(
                _relative_spread(engine_runs),
                _relative_spread(reference_runs),
            )
            > spread_threshold
        ):
            target_repetitions = max_repetitions

    engine_summary = _measurement_summary(engine_runs, engine=True)
    reference_summary = _measurement_summary(reference_runs, engine=False)
    speedup = (
        reference_summary["wall_time_seconds"]["median"]
        / engine_summary["wall_time_seconds"]["median"]
    )
    representative = _representative_scope(manifest_file, manifest)
    measured_hardware = inspect_hardware()
    memory_limit = (
        int(current_resource_limits(profile, hardware=measured_hardware)["working_memory_bytes"])
        if profile is not None
        else int(measured_hardware["memory"]["available_bytes"])
    )
    parity_complete = all(
        run["exit_code"] == 0 and run["report"] is not None and run["report"]["complete"]
        for run in [*engine_runs, *reference_runs]
    )
    determinism = _determinism_assessment(engine_runs, reference_runs)
    speed_target_met = speedup >= TARGET_SCREENING_SPEEDUP
    memory_target_met = engine_summary["peak_rss_bytes"]["maximum"] <= memory_limit
    complete, release_certified = _certification_verdict(
        representative=representative["eligible"],
        parity=parity_complete,
        speed=speed_target_met,
        memory=memory_target_met,
        determinism=determinism["met"],
    )
    report = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fixture_id": manifest["fixture_id"],
        "manifest_path": str(manifest_file),
        "manifest_sha256": sha256_file(manifest_file),
        "verification_level": verification_level,
        "repetitions": len(engine_runs),
        "measurement": {
            "warmup_runs": warmup_runs,
            "initial_repetitions": repetitions,
            "maximum_repetitions": max_repetitions,
            "adaptive": adaptive,
            "spread_threshold": spread_threshold,
            "orders": measured_orders,
            "engine_relative_spread": _relative_spread(engine_runs),
            "reference_relative_spread": _relative_spread(reference_runs),
        },
        "hardware": measured_hardware,
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
            "warmups": warmups["engine"],
            "runs": engine_runs,
            "summary": engine_summary,
        },
        "reference": {
            "warmups": warmups["reference"],
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
                "target_speedup": TARGET_SCREENING_SPEEDUP,
                "observed_speedup": speedup,
                "met": speed_target_met,
                "verdict": ("pass" if speed_target_met else "fail")
                if representative["eligible"]
                else "diagnostic-only",
            },
            "memory": {
                "limit_bytes": memory_limit,
                "observed_peak_bytes": engine_summary["peak_rss_bytes"]["maximum"],
                "met": memory_target_met,
            },
            "determinism": determinism,
        },
        "claim_scope": representative,
        "release_certified": release_certified,
        "complete": complete,
    }
    write_json(output / "performance.json", report)
    return report


def _certification_verdict(
    *,
    representative: bool,
    parity: bool,
    speed: bool,
    memory: bool,
    determinism: bool = True,
) -> tuple[bool, bool]:
    """Separate a completed diagnostic from release-grade certification."""
    complete = parity and memory and determinism and (speed if representative else True)
    return complete, representative and complete


def _measure_fixture_lane(
    lane: str,
    *,
    manifest_file: Path,
    output: Path,
    run_number: int,
    profile_file: Path | None,
    verification_level: PerformanceLevel,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Measure one fresh lane while keeping warmups and measured runs identical."""
    output.mkdir(parents=True, exist_ok=True)
    lane_output = output / f"{lane}-{run_number:02d}"
    if lane == "engine":
        arguments = [
            "engine",
            "fixture",
            str(manifest_file),
            "--output-dir",
            str(lane_output),
            "--level",
            verification_level,
            "--timeout",
            str(timeout_seconds),
            *(["--profile", str(profile_file)] if profile_file is not None else []),
        ]
    elif lane == "reference":
        arguments = [
            "reference",
            "run",
            str(manifest_file),
            "--output-dir",
            str(lane_output),
            "--trace",
            "full" if verification_level == "full" else "off",
            "--timeout",
            str(timeout_seconds),
        ]
    else:
        raise BenchmarkError(f"unsupported performance lane: {lane!r}")
    measurement = _measure_cli(
        arguments,
        output / f"{lane}-{run_number:02d}.stdout.log",
        output / f"{lane}-{run_number:02d}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = lane_output / "run.json"
    measurement["report"] = read_json(report_path) if report_path.is_file() else None
    measurement["result_sha256"] = _result_sha256(measurement["report"], lane=lane)
    return measurement


def _result_sha256(report: Any, *, lane: str) -> str | None:
    if not isinstance(report, dict):
        return None
    record: Any
    if lane == "engine":
        artifacts = report.get("artifacts")
        record = artifacts.get("trade_surface") if isinstance(artifacts, dict) else None
    else:
        record = report.get("trade_surface")
    value = record.get("sha256") if isinstance(record, dict) else None
    return value if isinstance(value, str) else None


def _determinism_assessment(
    engine_runs: list[dict[str, Any]],
    reference_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_engine_hashes = [run.get("result_sha256") for run in engine_runs]
    raw_reference_hashes = [run.get("result_sha256") for run in reference_runs]
    valid = all(
        isinstance(value, str)
        for value in [*raw_engine_hashes, *raw_reference_hashes]
    )
    engine_hashes = [
        value for value in raw_engine_hashes if isinstance(value, str)
    ]
    reference_hashes = [
        value for value in raw_reference_hashes if isinstance(value, str)
    ]
    engine_unique = sorted(set(engine_hashes)) if valid else []
    reference_unique = sorted(set(reference_hashes)) if valid else []
    met = (
        valid
        and len(engine_unique) == 1
        and len(reference_unique) == 1
        and engine_unique == reference_unique
    )
    return {
        "met": met,
        "engine_result_sha256": engine_unique,
        "reference_result_sha256": reference_unique,
        "rule": "every measured engine and reference trade surface must have one identical SHA-256",
    }


def _relative_spread(runs: list[dict[str, Any]]) -> float:
    values = [float(run["wall_time_seconds"]) for run in runs]
    if not values:
        return 0.0
    median = statistics.median(values)
    return (max(values) - min(values)) / median if median > 0 else 0.0


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
                _terminate_process_tree(root_process)
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


def measure_cli_process(
    arguments: list[str],
    stdout_path: str | Path,
    stderr_path: str | Path,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Measure an installed CLI process and its complete descendant RSS tree."""
    return _measure_cli(
        arguments,
        Path(stdout_path),
        Path(stderr_path),
        timeout_seconds=timeout_seconds,
    )


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


def _terminate_process_tree(root_process: psutil.Process) -> None:
    """Stop a timed-out command and every descendant it created.

    Backtests may launch worker processes or a Docker CLI child. Terminating only
    the Python parent would leave those processes consuming memory and poisoning
    later repetitions, so descendants are captured before termination and killed
    if they do not exit within the grace period.
    """
    try:
        descendants = root_process.children(recursive=True)
    except psutil.Error:
        descendants = []
    processes = [*descendants, root_process]
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(processes, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            continue
    psutil.wait_procs(alive, timeout=5)


def _measurement_summary(
    runs: list[dict[str, Any]],
    *,
    engine: bool,
) -> dict[str, Any]:
    wall_times = [run["wall_time_seconds"] for run in runs]
    pipeline_peaks = [run["peak_rss_bytes"] for run in runs]
    core_peaks_by_run: list[int | None] = []
    if engine:
        for run in runs:
            report = run.get("report")
            execution = report.get("execution") if isinstance(report, dict) else None
            peak = execution.get("peak_rss_bytes") if isinstance(execution, dict) else None
            core_peaks_by_run.append(peak if isinstance(peak, int) else None)
    else:
        for run in runs:
            report = run.get("report")
            peak = (
                report.get("container_peak_memory_bytes")
                if isinstance(report, dict)
                else None
            )
            core_peaks_by_run.append(peak if isinstance(peak, int) else None)
    core_peaks = [peak for peak in core_peaks_by_run if peak is not None]
    combined_peaks = [
        max(pipeline_peak, core_peak or 0)
        for pipeline_peak, core_peak in zip(
            pipeline_peaks,
            core_peaks_by_run,
            strict=True,
        )
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
    config_reference = next(item for item in manifest["inputs"] if item["role"] == "config")
    config = read_json(manifest_file.parent / config_reference["path"])
    pair_count = len(config["exchange"]["pair_whitelist"])
    start, end = manifest["freqtrade"]["timerange"].split("-", 1)
    start_date = datetime.strptime(start, "%Y%m%d")
    end_date = datetime.strptime(end, "%Y%m%d")
    days = (end_date - start_date).days
    eligible = (
        pair_count >= MIN_RELEASE_PAIR_COUNT
        and days >= MIN_RELEASE_BACKTEST_DAYS
    )
    return {
        "eligible": eligible,
        "required_pair_count": MIN_RELEASE_PAIR_COUNT,
        "required_days": MIN_RELEASE_BACKTEST_DAYS,
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
