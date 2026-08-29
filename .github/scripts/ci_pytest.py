#!/usr/bin/env python3
"""Run pytest while publishing deterministic per-test timing and resource evidence."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

PYTEST_REPORT_SCHEMA_VERSION = "1.0.0"
VALID_OUTCOMES = {"passed", "failed", "skipped", "xfailed", "xpassed", "error"}


def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid timing report identity source: {source}") from exc
    if not isinstance(value, dict):
        raise ValueError("timing report identity source must be an object")
    return value


def ownership_for_nodeid(nodeid: str) -> str:
    """Classify a test into a stable actionable ownership group."""
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    name = Path(path).name
    if path.startswith("tests/parity/") or "parity" in name:
        return "parity"
    if name in {"test_release_contract.py", "test_platform_benchmark.py"}:
        return "release_ci"
    if "compatibility" in name or "discovery" in name:
        return "compatibility"
    if "runtime" in name or name in {"test_engine.py", "test_cli.py"}:
        return "runtime"
    if path.startswith("tests/"):
        return "core"
    return "external"


def build_test_record(
    *,
    nodeid: str,
    outcome: str,
    wall_seconds: float,
    cpu_seconds: float,
    peak_rss_bytes: int,
) -> dict[str, Any]:
    """Build one machine-readable test record with a stable nodeid-derived ID."""
    if not nodeid:
        raise ValueError("pytest nodeid must not be empty")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid pytest outcome: {outcome}")
    if (
        not math.isfinite(wall_seconds)
        or not math.isfinite(cpu_seconds)
        or wall_seconds < 0
        or cpu_seconds < 0
        or peak_rss_bytes < 0
    ):
        raise ValueError("pytest resource measurements must be finite and non-negative")
    return {
        "nodeid": nodeid,
        "test_id": hashlib.sha256(nodeid.encode("utf-8")).hexdigest(),
        "owner": ownership_for_nodeid(nodeid),
        "outcome": outcome,
        "duration_seconds": round(wall_seconds, 6),
        "resources": {
            "cpu_seconds": round(cpu_seconds, 6),
            "peak_rss_bytes": peak_rss_bytes,
        },
    }


def build_pytest_report(
    records: Sequence[Mapping[str, Any]],
    *,
    slowest_count: int,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Order test records and derive deterministic slowest/resource summaries."""
    if slowest_count <= 0:
        raise ValueError("slowest test count must be positive")
    ordered = sorted((dict(record) for record in records), key=lambda record: record["nodeid"])
    nodeids = [record.get("nodeid") for record in ordered]
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("pytest timing nodeids must be unique")
    slowest = sorted(
        ordered,
        key=lambda record: (-float(record["duration_seconds"]), str(record["nodeid"])),
    )[:slowest_count]
    outcomes: dict[str, int] = {}
    for record in ordered:
        outcome = str(record["outcome"])
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "schema_version": PYTEST_REPORT_SCHEMA_VERSION,
        "kind": "pytest-test-timing",
        "identity": dict(identity or {}),
        "test_count": len(ordered),
        "outcomes": dict(sorted(outcomes.items())),
        "resources": {
            "cpu_seconds": round(
                sum(float(record["resources"]["cpu_seconds"]) for record in ordered),
                6,
            ),
            "peak_rss_bytes": max(
                (int(record["resources"]["peak_rss_bytes"]) for record in ordered),
                default=0,
            ),
        },
        "tests": ordered,
        "slowest_tests": [
            {
                "nodeid": record["nodeid"],
                "test_id": record["test_id"],
                "owner": record["owner"],
                "duration_seconds": record["duration_seconds"],
                "resources": dict(record["resources"]),
            }
            for record in slowest
        ],
    }


def _windows_peak_rss_bytes() -> int:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    get_process_memory_info.restype = ctypes.c_int
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not get_process_memory_info(
        get_current_process(), ctypes.byref(counters), counters.cb
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def peak_rss_bytes() -> int:
    """Return process peak RSS in bytes on Linux, macOS, and Windows."""
    if os.name == "nt":
        return _windows_peak_rss_bytes()
    import resource

    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


class _TimingPlugin:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._phase_reports: dict[str, list[Any]] = {}

    def pytest_runtest_logreport(self, report: Any) -> None:
        self._phase_reports.setdefault(str(report.nodeid), []).append(report)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: Any, nextitem: Any) -> Any:
        del nextitem
        nodeid = str(item.nodeid)
        self._phase_reports[nodeid] = []
        cpu_started = time.process_time_ns()
        yield
        cpu_seconds = (time.process_time_ns() - cpu_started) / 1_000_000_000
        reports = self._phase_reports.pop(nodeid)
        call = next((report for report in reports if report.when == "call"), None)
        if any(report.failed for report in reports):
            outcome = "failed" if call is not None and call.failed else "error"
        elif any(report.skipped for report in reports):
            was_xfail = any(hasattr(report, "wasxfail") for report in reports)
            outcome = "xfailed" if was_xfail else "skipped"
        elif call is not None and hasattr(call, "wasxfail"):
            outcome = "xpassed"
        else:
            outcome = "passed"
        self.records.append(
            build_test_record(
                nodeid=nodeid,
                outcome=outcome,
                wall_seconds=sum(float(report.duration) for report in reports),
                cpu_seconds=cpu_seconds,
                peak_rss_bytes=peak_rss_bytes(),
            )
        )


def run_pytest(
    pytest_args: Sequence[str],
    *,
    output: str | Path,
    timing_report: str | Path,
    slowest_count: int,
) -> int:
    """Run pytest in-process and persist test evidence even when tests fail."""
    source = _read_json_object(timing_report)
    identity = source.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("timing report identity must be an object")
    plugin = _TimingPlugin()
    exit_code = int(pytest.main(list(pytest_args), plugins=[plugin]))
    report = build_pytest_report(
        plugin.records,
        slowest_count=slowest_count,
        identity={
            field: identity.get(field)
            for field in (
                "report_id",
                "workflow",
                "workflow_ref",
                "repository",
                "run_id",
                "run_attempt",
                "commit_sha",
                "os",
                "python",
                "suite",
                "cache",
                "build",
            )
        },
    )
    _write_json(output, report)
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timing-report", type=Path, required=True)
    parser.add_argument("--slowest-count", type=int, default=20)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    try:
        return run_pytest(
            pytest_args,
            output=args.output,
            timing_report=args.timing_report,
            slowest_count=args.slowest_count,
        )
    except ValueError as exc:
        print(f"ci_pytest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
