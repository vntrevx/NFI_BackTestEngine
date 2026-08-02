"""Controlled before/after benchmark for the standalone Native simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .canonical import write_json
from .errors import BenchmarkError

NATIVE_RUNTIME_BENCHMARK_VERSION = "1.0.0"


def run_native_runtime_benchmark(
    *,
    baseline_binary: str | Path,
    candidate_binary: str | Path,
    workloads: dict[str, str | Path],
    output_path: str | Path,
    baseline_identity: str,
    candidate_identity: str,
    repetitions: int = 3,
    max_repetitions: int = 5,
    inner_iterations: int = 1,
    spread_threshold: float = 0.05,
    regression_tolerance: float = 0.05,
    poll_interval_ms: int = 1,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Measure two binaries against arbitrary sealed vector manifests.

    Every observation starts a fresh simulator process. The first invocation
    and one explicit warmup are retained as diagnostics but excluded from the
    measured medians and gates.
    """
    if repetitions < 3:
        raise BenchmarkError("native runtime benchmark requires at least three repetitions")
    if max_repetitions < repetitions:
        raise BenchmarkError("maximum repetitions cannot be less than initial repetitions")
    if inner_iterations < 1:
        raise BenchmarkError("inner iterations must be positive")
    if not 0 <= spread_threshold <= 1:
        raise BenchmarkError("spread threshold must be between zero and one")
    if not 0 <= regression_tolerance <= 1:
        raise BenchmarkError("regression tolerance must be between zero and one")
    if poll_interval_ms < 1:
        raise BenchmarkError("RSS poll interval must be positive")
    if timeout_seconds < 1:
        raise BenchmarkError("timeout must be positive")
    if not workloads:
        raise BenchmarkError("at least one workload is required")

    binaries = {
        "baseline": _required_file(baseline_binary, "baseline binary"),
        "candidate": _required_file(candidate_binary, "candidate binary"),
    }
    manifests = {
        name: _required_file(path, f"workload {name!r}") for name, path in sorted(workloads.items())
    }
    if any(not name or "=" in name for name in manifests):
        raise BenchmarkError("workload names must be non-empty and cannot contain '='")
    output = Path(output_path).resolve()
    if output.exists():
        raise BenchmarkError(f"native runtime benchmark output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / f"{output.stem}.files"
    if work.exists():
        raise BenchmarkError(f"native runtime benchmark work directory already exists: {work}")
    work.mkdir()

    identities: dict[str, dict[str, set[str]]] = {
        lane: {name: set() for name in manifests} for lane in binaries
    }

    def observe(lane: str, name: str, record: dict[str, Any]) -> None:
        identities[lane][name].add(str(record["result_sha256"]))

    cold: dict[str, dict[str, Any]] = {lane: {} for lane in binaries}
    for workload_index, (name, manifest) in enumerate(manifests.items()):
        lane_order = (
            ("baseline", "candidate") if workload_index % 2 == 0 else ("candidate", "baseline")
        )
        for lane in lane_order:
            record = _run_once(
                binaries[lane],
                manifest,
                work / f"{lane}-{name}.json",
                work / f"{lane}-{name}-profile.json",
                poll_interval_ms=poll_interval_ms,
                timeout_seconds=timeout_seconds,
            )
            cold[lane][name] = record
            observe(lane, name, record)

    warmups: dict[str, dict[str, Any]] = {lane: {} for lane in binaries}
    for lane in ("candidate", "baseline"):
        for name, manifest in manifests.items():
            record = _run_once(
                binaries[lane],
                manifest,
                work / f"{lane}-{name}.json",
                work / f"{lane}-{name}-profile.json",
                poll_interval_ms=poll_interval_ms,
                timeout_seconds=timeout_seconds,
            )
            warmups[lane][name] = record
            observe(lane, name, record)

    runs: dict[str, list[dict[str, Any]]] = {lane: [] for lane in binaries}
    target_repetitions = repetitions
    round_index = 0
    while round_index < target_repetitions:
        lane_order = (
            ("baseline", "candidate") if round_index % 2 == 0 else ("candidate", "baseline")
        )
        for lane in lane_order:
            record = _measure_lane(
                binary=binaries[lane],
                workloads=manifests,
                work=work,
                lane=lane,
                inner_iterations=inner_iterations,
                poll_interval_ms=poll_interval_ms,
                timeout_seconds=timeout_seconds,
            )
            runs[lane].append(record)
            for name, workload_record in record["workloads"].items():
                for digest in workload_record["result_sha256"]:
                    identities[lane][name].add(digest)
        round_index += 1
        if round_index == repetitions and _maximum_wall_spread(runs) > spread_threshold:
            target_repetitions = max_repetitions

    summaries = {lane: _summarize_lane(records) for lane, records in runs.items()}
    identity_report = {
        lane: {name: sorted(values) for name, values in lane_workloads.items()}
        for lane, lane_workloads in identities.items()
    }
    result_identity_exact = all(
        len(identity_report["baseline"][name]) == 1
        and identity_report["baseline"][name] == identity_report["candidate"][name]
        for name in manifests
    )
    gates = _performance_gates(
        summaries,
        result_identity_exact=result_identity_exact,
        spread_threshold=spread_threshold,
        regression_tolerance=regression_tolerance,
    )
    report = {
        "schema_version": NATIVE_RUNTIME_BENCHMARK_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": (
            "passed"
            if all(not gate["required"] or gate["met"] for gate in gates.values())
            else "failed"
        ),
        "claim_scope": (
            "Controlled Native runtime diagnostic only; not a five-year or release "
            "performance certification."
        ),
        "platform": {
            "system": platform.system().lower(),
            "machine": platform.machine().lower(),
            "python": platform.python_version(),
        },
        "measurement_contract": {
            "fresh_process_per_invocation": True,
            "cold_observations_excluded": 1,
            "warmups_excluded": 1,
            "initial_repetitions": repetitions,
            "measured_repetitions": len(runs["baseline"]),
            "maximum_repetitions": max_repetitions,
            "extended_to_maximum": len(runs["baseline"]) == max_repetitions,
            "inner_iterations": inner_iterations,
            "spread_threshold": spread_threshold,
            "regression_tolerance": regression_tolerance,
            "rss_scope": "maximum process-tree resident bytes",
        },
        "binaries": {
            "baseline": _binary_record(binaries["baseline"], baseline_identity),
            "candidate": _binary_record(binaries["candidate"], candidate_identity),
        },
        "workloads": {
            name: {"path": str(path), "sha256": _sha256(path)} for name, path in manifests.items()
        },
        "cold_diagnostics": cold,
        "warm_diagnostics": warmups,
        "cold_to_warm": _cold_to_warm(cold, warmups),
        "runs": runs,
        "summary": summaries,
        "result_identity": {"exact": result_identity_exact, "sha256": identity_report},
        "gates": gates,
    }
    write_json(output, report)
    return report


def _required_file(path: str | Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise BenchmarkError(f"{label} does not exist: {resolved}")
    return resolved


def _run_once(
    binary: Path,
    manifest: Path,
    output: Path,
    profile: Path,
    *,
    poll_interval_ms: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    started_ns = time.perf_counter_ns()
    process = subprocess.Popen(
        [
            str(binary),
            "--vector-manifest",
            "--profile-output",
            str(profile),
            str(manifest),
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    try:
        root: psutil.Process | None = psutil.Process(process.pid)
    except psutil.Error:
        root = None
    peak_rss_bytes = 0
    timed_out = False
    while process.poll() is None:
        if root is not None:
            peak_rss_bytes = max(peak_rss_bytes, _process_tree_rss(root))
        if (time.perf_counter_ns() - started_ns) / 1_000_000_000 > timeout_seconds:
            timed_out = True
            if root is None:
                process.kill()
            else:
                _terminate_process_tree(root)
            break
        time.sleep(poll_interval_ms / 1000)
    if root is not None:
        peak_rss_bytes = max(peak_rss_bytes, _process_tree_rss(root))
    _, stderr = process.communicate()
    wall_time_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    if process.returncode != 0 or timed_out:
        suffix = " (timed out)" if timed_out else ""
        raise BenchmarkError(f"native runtime benchmark process failed{suffix}: {stderr.strip()}")
    try:
        profile_data = json.loads(profile.read_text(encoding="utf-8"))
        event_loop_ns = profile_data["simulation"]["event_loop_ns"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"native runtime profile is invalid: {profile}") from exc
    if isinstance(event_loop_ns, bool) or not isinstance(event_loop_ns, int | float):
        raise BenchmarkError(f"native runtime event-loop profile is invalid: {profile}")
    return {
        "wall_time_seconds": wall_time_seconds,
        "event_loop_seconds": float(event_loop_ns) / 1_000_000_000,
        "peak_rss_bytes": peak_rss_bytes,
        "result_sha256": _sha256(output),
    }


def _measure_lane(
    *,
    binary: Path,
    workloads: dict[str, Path],
    work: Path,
    lane: str,
    inner_iterations: int,
    poll_interval_ms: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for name, manifest in workloads.items():
        observations = [
            _run_once(
                binary,
                manifest,
                work / f"{lane}-{name}.json",
                work / f"{lane}-{name}-profile.json",
                poll_interval_ms=poll_interval_ms,
                timeout_seconds=timeout_seconds,
            )
            for _ in range(inner_iterations)
        ]
        records[name] = {
            "wall_time_seconds": statistics.fmean(
                observation["wall_time_seconds"] for observation in observations
            ),
            "event_loop_seconds": statistics.fmean(
                observation["event_loop_seconds"] for observation in observations
            ),
            "peak_rss_bytes": max(observation["peak_rss_bytes"] for observation in observations),
            "result_sha256": sorted({observation["result_sha256"] for observation in observations}),
        }
    return {
        "wall_time_seconds_per_workload": statistics.fmean(
            record["wall_time_seconds"] for record in records.values()
        ),
        "event_loop_seconds_per_workload": statistics.fmean(
            record["event_loop_seconds"] for record in records.values()
        ),
        "peak_rss_bytes": max(record["peak_rss_bytes"] for record in records.values()),
        "workloads": records,
    }


def _metric(values: Sequence[float | int]) -> dict[str, float | int]:
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _relative_spread(values: Sequence[float]) -> float:
    median = statistics.median(values)
    return (max(values) - min(values)) / median if median > 0 else 0.0


def _summarize_lane(records: list[dict[str, Any]]) -> dict[str, Any]:
    wall = [float(record["wall_time_seconds_per_workload"]) for record in records]
    event_loop = [float(record["event_loop_seconds_per_workload"]) for record in records]
    rss = [int(record["peak_rss_bytes"]) for record in records]
    return {
        "wall_time_seconds_per_workload": {
            **_metric(wall),
            "relative_spread": _relative_spread(wall),
        },
        "event_loop_seconds_per_workload": {
            **_metric(event_loop),
            "relative_spread": _relative_spread(event_loop),
        },
        "peak_rss_bytes": _metric(rss),
    }


def _maximum_wall_spread(runs: dict[str, list[dict[str, Any]]]) -> float:
    return max(
        _relative_spread([float(record["wall_time_seconds_per_workload"]) for record in records])
        for records in runs.values()
    )


def _performance_gates(
    summaries: dict[str, dict[str, Any]],
    *,
    result_identity_exact: bool,
    spread_threshold: float,
    regression_tolerance: float,
) -> dict[str, dict[str, Any]]:
    baseline = summaries["baseline"]
    candidate = summaries["candidate"]

    def comparison(name: str, statistic: str) -> dict[str, Any]:
        baseline_value = float(baseline[name][statistic])
        candidate_value = float(candidate[name][statistic])
        limit = baseline_value * (1 + regression_tolerance)
        return {
            "met": candidate_value <= limit if baseline_value > 0 else candidate_value == 0,
            "baseline": baseline_value,
            "candidate": candidate_value,
            "maximum_allowed": limit,
            "candidate_to_baseline_ratio": _ratio(candidate_value, baseline_value),
        }

    maximum_spread = max(
        float(summaries[lane]["wall_time_seconds_per_workload"]["relative_spread"])
        for lane in summaries
    )
    return {
        "result_identity": {"required": True, "met": result_identity_exact},
        "wall_spread": {
            "required": False,
            "met": maximum_spread <= spread_threshold,
            "observed": maximum_spread,
            "maximum_allowed": spread_threshold,
            "action": "extend from three to five repetitions when exceeded",
        },
        "fresh_process_wall": {
            "required": True,
            **comparison("wall_time_seconds_per_workload", "median"),
        },
        "event_loop": {
            "required": True,
            **comparison("event_loop_seconds_per_workload", "median"),
        },
        "peak_rss": {"required": True, **comparison("peak_rss_bytes", "maximum")},
    }


def _cold_to_warm(
    cold: dict[str, dict[str, Any]],
    warm: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        lane: {
            name: {
                "wall_ratio": _ratio(
                    cold[lane][name]["wall_time_seconds"],
                    warm[lane][name]["wall_time_seconds"],
                ),
                "event_loop_ratio": _ratio(
                    cold[lane][name]["event_loop_seconds"],
                    warm[lane][name]["event_loop_seconds"],
                ),
                "peak_rss_ratio": _ratio(
                    cold[lane][name]["peak_rss_bytes"],
                    warm[lane][name]["peak_rss_bytes"],
                ),
            }
            for name in cold[lane]
        }
        for lane in cold
    }


def _process_tree_rss(root: psutil.Process) -> int:
    try:
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        processes = [root]
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except psutil.Error:
            continue
    return total


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _terminate_process_tree(root: psutil.Process) -> None:
    try:
        processes = root.children(recursive=True)
    except psutil.Error:
        processes = []
    for process in [*reversed(processes), root]:
        try:
            process.kill()
        except psutil.Error:
            continue


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binary_record(path: Path, identity: str) -> dict[str, str]:
    return {"identity": identity, "path": str(path), "sha256": _sha256(path)}


def _workload_argument(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("workload must use NAME=MANIFEST syntax")
    return name, Path(path)


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-binary", type=Path, required=True)
    parser.add_argument("--candidate-binary", type=Path, required=True)
    parser.add_argument("--baseline-identity", required=True)
    parser.add_argument("--candidate-identity", required=True)
    parser.add_argument("--workload", action="append", type=_workload_argument, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-repetitions", type=int, default=5)
    parser.add_argument("--inner-iterations", type=int, default=1)
    parser.add_argument("--spread-threshold", type=float, default=0.05)
    parser.add_argument("--regression-tolerance", type=float, default=0.05)
    parser.add_argument("--poll-interval-ms", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_arguments(arguments)
    workloads = dict(args.workload)
    if len(workloads) != len(args.workload):
        raise BenchmarkError("workload names must be unique")
    report = run_native_runtime_benchmark(
        baseline_binary=args.baseline_binary,
        candidate_binary=args.candidate_binary,
        workloads=workloads,
        output_path=args.output,
        baseline_identity=args.baseline_identity,
        candidate_identity=args.candidate_identity,
        repetitions=args.repetitions,
        max_repetitions=args.max_repetitions,
        inner_iterations=args.inner_iterations,
        spread_threshold=args.spread_threshold,
        regression_tolerance=args.regression_tolerance,
        poll_interval_ms=args.poll_interval_ms,
        timeout_seconds=args.timeout_seconds,
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
