"""Pinned, offline Freqtrade reference execution for sealed fixtures."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .docker_environment import docker_subprocess_environment
from .docker_runtime import managed_docker_run, run_managed_container
from .errors import BenchmarkError, TraceError
from .fixture import validate_fixture
from .normalize import normalize_file
from .parity import ParityDifference, first_difference
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
    reference_dependency_lock,
    reference_runtime_volume,
    validate_reference_dependencies,
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
from .reference_tracer.nfi_reference_trace import REFERENCE_STATE_SCHEMA_VERSION
from .specs import validate_trade_surface
from .state_trace import (
    TraceDifference,
    iter_validated_trace_events,
    trace_summary,
)

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


_REFERENCE_HEADER_FIELDS = (
    "schema_version",
    "input_sha256",
    "strategy_sha256",
    "profile_sha256",
    "trading_mode",
)


def _first_reference_trace_difference(
    expected_path: str | Path,
    actual_path: str | Path,
) -> TraceDifference | None:
    expected_summary = trace_summary(expected_path)
    actual_summary = trace_summary(actual_path)
    if not expected_summary["include_state"] or not actual_summary["include_state"]:
        raise TraceError("reference trace comparison requires materialized source records")
    for field in _REFERENCE_HEADER_FIELDS:
        if expected_summary[field] != actual_summary[field]:
            return TraceDifference(
                sequence=None,
                path=f"$.header.{field}",
                expected=expected_summary[field],
                actual=actual_summary[field],
                reason="header value differs",
            )

    expected_events = iter_validated_trace_events(expected_path)
    actual_events = iter_validated_trace_events(actual_path)
    sequence = 0
    while True:
        expected_event = next(expected_events, None)
        actual_event = next(actual_events, None)
        if expected_event is None or actual_event is None:
            break
        expected_key = _reference_event_key(expected_event)
        actual_key = _reference_event_key(actual_event)
        difference = first_difference(expected_key, actual_key, "$.event_key")
        if difference is not None:
            return _reference_trace_difference(sequence, difference, expected_key)
        expected_state = expected_event.get("state")
        actual_state = actual_event.get("state")
        if not isinstance(expected_state, dict) or not isinstance(actual_state, dict):
            raise TraceError("reference trace event requires materialized state objects")
        comparable_actual = _reference_state_for_comparison(expected_state, actual_state)
        difference = first_difference(expected_state, comparable_actual, "$.state")
        if difference is not None:
            return _reference_trace_difference(sequence, difference, expected_key)
        sequence += 1

    if expected_event is not None or actual_event is not None:
        remaining_event = expected_event
        if remaining_event is None:
            remaining_event = actual_event
        if remaining_event is None:
            raise TraceError("reference trace event iterator ended inconsistently")
        return TraceDifference(
            sequence=sequence,
            path="$.events.length",
            expected=expected_summary["event_count"],
            actual=actual_summary["event_count"],
            reason="event count differs",
            event_key=_reference_event_key(remaining_event),
        )
    return None


def _reference_state_for_comparison(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> dict[str, Any]:
    expected_schema = expected.get("schema_version")
    actual_schema = actual.get("schema_version")
    if expected_schema == REFERENCE_STATE_SCHEMA_VERSION:
        _validate_reference_v2_state(expected, "expected")
    elif expected_schema is not None:
        raise TraceError(f"unsupported expected reference state schema: {expected_schema!r}")
    if actual_schema == REFERENCE_STATE_SCHEMA_VERSION:
        _validate_reference_v2_state(actual, "actual")
    elif actual_schema is not None:
        raise TraceError(f"unsupported actual reference state schema: {actual_schema!r}")

    if expected_schema == actual_schema:
        return dict(actual)
    if expected_schema is None and actual_schema == REFERENCE_STATE_SCHEMA_VERSION:
        migrated = dict(actual)
        migrated.pop("schema_version")
        migrated["trades"] = [
            trade for trade in actual["trades"] if not trade["is_open"]
        ]
        return migrated
    raise TraceError("reference state schema migration must be legacy expected to v2 actual")


def _validate_reference_v2_state(state: Mapping[str, Any], label: str) -> None:
    trades = state.get("trades")
    if not isinstance(trades, list):
        raise TraceError(f"{label} reference v2 trades must be an array")
    open_trade_count = 0
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict) or not isinstance(trade.get("is_open"), bool):
            raise TraceError(
                f"{label} reference v2 trade {index} requires boolean is_open"
            )
        open_trade_count += int(trade["is_open"])
    if open_trade_count != state.get("open_trade_count"):
        raise TraceError(
            f"{label} reference v2 open trade records differ from open_trade_count"
        )


def _reference_event_key(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": event["sequence"],
        "timestamp_ms": event["timestamp_ms"],
        "phase": event["phase"],
        "pair": event["pair"],
        "callback": event["callback"],
    }

def _reference_trace_difference(
    sequence: int,
    difference: ParityDifference,
    event_key: dict[str, Any],
) -> TraceDifference:
    return TraceDifference(
        sequence=sequence,
        path=difference.path,
        expected=difference.expected,
        actual=difference.actual,
        reason=difference.reason,
        event_key=event_key,
    )


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
            dependency_guard = (
                reference_dependency_lock(project_root)
                if dependency_directory is not None
                else nullcontext()
            )
            with dependency_guard, reference_runtime_volume(
                docker_config
            ) as runtime_volume, managed_docker_run(
                docker_config=docker_config,
                role="reference",
            ) as lease:
                if dependency_directory is not None:
                    validate_reference_dependencies(dependency_directory)
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
                    runtime_volume=runtime_volume,
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
                    env=docker_subprocess_environment(),
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
            trace_difference = _first_reference_trace_difference(
                expected_trace_path,
                trace_path,
            )
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
