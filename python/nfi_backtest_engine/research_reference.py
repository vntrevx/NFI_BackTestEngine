"""Official Freqtrade verification for a completed research run."""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .config_loader import config_sha256, strip_service_only_settings
from .data_seal import validate_data_seal
from .docker_runtime import managed_docker_run, run_managed_container
from .errors import BenchmarkError
from .fixture import sha256_file
from .normalize import normalize_file
from .parity import first_difference
from .reference_assets import reference_package_root, reference_tracer_root
from .reference_runtime import (
    REFERENCE_CCXT_VERSION,
    REFERENCE_IMAGE,
    REFERENCE_IMAGE_REF,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
    ensure_docker_config,
    ensure_reference_dependencies,
    ensure_reference_image,
)

RESEARCH_REFERENCE_VERSION = "1.3.0"

_RESOURCE_CAPTURE_SCRIPT = """\
freqtrade "$@"
status=$?
if [ -r /sys/fs/cgroup/memory.peak ]; then
  cat /sys/fs/cgroup/memory.peak > /output/container-memory-peak.txt
elif [ -r /sys/fs/cgroup/memory/memory.max_usage_in_bytes ]; then
  cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes > /output/container-memory-peak.txt
fi
if [ -r /sys/fs/cgroup/memory.events ]; then
  cat /sys/fs/cgroup/memory.events > /output/container-memory.events
fi
if [ -r /sys/fs/cgroup/memory.swap.current ]; then
  cat /sys/fs/cgroup/memory.swap.current > /output/container-memory-swap-current.txt
fi
if [ -r /sys/fs/cgroup/memory.swap.peak ]; then
  cat /sys/fs/cgroup/memory.swap.peak > /output/container-memory-swap-peak.txt
fi
if [ -r /sys/fs/cgroup/memory.swap.events ]; then
  cat /sys/fs/cgroup/memory.swap.events > /output/container-memory.swap.events
fi
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  cat /sys/fs/cgroup/cpu.stat > /output/container-cpu.stat
fi
if [ -r /sys/fs/cgroup/io.stat ]; then
  cat /sys/fs/cgroup/io.stat > /output/container-io.stat
fi
exit "$status"
"""


def run_research_reference(
    run_directory: str | Path,
    output_directory: str | Path,
    *,
    market_snapshot_path: str | Path | None = None,
    capture_markets: bool = True,
    audit_timestamps_ms: list[int] | None = None,
    timeout_seconds: int | None = None,
    reference_memory_mode: str = "normal",
    reference_storage_mode: str = "spooled",
    swap_cap_bytes: int | None = None,
    trace_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the pinned oracle against the exact inputs of one completed run."""
    root, run, inputs = _load_completed_run(run_directory)
    output = Path(output_directory).resolve()
    _initialize_output(output)
    input_directory = output / "inputs"
    input_directory.mkdir()
    materialized = _materialize_reference_inputs(root, run, inputs, input_directory)

    snapshot_path = output / "reference-markets.json"
    if market_snapshot_path is not None:
        source_snapshot = Path(market_snapshot_path).resolve()
        _validate_reference_market_snapshot(
            read_json(source_snapshot),
            expected_exchange=materialized["exchange"],
            expected_trading_mode=materialized["trading_mode"],
            required_pairs=materialized["market_pairs"],
        )
        shutil.copyfile(source_snapshot, snapshot_path)
    elif capture_markets:
        capture_research_markets(
            input_directory,
            snapshot_path,
            exchange=materialized["exchange"],
            trading_mode=materialized["trading_mode"],
            required_pairs=materialized["market_pairs"],
            timeout_seconds=timeout_seconds or 180,
        )
    else:
        raise BenchmarkError(
            "official research verification requires --markets or online market capture"
        )

    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)
    project_root = _project_root()
    validated_trace_identity = _validate_trace_identity(
        trace_identity,
        trading_mode=materialized["trading_mode"],
    )
    dependency_directory = (
        ensure_reference_dependencies(
            project_root=project_root,
            docker_config=docker_config,
        )
        if validated_trace_identity is not None
        else None
    )
    stdout_path = output / "stdout.log"
    stderr_path = output / "stderr.log"
    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    timed_out = False
    resources: dict[str, Any] | None = None
    audit_timestamps = _validate_audit_timestamps(audit_timestamps_ms or [])
    if reference_memory_mode not in {"normal", "certification-swap"}:
        raise BenchmarkError(
            "reference memory mode must be 'normal' or 'certification-swap'"
        )
    if reference_storage_mode not in {"in-memory", "spooled"}:
        raise BenchmarkError(
            "reference storage mode must be 'in-memory' or 'spooled'"
        )
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            with managed_docker_run(
                docker_config=docker_config,
                role="reference",
                swap_mode=(
                    "daemon"
                    if reference_memory_mode == "certification-swap"
                    else "disabled"
                ),
                swap_cap_bytes=swap_cap_bytes,
                swap_probe_image=(
                    REFERENCE_IMAGE_REF
                    if reference_memory_mode == "certification-swap"
                    else None
                ),
            ) as lease:
                resources = {
                    "daemon": lease["daemon"],
                    "policy": lease["policy"],
                    "cleaned_stopped_containers": lease["cleaned_stopped_containers"],
                }
                command = build_research_reference_command(
                    run_prefix=lease["command_prefix"],
                    input_directory=input_directory,
                    output_directory=output,
                    data_directory=materialized["data_directory"],
                    strategy=materialized["strategy"],
                    timerange=materialized["timerange"],
                    pairs=materialized["pairs"],
                    audit_timestamps_ms=audit_timestamps,
                    storage_mode=reference_storage_mode,
                    trace_identity=validated_trace_identity,
                    dependency_directory=dependency_directory,
                )
                completed = subprocess.run(
                    command,
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

    result_zip = _single_result_zip(output) if exit_code == 0 else None
    official_surface_path = output / "official-trade-surface.json"
    difference = None
    if result_zip is not None:
        official_surface = normalize_file(
            result_zip,
            official_surface_path,
            strategy=materialized["strategy"],
            surface_version="2",
        )
        difference = first_difference(materialized["engine_surface"], official_surface)

    memory_peak = _read_nonnegative_integer(output / "container-memory-peak.txt")
    memory_events = _read_integer_record(output / "container-memory.events")
    swap_current = _read_nonnegative_integer(
        output / "container-memory-swap-current.txt"
    )
    swap_peak = _read_nonnegative_integer(output / "container-memory-swap-peak.txt")
    swap_events = _read_integer_record(output / "container-memory.swap.events")
    memory = _memory_assessment(
        exit_code=exit_code,
        peak_bytes=memory_peak,
        events=memory_events,
        resources=resources,
    )
    audit_path = output / "callback-audit.json"
    trace_path = output / "state-trace.nfitrace"
    storage_path = output / "reference-storage.json"
    storage_metrics = read_json(storage_path) if storage_path.is_file() else None
    storage_complete = (
        reference_storage_mode == "in-memory"
        or (
            isinstance(storage_metrics, dict)
            and storage_metrics.get("mode") == "spooled"
            and storage_metrics.get("removed_on_exit") is True
        )
    )
    complete = exit_code == 0 and difference is None and (
        not audit_timestamps or audit_path.is_file()
    ) and (
        validated_trace_identity is None or trace_path.is_file()
    ) and storage_complete
    report = {
        "schema_version": RESEARCH_REFERENCE_VERSION,
        "run_id": run["run_id"],
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "network": "none",
        },
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(datetime.now(UTC)),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "complete": complete,
        "exact_parity": difference is None if result_zip is not None else False,
        "difference": _difference_record(difference),
        "inputs": {
            "strategy": _file_record(input_directory / "strategy.py"),
            "config": _file_record(input_directory / "config.json"),
            "market_config": _file_record(input_directory / "market-config.json"),
            "market_snapshot": _file_record(snapshot_path),
            "data_seal": _file_record(root / "data-seal.json"),
            "engine_trade_surface": _file_record(materialized["engine_surface_path"]),
        },
        "result": _file_record(result_zip) if result_zip is not None else None,
        "official_trade_surface": (
            _file_record(official_surface_path) if official_surface_path.is_file() else None
        ),
        "callback_audit": (
            {
                **_file_record(audit_path),
                "requested_timestamps_ms": audit_timestamps,
            }
            if audit_path.is_file()
            else None
        ),
        "state_trace": (
            _file_record(trace_path) if trace_path.is_file() else None
        ),
        "reference_storage": {
            "mode": reference_storage_mode,
            "complete": storage_complete,
            "metrics": storage_metrics,
            "artifact": (
                _file_record(storage_path) if storage_path.is_file() else None
            ),
        },
        "container_resources": resources,
        "container_memory": memory,
        "container_swap": {
            "mode": reference_memory_mode,
            "current_bytes_at_exit": swap_current,
            "peak_bytes": swap_peak,
            "events": swap_events,
        },
        "container_cpu": _read_integer_record(output / "container-cpu.stat"),
        "container_io": _read_io_stat(output / "container-io.stat"),
        "stdout": _file_record(stdout_path),
        "stderr": _file_record(stderr_path),
    }
    write_json(output / "run.json", report)
    return report


def capture_research_markets(
    input_directory: str | Path,
    destination: str | Path,
    *,
    exchange: str,
    trading_mode: str,
    required_pairs: list[str],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Capture the pinned Freqtrade/CCXT market state used by a research run."""
    inputs = Path(input_directory).resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise BenchmarkError(f"market snapshot destination already exists: {target}")
    output = target.parent
    (output / "user_data").mkdir(exist_ok=True)
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)
    command = build_research_market_capture_command(
        input_directory=inputs,
        output_directory=output,
    )
    completed, resources = run_managed_container(
        command,
        docker_config=docker_config,
        role="market-capture",
        capture_output=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0 or not target.is_file():
        detail = completed.stderr[-2000:].strip() or completed.stdout[-2000:].strip()
        raise BenchmarkError(f"failed to capture research markets: {detail}")
    _validate_reference_market_snapshot(
        read_json(target),
        expected_exchange=exchange,
        expected_trading_mode=trading_mode,
        required_pairs=required_pairs,
    )
    return {
        **_file_record(target),
        "docker_resources": resources,
    }


def build_research_market_capture_command(
    *,
    input_directory: Path,
    output_directory: Path,
) -> list[str]:
    """Build the lightweight Freqtrade command that triggers CCXT market loading.

    `list-pairs` already initializes the exchange and prints a human-readable
    result. The pinned Freqtrade 2026.5.1 CLI has no `--json` option for this
    command; the injected tracer writes the canonical JSON snapshot directly.
    """
    tracer_root = reference_tracer_root()
    package_root = reference_package_root()
    return [
        "--platform",
        REFERENCE_PLATFORM,
        "--workdir",
        "/input",
        "--volume",
        f"{input_directory}:/input:ro",
        "--volume",
        f"{output_directory}:/output",
        "--volume",
        f"{tracer_root}:/nfi-reference-tracer:ro",
        "--volume",
        f"{package_root}:/nfi-python/nfi_backtest_engine:ro",
        "--env",
        "PYTHONPATH=/nfi-reference-tracer:/nfi-python",
        "--env",
        "NFI_MARKET_CAPTURE_PATH=/output/reference-markets.json",
        "--entrypoint",
        "freqtrade",
        REFERENCE_IMAGE_REF,
        "list-pairs",
        "--config",
        "/input/market-config.json",
        "--userdir",
        "/output/user_data",
    ]


def build_research_reference_command(
    *,
    run_prefix: list[str],
    input_directory: Path,
    output_directory: Path,
    data_directory: Path,
    strategy: str,
    timerange: str,
    pairs: list[str],
    audit_timestamps_ms: list[int],
    storage_mode: str = "spooled",
    trace_identity: dict[str, str] | None = None,
    dependency_directory: Path | None = None,
) -> list[str]:
    """Build a shell-safe Docker argv for one official research rerun."""
    tracer_root = reference_tracer_root()
    package_root = reference_package_root()
    command = [
        *run_prefix,
        "--platform",
        REFERENCE_PLATFORM,
        "--network",
        "none",
        "--workdir",
        "/input",
        "--volume",
        f"{input_directory}:/input:ro",
        "--volume",
        f"{output_directory}:/output",
        "--volume",
        f"{data_directory}:/data:ro",
        "--volume",
        f"{tracer_root}:/nfi-reference-tracer:ro",
        "--volume",
        f"{package_root}:/nfi-python/nfi_backtest_engine:ro",
        "--env",
        "PYTHONPATH=/nfi-reference-tracer:/nfi-python"
        + (":/reference-deps" if dependency_directory is not None else ""),
        "--env",
        "NFI_MARKET_SNAPSHOT_PATH=/output/reference-markets.json",
    ]
    if storage_mode == "spooled":
        command.extend(
            [
                "--env",
                "NFI_REFERENCE_DATASTORE=spooled",
                "--env",
                "NFI_REFERENCE_STORAGE_REPORT=/output/reference-storage.json",
                "--env",
                "NFI_REFERENCE_SPOOL_DIRECTORY=/tmp/nfi-reference-spool",
            ]
        )
    elif storage_mode != "in-memory":
        raise BenchmarkError(
            "reference storage mode must be 'in-memory' or 'spooled'"
        )
    if audit_timestamps_ms:
        command.extend(
            [
                "--env",
                "NFI_CALLBACK_AUDIT_PATH=/output/callback-audit.json",
                "--env",
                "NFI_CALLBACK_AUDIT_TIMESTAMPS_MS="
                + ",".join(str(value) for value in audit_timestamps_ms),
            ]
        )
    if dependency_directory is not None:
        command.extend(
            ["--volume", f"{dependency_directory}:/reference-deps:ro"]
        )
    if trace_identity is not None:
        command.extend(
            [
                "--env",
                "NFI_TRACE_PATH=/output/state-trace.nfitrace",
                "--env",
                f"NFI_TRACE_RUN_ID={trace_identity['run_id']}",
                "--env",
                f"NFI_TRACE_INPUT_SHA256={trace_identity['input_sha256']}",
                "--env",
                f"NFI_TRACE_STRATEGY_SHA256={trace_identity['strategy_sha256']}",
                "--env",
                f"NFI_TRACE_PROFILE_SHA256={trace_identity['profile_sha256']}",
                "--env",
                "NFI_TRACE_INCLUDE_STATE=1",
            ]
        )
    command.extend(
        [
            "--entrypoint",
            "/bin/sh",
            REFERENCE_IMAGE_REF,
            "-c",
            _RESOURCE_CAPTURE_SCRIPT,
            "nfi-research-reference",
            "backtesting",
            "--config",
            "/input/config.json",
            "--userdir",
            "/output/user_data",
            "--strategy",
            strategy,
            "--strategy-path",
            "/input",
            "--datadir",
            "/data",
            "--timerange",
            timerange,
            "--pairs",
            *pairs,
            "--cache",
            "none",
            "--export",
            "trades",
            "--backtest-directory",
            "/output",
        ]
    )
    return command


def _validate_trace_identity(
    identity: dict[str, Any] | None,
    *,
    trading_mode: str,
) -> dict[str, str] | None:
    if identity is None:
        return None
    required = {
        "run_id",
        "input_sha256",
        "strategy_sha256",
        "profile_sha256",
        "trading_mode",
    }
    if not isinstance(identity, dict) or set(identity) != required:
        raise BenchmarkError("research trace identity fields are invalid")
    if identity["trading_mode"] != trading_mode:
        raise BenchmarkError("research trace identity trading mode differs")
    for field in ("input_sha256", "strategy_sha256", "profile_sha256"):
        value = identity[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BenchmarkError(f"research trace identity {field} is not SHA-256")
    run_id = identity["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise BenchmarkError("research trace identity run_id must be non-empty")
    return {field: str(identity[field]) for field in sorted(required)}


def _load_completed_run(
    run_directory: str | Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"research run.json does not exist: {run_path}")
    run = read_json(run_path)
    if run.get("status") != "complete" or not isinstance(run.get("result"), dict):
        raise BenchmarkError("only a complete research run can use the official reference")
    inputs = run.get("inputs")
    if not isinstance(inputs, dict):
        raise BenchmarkError("research run has no sealed input identity")
    validate_data_seal(root / "data-seal.json")
    return root, run, inputs


def _materialize_reference_inputs(
    root: Path,
    run: dict[str, Any],
    inputs: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    strategy_record = inputs.get("strategy")
    config_record = inputs.get("config")
    if not isinstance(strategy_record, dict) or not isinstance(config_record, dict):
        raise BenchmarkError("research strategy/config identity is invalid")
    sealed_strategy = _sealed_input_path(root, strategy_record.get("sealed"))
    strategy_source = (
        sealed_strategy
        if sealed_strategy is not None
        else Path(str(strategy_record.get("path", ""))).resolve()
    )
    expected_strategy_hash = strategy_record.get("file_sha256")
    if (
        not strategy_source.is_file()
        or not isinstance(expected_strategy_hash, str)
        or sha256_file(strategy_source) != expected_strategy_hash
    ):
        raise BenchmarkError("research strategy source no longer matches its sealed hash")
    strategy_copy = destination / "strategy.py"
    shutil.copyfile(strategy_source, strategy_copy)

    sealed_config = _sealed_input_path(root, config_record.get("sealed"))
    if sealed_config is not None:
        config = read_json(sealed_config)
    else:
        effective = read_json(root / "effective-config.redacted.json")
        config = effective.get("config")
    expected_config_hash = config_record.get("run_effective_sha256")
    if (
        not isinstance(config, dict)
        or not isinstance(expected_config_hash, str)
        or config_sha256(config) != expected_config_hash
    ):
        raise BenchmarkError("research effective config failed its hash binding")
    write_json(destination / "config.json", _official_backtest_config(config))

    market_config = read_json(root / "download-config.json")
    if not isinstance(market_config, dict):
        raise BenchmarkError("research market/download config is invalid")
    market_config = _official_backtest_config(market_config)
    write_json(destination / "market-config.json", market_config)
    exchange_config = config.get("exchange")
    market_exchange = market_config.get("exchange")
    if not isinstance(exchange_config, dict) or not isinstance(market_exchange, dict):
        raise BenchmarkError("research exchange config is invalid")
    pairs = exchange_config.get("pair_whitelist")
    market_pairs = market_exchange.get("pair_whitelist")
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
        or not isinstance(market_pairs, list)
        or not market_pairs
        or not all(isinstance(pair, str) and pair for pair in market_pairs)
    ):
        raise BenchmarkError("research pairlists are invalid")

    result = run["result"]
    surface_record = result.get("trade_surface")
    if not isinstance(surface_record, dict) or not isinstance(surface_record.get("path"), str):
        raise BenchmarkError("research run has no engine trade surface")
    engine_surface_path = Path(surface_record["path"]).resolve()
    if (
        not engine_surface_path.is_relative_to(root)
        or not engine_surface_path.is_file()
        or sha256_file(engine_surface_path) != surface_record.get("sha256")
    ):
        raise BenchmarkError("research engine trade surface failed its hash binding")
    data_directory = Path(str(inputs.get("data_directory", ""))).resolve()
    if not data_directory.is_dir():
        raise BenchmarkError(f"research data directory does not exist: {data_directory}")
    timerange = inputs.get("timerange")
    strategy_name = strategy_record.get("class_name")
    exchange = exchange_config.get("name")
    trading_mode = config.get("trading_mode", "spot")
    if (
        not isinstance(timerange, str)
        or not timerange
        or not isinstance(strategy_name, str)
        or not strategy_name
        or not isinstance(exchange, str)
        or not exchange
        or not isinstance(trading_mode, str)
        or not trading_mode
    ):
        raise BenchmarkError("research reference identity contains invalid strings")
    return {
        "strategy": strategy_name,
        "timerange": timerange,
        "pairs": pairs,
        "market_pairs": market_pairs,
        "exchange": exchange.lower(),
        "trading_mode": trading_mode,
        "data_directory": data_directory,
        "engine_surface_path": engine_surface_path,
        "engine_surface": read_json(engine_surface_path),
    }


def _official_backtest_config(config: dict[str, Any]) -> dict[str, Any]:
    """Remove service-only settings from the immutable backtest copy.

    User configs commonly leave API credentials blank because the service is
    never started during backtesting. Newer Freqtrade releases validate those
    fields even for read-only commands such as `list-pairs`. The original
    document is hash-checked before this function runs; only the disposable
    official-oracle copy drops the unrelated API server section.

    The native manifest contains the explicit, sealed pair order. Freqtrade's
    default StaticPairList filters markets that happen to be inactive today,
    which would silently change a historical run after a delisting. Enabling
    its documented `allow_inactive` option makes the oracle consume that same
    immutable universe. Dynamic pairlists are rejected because the native run
    did not execute their live filtering policy.
    """
    result = strip_service_only_settings(config)
    pairlists = result.get("pairlists")
    if pairlists is None:
        result["pairlists"] = [{"method": "StaticPairList", "allow_inactive": True}]
    elif (
        not isinstance(pairlists, list)
        or not pairlists
        or any(
            not isinstance(item, dict) or item.get("method") != "StaticPairList"
            for item in pairlists
        )
    ):
        raise BenchmarkError(
            "official research verification requires a static sealed pairlist"
        )
    else:
        for item in pairlists:
            item["allow_inactive"] = True
    return result


def _sealed_input_path(root: Path, record: Any) -> Path | None:
    if record is None:
        return None
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("path"), str)
        or not isinstance(record.get("sha256"), str)
    ):
        raise BenchmarkError("research sealed-input record is invalid")
    path = (root / record["path"]).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or sha256_file(path) != record["sha256"]
    ):
        raise BenchmarkError("research sealed input failed its hash binding")
    return path


def _validate_reference_market_snapshot(
    snapshot: Any,
    *,
    expected_exchange: str,
    expected_trading_mode: str,
    required_pairs: list[str],
) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "1.0.0":
        raise BenchmarkError("reference market snapshot has an invalid schema")
    expected = {
        "freqtrade_version": REFERENCE_VERSION,
        "ccxt_version": REFERENCE_CCXT_VERSION,
        "exchange": expected_exchange,
        "trading_mode": expected_trading_mode,
    }
    for field, value in expected.items():
        if snapshot.get(field) != value:
            raise BenchmarkError(
                f"reference market snapshot {field} differs: "
                f"expected {value!r}, actual {snapshot.get(field)!r}"
            )
    markets = snapshot.get("markets")
    if not isinstance(markets, dict):
        raise BenchmarkError("reference market snapshot markets must be an object")
    missing = [pair for pair in required_pairs if pair not in markets]
    if missing:
        raise BenchmarkError(
            "reference market snapshot is missing pairs: " + ", ".join(missing)
        )


def _initialize_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"research reference output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "user_data").mkdir()


def _single_result_zip(output: Path) -> Path:
    candidates = sorted(output.glob("backtest-result-*.zip"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise BenchmarkError(f"expected one official result ZIP; found {names}")
    return candidates[0]


def _validate_audit_timestamps(values: list[int]) -> list[int]:
    normalized = sorted(set(values))
    if any(isinstance(value, bool) or value < 0 for value in normalized):
        raise BenchmarkError("callback audit timestamps must be non-negative milliseconds")
    return normalized


def _memory_assessment(
    *,
    exit_code: int,
    peak_bytes: int | None,
    events: dict[str, int] | None,
    resources: dict[str, Any] | None,
) -> dict[str, Any]:
    policy = resources.get("policy") if isinstance(resources, dict) else None
    raw_limit = policy.get("container_memory_limit_bytes") if isinstance(policy, dict) else None
    limit = raw_limit if isinstance(raw_limit, int) and raw_limit > 0 else None
    ratio = peak_bytes / limit if peak_bytes is not None and limit is not None else None
    oom_kills = events.get("oom_kill", 0) if events is not None else 0
    if oom_kills:
        verdict = "oom_killed"
    elif exit_code in {137, -9}:
        verdict = "possible_oom"
    elif ratio is not None and ratio >= 0.9:
        verdict = "near_limit"
    elif peak_bytes is not None:
        verdict = "within_limit"
    else:
        verdict = "unmeasured"
    return {
        "verdict": verdict,
        "limit_bytes": limit,
        "peak_bytes": peak_bytes,
        "peak_ratio": ratio,
        "oom_kill_count": oom_kills,
        "events": events,
    }


def _read_nonnegative_integer(path: Path) -> int | None:
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return int(value) if value.isdigit() else None


def _read_integer_record(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    record: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            record[parts[0]] = int(parts[1])
    return record or None


def _read_io_stat(path: Path) -> list[dict[str, Any]] | None:
    """Parse cgroup-v2 per-device IO counters without assuming device names."""
    if not path.is_file():
        return None
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        counters: dict[str, int] = {}
        for token in parts[1:]:
            name, separator, raw_value = token.partition("=")
            if separator and raw_value.isdigit():
                counters[name] = int(raw_value)
        records.append({"device": parts[0], "counters": counters})
    return records or None


def _difference_record(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
    }


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_string(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
