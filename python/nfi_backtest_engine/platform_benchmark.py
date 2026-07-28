"""Installed-wheel portability and performance evidence across supported hosts."""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import read_json, write_json
from .engine_runtime import build_engine
from .errors import BenchmarkError, SpecValidationError
from .evidence_bundle import public_hardware_record, write_evidence_bundle
from .fixture import sha256_file, validate_fixture
from .full_x7_certification import (
    validate_full_x7_inputs,
    verify_installed_wheel,
)
from .hardware import inspect_hardware
from .performance_gate import measure_cli_process
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_CERTIFICATION_REPETITIONS,
)
from .release_contract import release_contract_for_config
from .timerange import parse_timerange_milliseconds

PLATFORM_BENCHMARK_VERSION = "1.2.0"
RAW_INPUT_LANE = "portable-raw-input"
EXACT_FIXTURE_LANE = "exact-fixture"
PORTABLE_PAIR_COUNT = 20
REQUIRED_PLATFORM_SYSTEMS = frozenset({"windows", "linux", "darwin"})
REQUIRED_PLATFORM_MACHINES = {
    "windows": frozenset({"amd64", "x86_64"}),
    "linux": frozenset({"amd64", "x86_64"}),
    "darwin": frozenset({"arm64", "aarch64"}),
}


def run_platform_benchmark(
    release_lock_path: str | Path,
    output_directory: str | Path,
    *,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    wheel_path: str | Path,
    execution_profile_path: str | Path,
    repetitions: int = MIN_CERTIFICATION_REPETITIONS,
    timeout_seconds: int,
    pair_count: int = PORTABLE_PAIR_COUNT,
) -> dict[str, Any]:
    """Measure a portable raw-input pipeline using only the installed wheel."""
    if repetitions < MIN_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"platform benchmark requires at least {MIN_CERTIFICATION_REPETITIONS} runs"
        )
    if repetitions > MAX_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"platform benchmark permits at most {MAX_CERTIFICATION_REPETITIONS} runs"
        )
    if pair_count < 1 or pair_count > PORTABLE_PAIR_COUNT:
        raise BenchmarkError(
            f"portable pair count must be between 1 and {PORTABLE_PAIR_COUNT}"
        )
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"platform benchmark output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    inputs = validate_full_x7_inputs(
        release_lock_path=release_lock_path,
        strategy_path=strategy_path,
        class_name=class_name,
        config_path=config_path,
        data_directory=data_directory,
        engine_market_snapshot=engine_market_snapshot,
        reference_market_snapshot=None,
    )
    build = build_engine()
    wheel = verify_installed_wheel(wheel_path, build)
    pairs = inputs["lock"]["pairlist"]["pairs"][:pair_count]
    timerange = _portable_timerange(inputs["lock"]["scope"]["timerange"])
    workload = {
        "mode_contract": inputs["contract"].contract_id,
        "strategy_sha256": inputs["public"]["strategy_sha256"],
        "config_sha256": inputs["lock"]["config"]["selected_sha256"],
        "data_aggregate_sha256": inputs["public"]["data_aggregate_sha256"],
        "market_snapshot_sha256": inputs["public"]["engine_market_snapshot_sha256"],
        "pairs": pairs,
        "timerange": timerange,
        "timeframes": inputs["lock"]["scope"]["timeframes"],
    }
    workload_sha = _document_sha256(workload)

    warmup = _measure_portable_run(
        inputs,
        output / "warmup",
        pairs=pairs,
        timerange=timerange,
        profile_path=Path(execution_profile_path).resolve(),
        timeout_seconds=timeout_seconds,
    )
    if not warmup["complete"]:
        raise BenchmarkError(
            "portable wheel warmup did not complete a cold strict run; inspect warmup/run.json"
        )
    runs: list[dict[str, Any]] = []
    target = repetitions
    while len(runs) < target:
        run = _measure_portable_run(
            inputs,
            output / "measurements" / f"run-{len(runs) + 1:02d}",
            pairs=pairs,
            timerange=timerange,
            profile_path=Path(execution_profile_path).resolve(),
            timeout_seconds=timeout_seconds,
        )
        runs.append(run)
        if (
            len(runs) == repetitions
            and repetitions < MAX_CERTIFICATION_REPETITIONS
            and _relative_spread(runs) > CERTIFICATION_SPREAD_THRESHOLD
        ):
            target = MAX_CERTIFICATION_REPETITIONS

    result_hashes = sorted(
        {
            value
            for value in [warmup["result_sha256"], *(run["result_sha256"] for run in runs)]
            if isinstance(value, str)
        }
    )
    deterministic = (
        warmup["result_sha256"] is not None
        and all(run["complete"] for run in runs)
        and len(result_hashes) == 1
    )
    wall = [float(run["wall_time_seconds"]) for run in runs]
    peaks = [int(run["peak_rss_bytes"]) for run in runs]
    system = platform.system().lower()
    report = {
        "schema_version": PLATFORM_BENCHMARK_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "complete": deterministic,
        "lane": RAW_INPUT_LANE,
        "platform": {
            "system": system,
            "machine": platform.machine().lower(),
            "python": platform.python_version(),
            "wsl": system == "linux" and "microsoft" in platform.release().lower(),
        },
        "hardware": public_hardware_record(inspect_hardware()),
        "package": {
            "version": __version__,
            "wheel_sha256": wheel["sha256"],
            "native_extension_sha256": wheel["native_member_sha256"],
            "installed_extension_equal": wheel["installed_extension_equal"],
        },
        "workload": {
            **workload,
            "identity_sha256": workload_sha,
        },
        "measurement": {
            "warmups_excluded": 1,
            "initial_repetitions": repetitions,
            "measured_repetitions": len(runs),
            "spread_threshold": CERTIFICATION_SPREAD_THRESHOLD,
            "relative_spread": _relative_spread(runs),
            "wall_time_seconds": {
                "minimum": min(wall),
                "median": statistics.median(wall),
                "maximum": max(wall),
            },
            "peak_rss_bytes": {
                "minimum": min(peaks),
                "maximum": max(peaks),
            },
            "result_sha256": result_hashes,
            "runs": runs,
        },
    }
    write_json(output / "platform-benchmark.json", report)
    bundle = write_evidence_bundle(
        output,
        evidence_id=f"{workload_sha}-{system}-{platform.machine().lower()}",
        release_certified=False,
        archive_name="platform-benchmark-bundle.zip",
        include_paths=[output / "platform-benchmark.json"],
    )
    result = {**report, "bundle": bundle}
    write_json(output / "platform-result.json", result)
    return result


def run_platform_fixture_benchmark(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    wheel_path: str | Path,
    repetitions: int = MIN_CERTIFICATION_REPETITIONS,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Measure one sealed exact-parity fixture using the installed candidate wheel.

    This lane proves that each release wheel executes the same portable Futures
    state transition stream.  It deliberately does not replace the representative
    80-pair, five-year performance certificate.
    """
    if repetitions < MIN_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"platform benchmark requires at least {MIN_CERTIFICATION_REPETITIONS} runs"
        )
    if repetitions > MAX_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"platform benchmark permits at most {MAX_CERTIFICATION_REPETITIONS} runs"
        )
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"platform benchmark output must be empty: {output}")

    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    config_reference = _fixture_input(manifest, role="config")
    strategy_reference = _fixture_input(manifest, role="strategy")
    config = read_json((manifest_file.parent / config_reference["path"]).resolve())
    if not isinstance(config, dict):
        raise SpecValidationError("fixture config must be an object")
    contract = release_contract_for_config(config)
    if manifest["freqtrade"]["trading_mode"] != contract.trading_mode:
        raise SpecValidationError(
            "fixture trading mode contradicts its release-mode config"
        )
    strategy_provenance = manifest.get("strategy_provenance")
    base_strategy_sha256 = strategy_reference["sha256"]
    if strategy_provenance is not None:
        if not isinstance(strategy_provenance, dict):
            raise SpecValidationError("fixture strategy provenance must be an object")
        declared_base = strategy_provenance.get("base_source_sha256")
        if not _is_sha256(declared_base):
            raise SpecValidationError(
                "fixture strategy provenance has no valid base source SHA"
            )
        base_strategy_sha256 = declared_base

    output.mkdir(parents=True, exist_ok=True)
    build = build_engine()
    wheel = verify_installed_wheel(wheel_path, build)
    workload = {
        "lane": EXACT_FIXTURE_LANE,
        "mode_contract": contract.contract_id,
        "fixture_id": manifest["fixture_id"],
        "manifest_sha256": sha256_file(manifest_file),
        "strategy_sha256": strategy_reference["sha256"],
        "base_strategy_sha256": base_strategy_sha256,
        "verification_level": "full",
    }
    workload_sha = _document_sha256(workload)

    warmup = _measure_fixture_run(
        manifest_file,
        output / "warmup",
        timeout_seconds=timeout_seconds,
    )
    if not warmup["complete"]:
        raise BenchmarkError(
            "platform fixture warmup did not complete exact full-state parity"
        )
    runs: list[dict[str, Any]] = []
    target = repetitions
    while len(runs) < target:
        run = _measure_fixture_run(
            manifest_file,
            output / "measurements" / f"run-{len(runs) + 1:02d}",
            timeout_seconds=timeout_seconds,
        )
        runs.append(run)
        if (
            len(runs) == repetitions
            and repetitions < MAX_CERTIFICATION_REPETITIONS
            and _relative_spread(runs) > CERTIFICATION_SPREAD_THRESHOLD
        ):
            target = MAX_CERTIFICATION_REPETITIONS

    result_hashes = sorted(
        {
            value
            for value in [warmup["result_sha256"], *(run["result_sha256"] for run in runs)]
            if isinstance(value, str)
        }
    )
    deterministic = (
        warmup["result_sha256"] is not None
        and all(run["complete"] for run in runs)
        and len(result_hashes) == 1
    )
    wall = [float(run["wall_time_seconds"]) for run in runs]
    peaks = [int(run["peak_rss_bytes"]) for run in runs]
    system = platform.system().lower()
    report = {
        "schema_version": PLATFORM_BENCHMARK_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "complete": deterministic,
        "lane": EXACT_FIXTURE_LANE,
        "platform": {
            "system": system,
            "machine": platform.machine().lower(),
            "python": platform.python_version(),
            "wsl": system == "linux" and "microsoft" in platform.release().lower(),
        },
        "hardware": public_hardware_record(inspect_hardware()),
        "package": {
            "version": __version__,
            "wheel_sha256": wheel["sha256"],
            "native_extension_sha256": wheel["native_member_sha256"],
            "installed_extension_equal": wheel["installed_extension_equal"],
            "installed_package_files": wheel["installed_package_files"],
            "installed_package_sha256": wheel["installed_package_sha256"],
            "portable_package_files": wheel["portable_package_files"],
            "portable_package_sha256": wheel["portable_package_sha256"],
            "engine_build_fingerprint": build["source_fingerprint"],
        },
        "workload": {
            **workload,
            "identity_sha256": workload_sha,
        },
        "measurement": {
            "warmups_excluded": 1,
            "initial_repetitions": repetitions,
            "measured_repetitions": len(runs),
            "spread_threshold": CERTIFICATION_SPREAD_THRESHOLD,
            "relative_spread": _relative_spread(runs),
            "wall_time_seconds": {
                "minimum": min(wall),
                "median": statistics.median(wall),
                "maximum": max(wall),
            },
            "peak_rss_bytes": {
                "minimum": min(peaks),
                "maximum": max(peaks),
            },
            "result_sha256": result_hashes,
            "runs": runs,
        },
    }
    write_json(output / "platform-benchmark.json", report)
    bundle = write_evidence_bundle(
        output,
        evidence_id=f"{workload_sha}-{system}-{platform.machine().lower()}",
        release_certified=False,
        archive_name="platform-benchmark-bundle.zip",
        include_paths=[output / "platform-benchmark.json"],
    )
    result = {**report, "bundle": bundle}
    write_json(output / "platform-result.json", result)
    return result


def seal_platform_evidence(
    report_paths: list[str | Path],
    output_directory: str | Path,
) -> dict[str, Any]:
    """Require Windows, Linux, and macOS reports with one deterministic result."""
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"platform evidence output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    reports = [read_json(path) for path in report_paths]
    if not reports:
        raise SpecValidationError("at least one platform report is required")
    for report in reports:
        _validate_platform_report(report)
    systems = {report["platform"]["system"] for report in reports}
    missing = sorted(REQUIRED_PLATFORM_SYSTEMS - systems)
    if missing:
        raise SpecValidationError(
            "platform evidence is missing systems: " + ", ".join(missing)
        )
    if len(systems) != len(reports):
        raise SpecValidationError("platform evidence must contain exactly one report per system")
    workload_hashes = {report["workload"]["identity_sha256"] for report in reports}
    mode_contracts = {report["workload"]["mode_contract"] for report in reports}
    lanes = {report["lane"] for report in reports}
    result_hashes = {
        hash_value
        for report in reports
        for hash_value in report["measurement"]["result_sha256"]
    }
    package_versions = {report["package"]["version"] for report in reports}
    portable_package_hashes = {
        report["package"].get("portable_package_sha256")
        for report in reports
        if report["lane"] == EXACT_FIXTURE_LANE
    }
    complete = (
        len(workload_hashes) == 1
        and len(mode_contracts) == 1
        and len(lanes) == 1
        and len(result_hashes) == 1
        and len(package_versions) == 1
        and (
            next(iter(lanes)) != EXACT_FIXTURE_LANE
            or len(portable_package_hashes) == 1
        )
        and all(report["complete"] for report in reports)
    )
    if not complete:
        raise SpecValidationError(
            "platform workload, result, package version, or completion verdict differs"
        )
    evidence = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release_certified": True,
        "lane": next(iter(lanes)),
        "mode_contract": next(iter(mode_contracts)),
        "workload_identity_sha256": next(iter(workload_hashes)),
        "workload": reports[0]["workload"],
        "result_sha256": next(iter(result_hashes)),
        "package_version": next(iter(package_versions)),
        "portable_package_sha256": (
            next(iter(portable_package_hashes))
            if next(iter(lanes)) == EXACT_FIXTURE_LANE
            else None
        ),
        "platforms": [
            {
                "system": report["platform"]["system"],
                "machine": report["platform"]["machine"],
                "wheel_sha256": report["package"]["wheel_sha256"],
                "native_extension_sha256": report["package"].get(
                    "native_extension_sha256"
                ),
                "wall_time_median_seconds": report["measurement"]["wall_time_seconds"][
                    "median"
                ],
                "peak_rss_bytes": report["measurement"]["peak_rss_bytes"]["maximum"],
                "measured_repetitions": report["measurement"]["measured_repetitions"],
                "report_sha256": sha256_file(path),
            }
            for path, report in sorted(
                zip(report_paths, reports, strict=True),
                key=lambda item: item[1]["platform"]["system"],
            )
        ],
    }
    write_json(output / "platform-evidence.json", evidence)
    bundle = write_evidence_bundle(
        output,
        evidence_id=evidence["workload_identity_sha256"],
        release_certified=True,
        archive_name="platform-evidence-bundle.zip",
        include_paths=[output / "platform-evidence.json"],
    )
    return {**evidence, "bundle": bundle}


def _measure_fixture_run(
    manifest_path: Path,
    output: Path,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    measured = measure_cli_process(
        [
            "engine",
            "fixture",
            str(manifest_path),
            "--output-dir",
            str(output),
            "--level",
            "full",
        ],
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    report = read_json(report_path) if report_path.is_file() else None
    execution = report.get("execution") if isinstance(report, dict) else None
    native_peak = execution.get("peak_rss_bytes") if isinstance(execution, dict) else None
    peak = int(measured["peak_rss_bytes"])
    if isinstance(native_peak, int):
        peak = max(peak, native_peak)
    surface_path = output / "research" / "trade-surface.json"
    surface = read_json(surface_path) if surface_path.is_file() else None
    result_sha = _fixture_result_sha256(report, surface)
    complete = bool(
        measured["exit_code"] == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and report.get("verification_level") == "full"
        and report.get("parity", {}).get("trade_surface", {}).get("equal") is True
        and report.get("parity", {}).get("state_trace", {}).get("checked") is True
        and report.get("parity", {}).get("state_trace", {}).get("equal") is True
        and report.get("branch_coverage", {}).get("met") is True
        and isinstance(result_sha, str)
    )
    return {
        "wall_time_seconds": measured["wall_time_seconds"],
        "peak_rss_bytes": peak,
        "exit_code": measured["exit_code"],
        "timed_out": measured["timed_out"],
        "complete": complete,
        "result_sha256": result_sha,
    }


def _measure_portable_run(
    inputs: dict[str, Any],
    output: Path,
    *,
    pairs: list[str],
    timerange: str,
    profile_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
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
        timerange,
        "--output-dir",
        str(output),
        "--recalibrate",
        "--cache-dir",
        str(output / "cold-vector-cache"),
        "--markets",
        str(inputs["engine_market_snapshot"]),
        "--no-market-download",
        "--registry",
        str(output / "runs.sqlite"),
        "--profile",
        str(profile_path),
        "--no-download",
        "--history-coverage",
        "strict",
    ]
    for pair in pairs:
        arguments.extend(["--pair", pair])
    measured = measure_cli_process(
        arguments,
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    report = read_json(report_path) if report_path.is_file() else None
    result = report.get("result") if isinstance(report, dict) else None
    surface = result.get("trade_surface") if isinstance(result, dict) else None
    result_sha = surface.get("sha256") if isinstance(surface, dict) else None
    native = result.get("execution") if isinstance(result, dict) else None
    native_peak = native.get("peak_rss_bytes") if isinstance(native, dict) else None
    peak = int(measured["peak_rss_bytes"])
    if isinstance(native_peak, int):
        peak = max(peak, native_peak)
    complete = bool(
        measured["exit_code"] == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and report.get("pipeline_evidence", {}).get("cold") is True
        and report.get("data", {}).get("history_coverage_policy") == "strict"
        and report.get("data", {}).get("coverage_shortfall_count") == 0
        and not report.get("capability", {}).get("blockers")
        and isinstance(result_sha, str)
    )
    return {
        "wall_time_seconds": measured["wall_time_seconds"],
        "peak_rss_bytes": peak,
        "exit_code": measured["exit_code"],
        "timed_out": measured["timed_out"],
        "complete": complete,
        "result_sha256": result_sha if isinstance(result_sha, str) else None,
    }


def _portable_timerange(full_timerange: str) -> str:
    _start_ms, end_ms = parse_timerange_milliseconds(full_timerange)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    try:
        start = end.replace(year=end.year - 1)
    except ValueError:
        start = end.replace(year=end.year - 1, day=28)
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def _relative_spread(runs: list[dict[str, Any]]) -> float:
    values = [float(run["wall_time_seconds"]) for run in runs]
    median = statistics.median(values)
    return (max(values) - min(values)) / median if median > 0 else 0.0


def _validate_platform_report(report: Any) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != (
        PLATFORM_BENCHMARK_VERSION
    ):
        raise SpecValidationError("unsupported platform benchmark report")
    system = report.get("platform", {}).get("system")
    if system not in REQUIRED_PLATFORM_SYSTEMS:
        raise SpecValidationError(f"unsupported platform evidence system: {system!r}")
    if report.get("package", {}).get("installed_extension_equal") is not True:
        raise SpecValidationError("platform report did not run its candidate wheel")
    machine = str(report.get("platform", {}).get("machine", "")).lower()
    if machine not in REQUIRED_PLATFORM_MACHINES[system]:
        raise SpecValidationError(
            f"{system} platform evidence has unsupported machine: {machine!r}"
        )
    mode_contract = report.get("workload", {}).get("mode_contract")
    if mode_contract not in {"binance-spot", "binance-usdtm-isolated"}:
        raise SpecValidationError("platform report has an unsupported mode contract")
    lane = report.get("lane")
    if lane == RAW_INPUT_LANE:
        pairs = report.get("workload", {}).get("pairs")
        if not isinstance(pairs, list) or len(pairs) != PORTABLE_PAIR_COUNT:
            raise SpecValidationError(
                f"sealed platform evidence must use exactly {PORTABLE_PAIR_COUNT} pairs"
            )
    elif lane == EXACT_FIXTURE_LANE:
        workload = report.get("workload", {})
        if (
            workload.get("lane") != EXACT_FIXTURE_LANE
            or workload.get("verification_level") != "full"
            or not isinstance(workload.get("fixture_id"), str)
            or not _is_sha256(workload.get("manifest_sha256"))
            or not _is_sha256(workload.get("strategy_sha256"))
            or not _is_sha256(workload.get("base_strategy_sha256"))
            or not _is_sha256(
                report.get("package", {}).get("portable_package_sha256")
            )
        ):
            raise SpecValidationError("exact-fixture platform report is incomplete")
    else:
        raise SpecValidationError(f"unsupported platform benchmark lane: {lane!r}")
    hashes = report.get("measurement", {}).get("result_sha256")
    if not isinstance(hashes, list) or len(hashes) != 1:
        raise SpecValidationError("platform report is not result-deterministic")


def _fixture_input(manifest: dict[str, Any], *, role: str) -> dict[str, Any]:
    matches = [item for item in manifest["inputs"] if item["role"] == role]
    if len(matches) != 1:
        raise SpecValidationError(f"fixture requires exactly one {role!r} input")
    return matches[0]


def _fixture_result_sha256(
    report: Any,
    trade_surface: Any,
) -> str | None:
    if not isinstance(report, dict) or not isinstance(trade_surface, dict):
        return None
    state = report.get("parity", {}).get("state_trace", {}).get("actual", {})
    identity = {
        "fixture_id": report.get("fixture_id"),
        "trade_surface_sha256": _document_sha256(trade_surface),
        "state_input_sha256": state.get("input_sha256"),
        "state_profile_sha256": state.get("profile_sha256"),
        "state_stream_hash": state.get("stream_hash"),
    }
    if (
        not isinstance(identity["fixture_id"], str)
        or not all(
            _is_sha256(identity[key])
            for key in (
                "trade_surface_sha256",
                "state_input_sha256",
                "state_profile_sha256",
                "state_stream_hash",
            )
        )
    ):
        return None
    return _document_sha256(identity)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _document_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
