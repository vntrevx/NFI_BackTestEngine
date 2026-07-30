"""Pinned, offline Freqtrade reference execution for sealed fixtures."""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .docker_runtime import managed_docker_run, run_managed_container
from .errors import BenchmarkError
from .fixture import validate_fixture
from .normalize import normalize_file
from .parity import first_difference
from .profiling import aggregate_profile_events
from .reference import execution as _reference_execution

# Binance intentionally serves leverage brackets from an authenticated endpoint.
# Freqtrade dry-run/backtest therefore uses the versioned table bundled in its
# distribution. Reading that table from the digest-pinned reference image keeps the
# engine's liquidation contract tied to the same oracle without adding Freqtrade as a
# host-side dependency.
from .reference.contracts import (
    REFERENCE_BLAKE3_VERSION,
    REFERENCE_CCXT_VERSION,
    REFERENCE_CONFIG_DIGEST,
    REFERENCE_DOCKER_IMAGE_IDS,
    REFERENCE_IMAGE,
    REFERENCE_IMAGE_REF,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_REPORT_VERSION,
    REFERENCE_TRACER_VERSION,
    REFERENCE_VERSION,
    SUPPORTED_REFERENCE_TRACER_VERSIONS,
)
from .reference.execution import (
    _project_root,
    _validate_reference_pin,
    ensure_docker_config,
    ensure_reference_dependencies,
    ensure_reference_image,
)
from .reference.storage import (
    _container_memory_assessment,
    _file_record,
    _find_result_zip,
    _initialize_output_directory,
    _read_cpu_stat,
    _read_integer_record,
    _read_io_stat,
    _read_nonnegative_integer,
    _reference_market_input,
)
from .reference.trace import (
    _parity_difference_record,
    _trace_difference_record,
    _utc_string,
)
from .specs import validate_trade_surface
from .state_trace import first_trace_difference, trace_summary

__all__ = [
    "REFERENCE_BLAKE3_VERSION",
    "REFERENCE_CCXT_VERSION",
    "REFERENCE_CONFIG_DIGEST",
    "REFERENCE_DOCKER_IMAGE_IDS",
    "REFERENCE_IMAGE",
    "REFERENCE_IMAGE_REF",
    "REFERENCE_INDEX_DIGEST",
    "REFERENCE_PLATFORM",
    "REFERENCE_PLATFORM_DIGEST",
    "REFERENCE_REPORT_VERSION",
    "REFERENCE_TRACER_VERSION",
    "REFERENCE_VERSION",
    "SUPPORTED_REFERENCE_TRACER_VERSIONS",
    "build_reference_docker_command",
    "capture_reference_markets",
    "ensure_docker_config",
    "ensure_reference_dependencies",
    "ensure_reference_image",
    "load_reference_leverage_tiers",
    "run_reference_fixture",
]


def build_reference_docker_command(*args: Any, **kwargs: Any) -> list[str]:
    """Compatibility entry point retaining the patchable Docker lookup surface."""
    _reference_execution.shutil = shutil  # pyright: ignore[reportPrivateImportUsage]
    return _reference_execution.build_reference_docker_command(*args, **kwargs)


def capture_reference_markets(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point for pinned reference market capture."""
    _reference_execution.ensure_docker_config = (  # pyright: ignore[reportPrivateImportUsage]
        ensure_docker_config
    )
    _reference_execution.ensure_reference_image = (  # pyright: ignore[reportPrivateImportUsage]
        ensure_reference_image
    )
    return _reference_execution.capture_reference_markets(*args, **kwargs)


def load_reference_leverage_tiers(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point retaining the patchable container runner."""
    _reference_execution.ensure_docker_config = (  # pyright: ignore[reportPrivateImportUsage]
        ensure_docker_config
    )
    _reference_execution.ensure_reference_image = (  # pyright: ignore[reportPrivateImportUsage]
        ensure_reference_image
    )
    _reference_execution.run_managed_container = (  # pyright: ignore[reportPrivateImportUsage]
        run_managed_container
    )
    return _reference_execution.load_reference_leverage_tiers(*args, **kwargs)


def run_reference_fixture(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    trace_mode: str = "off",
    profile: bool = True,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one sealed fixture in the pinned container and compare official evidence."""
    if trace_mode not in {"off", "hash", "full"}:
        raise BenchmarkError("trace_mode must be one of: off, hash, full")

    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    _validate_reference_pin(manifest)
    market_snapshot = _reference_market_input(manifest)
    fixture_root = manifest_file.parent
    output = Path(output_directory).resolve()
    _initialize_output_directory(output)
    project_root = _project_root()
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)

    dependency_directory: Path | None = None
    if trace_mode != "off":
        dependency_directory = ensure_reference_dependencies(
            project_root=project_root,
            docker_config=docker_config,
        )

    stdout_path = output / "stdout.log"
    stderr_path = output / "stderr.log"
    profile_path = output / "profile.jsonl"
    trace_path = output / "state-trace.nfitrace"
    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    timed_out = False
    container_resources: dict[str, Any] | None = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            with managed_docker_run(
                docker_config=docker_config,
                role="reference",
            ) as lease:
                docker_argv = build_reference_docker_command(
                    manifest,
                    fixture_root=fixture_root,
                    output_directory=output,
                    dependency_directory=dependency_directory,
                    trace_mode=trace_mode,
                    profile=profile,
                    docker_config=docker_config,
                    market_snapshot=market_snapshot,
                    run_prefix=lease["command_prefix"],
                )
                container_resources = {
                    "daemon": lease["daemon"],
                    "policy": lease["policy"],
                    "cleaned_stopped_containers": lease["cleaned_stopped_containers"],
                }
                completed = subprocess.run(
                    docker_argv,
                    cwd=project_root,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=timeout_seconds,
                )
                exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
        except OSError as exc:
            raise BenchmarkError(f"cannot execute Docker: {exc}") from exc
    ended_at = datetime.now(UTC)
    container_peak_memory = _read_nonnegative_integer(output / "container-memory-peak.txt")
    container_memory_events = _read_integer_record(output / "container-memory.events")
    container_swap_current = _read_nonnegative_integer(output / "container-memory-swap-current.txt")
    container_swap_peak = _read_nonnegative_integer(output / "container-memory-swap-peak.txt")
    container_swap_events = _read_integer_record(output / "container-memory.swap.events")
    container_memory = _container_memory_assessment(
        exit_code=exit_code,
        peak_bytes=container_peak_memory,
        events=container_memory_events,
        resources=container_resources,
    )

    report: dict[str, Any] = {
        "schema_version": REFERENCE_REPORT_VERSION,
        "fixture_id": manifest["fixture_id"],
        "manifest_path": str(manifest_file),
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "network": "none",
            "tracer_version": REFERENCE_TRACER_VERSION if trace_mode != "off" else None,
        },
        "trace_mode": trace_mode,
        "profile_enabled": profile,
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(ended_at),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "container_resources": container_resources,
        "container_peak_memory_bytes": container_peak_memory,
        "container_memory_events": container_memory_events,
        "container_memory": container_memory,
        "container_swap": {
            "mode": "disabled",
            "current_bytes_at_exit": container_swap_current,
            "peak_bytes": container_swap_peak,
            "events": container_swap_events,
        },
        "container_cpu": _read_cpu_stat(output / "container-cpu.stat"),
        "container_io": _read_io_stat(output / "container-io.stat"),
        "stdout": _file_record(stdout_path),
        "stderr": _file_record(stderr_path),
        "result": None,
        "trade_surface": None,
        "profile": None,
        "state_trace": None,
        "parity": {
            "trade_surface": {"equal": False, "difference": None},
            "state_trace": None,
        },
        "complete": False,
    }

    if exit_code == 0:
        result_zip = _find_result_zip(output)
        surface_path = output / "trade-surface.json"
        surface = normalize_file(
            result_zip,
            surface_path,
            strategy=manifest["freqtrade"]["strategy"],
            surface_version="2",
        )
        expected_surface_path = (
            fixture_root / manifest["artifacts"]["trade_surface"]["path"]
        ).resolve()
        expected_surface = read_json(expected_surface_path)
        validate_trade_surface(expected_surface)
        difference = first_difference(expected_surface, surface)
        report["result"] = _file_record(result_zip)
        report["trade_surface"] = _file_record(surface_path)
        report["parity"]["trade_surface"] = {
            "equal": difference is None,
            "difference": _parity_difference_record(difference),
        }

        if profile:
            report["profile"] = (
                aggregate_profile_events(profile_path)
                if profile_path.is_file()
                else {
                    "schema_version": "1.0.0",
                    "phases": {},
                    "missing_phases": list(manifest["measurement"]["required_profile_phases"]),
                }
            )

        if trace_mode != "off":
            actual_trace_summary = trace_summary(trace_path)
            expected_trace_path = (
                fixture_root / manifest["artifacts"]["state_trace"]["path"]
            ).resolve()
            trace_difference = first_trace_difference(expected_trace_path, trace_path)
            report["state_trace"] = {
                **_file_record(trace_path),
                "summary": actual_trace_summary,
            }
            report["parity"]["state_trace"] = {
                "equal": trace_difference is None,
                "difference": _trace_difference_record(trace_difference),
            }

        profile_complete = not profile or not report["profile"]["missing_phases"]
        trace_complete = trace_mode == "off" or bool(report["parity"]["state_trace"]["equal"])
        report["complete"] = (
            bool(report["parity"]["trade_surface"]["equal"]) and profile_complete and trace_complete
        )

    write_json(output / "run.json", report)
    return report
