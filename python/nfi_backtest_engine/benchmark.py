"""Reproducible external-command benchmark runner for sealed fixtures."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file, validate_fixture
from .profiling import PROFILE_ENV, aggregate_profile_events


def run_benchmark(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    command_override: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run warmups and measured repetitions against one verified fixture."""
    try:
        import psutil
    except ImportError as exc:
        raise BenchmarkError(
            "benchmark measurement requires psutil; install with `uv sync --extra benchmark`"
        ) from exc

    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    command = list(command_override or manifest["freqtrade"]["command"])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise BenchmarkError("benchmark command is empty")

    output_file = Path(output_path).resolve()
    run_directory = output_file.parent / f"{output_file.stem}.files"
    run_directory.mkdir(parents=True, exist_ok=True)
    measurement = manifest["measurement"]
    warmup_count = measurement["warmup_runs"]
    measured_count = measurement["measured_runs"]
    poll_seconds = measurement["poll_interval_ms"] / 1000

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "fixture_id": manifest["fixture_id"],
        "fixture_evidence_status": manifest["evidence_status"],
        "manifest_path": str(manifest_file),
        "manifest_sha256": sha256_file(manifest_file),
        "trade_count": _fixture_trade_count(manifest_file, manifest),
        "command": command,
        "working_directory": str(manifest_file.parent),
        "hardware": _hardware_record(psutil),
        "warmups": [],
        "runs": [],
    }

    total_invocations = warmup_count + measured_count
    for invocation_index in range(total_invocations):
        is_warmup = invocation_index < warmup_count
        category = "warmup" if is_warmup else "measured"
        category_index = invocation_index if is_warmup else invocation_index - warmup_count
        label = f"{category}-{category_index + 1:02d}"
        invocation = _run_once(
            psutil=psutil,
            command=command,
            cwd=manifest_file.parent,
            run_directory=run_directory,
            label=label,
            poll_seconds=poll_seconds,
        )
        report["warmups" if is_warmup else "runs"].append(invocation)
        if invocation["exit_code"] != 0:
            break

    all_runs = [*report["warmups"], *report["runs"]]
    report["complete"] = (
        len(report["warmups"]) == warmup_count
        and len(report["runs"]) == measured_count
        and all(item["exit_code"] == 0 for item in all_runs)
        and all(not item["profile"]["missing_phases"] for item in report["runs"])
    )
    report["measurement_summary"] = _summarize_measured_runs(report["runs"])
    write_json(output_file, report)
    return report


def _run_once(
    *,
    psutil: Any,
    command: list[str],
    cwd: Path,
    run_directory: Path,
    label: str,
    poll_seconds: float,
) -> dict[str, Any]:
    stdout_path = run_directory / f"{label}.stdout.log"
    stderr_path = run_directory / f"{label}.stderr.log"
    events_path = run_directory / f"{label}.profile.jsonl"
    if events_path.exists():
        events_path.unlink()

    environment = os.environ.copy()
    environment[PROFILE_ENV] = str(events_path)
    started_at = datetime.now(timezone.utc)
    started_ns = time.perf_counter_ns()
    peak_rss = 0
    latest_cpu_seconds = 0.0

    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                shell=False,
            )
        except OSError as exc:
            raise BenchmarkError(f"failed to start benchmark command: {exc}") from exc

        root_process = psutil.Process(process.pid)
        while process.poll() is None:
            rss, cpu_seconds = _process_tree_snapshot(psutil, root_process)
            peak_rss = max(peak_rss, rss)
            latest_cpu_seconds = max(latest_cpu_seconds, cpu_seconds)
            time.sleep(poll_seconds)
        rss, cpu_seconds = _process_tree_snapshot(psutil, root_process)
        peak_rss = max(peak_rss, rss)
        latest_cpu_seconds = max(latest_cpu_seconds, cpu_seconds)
        exit_code = process.wait()

    ended_at = datetime.now(timezone.utc)
    profile = (
        aggregate_profile_events(events_path)
        if events_path.is_file()
        else {
            "schema_version": "1.0.0",
            "phases": {},
            "missing_phases": [
                "indicators",
                "callbacks",
                "trade_scans",
                "event_simulation",
            ],
        }
    )
    return {
        "label": label,
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(ended_at),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "peak_rss_bytes": peak_rss,
        "cpu_time_seconds": latest_cpu_seconds,
        "exit_code": exit_code,
        "stdout": {
            "path": str(stdout_path),
            "bytes": stdout_path.stat().st_size,
            "sha256": sha256_file(stdout_path),
        },
        "stderr": {
            "path": str(stderr_path),
            "bytes": stderr_path.stat().st_size,
            "sha256": sha256_file(stderr_path),
        },
        "profile": profile,
    }


def _process_tree_snapshot(psutil: Any, root_process: Any) -> tuple[int, float]:
    try:
        processes = [root_process, *root_process.children(recursive=True)]
    except psutil.Error:
        processes = [root_process]
    rss = 0
    cpu_seconds = 0.0
    for process in processes:
        try:
            rss += process.memory_info().rss
            cpu = process.cpu_times()
            cpu_seconds += cpu.user + cpu.system
        except psutil.Error:
            continue
    return rss, cpu_seconds


def _summarize_measured_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if run["exit_code"] == 0]
    if not successful:
        return {"successful_runs": 0}
    wall_times = [run["wall_time_seconds"] for run in successful]
    peaks = [run["peak_rss_bytes"] for run in successful]
    return {
        "successful_runs": len(successful),
        "wall_time_seconds": {
            "minimum": min(wall_times),
            "maximum": max(wall_times),
            "mean": sum(wall_times) / len(wall_times),
        },
        "peak_rss_bytes": {
            "minimum": min(peaks),
            "maximum": max(peaks),
            "mean": sum(peaks) / len(peaks),
        },
    }


def _hardware_record(psutil: Any) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "total_memory_bytes": memory.total,
    }


def _fixture_trade_count(manifest_file: Path, manifest: dict[str, Any]) -> int:
    surface_path = manifest_file.parent / manifest["artifacts"]["trade_surface"]["path"]
    surface = read_json(surface_path)
    return len(surface["trades"])


def _utc_string(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
