"""Checkpointed public research-run orchestration."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import read_json, write_json
from .config_loader import (
    config_sha256,
    freeze_pairlist,
    load_effective_config,
    sanitize_config,
)
from .data_seal import DATA_SEAL_VERSION, prepare_data, validate_data_seal
from .engine_runtime import run_engine
from .errors import BenchmarkError, SpecValidationError
from .fixture import sha256_file
from .generic_adapter import (
    GENERIC_ADAPTER_VERSION,
    build_generic_vector_manifest,
    generic_adapter_blockers,
    generic_data_blockers,
    generic_result_to_surface,
)
from .hardware import (
    current_resource_limits,
    ensure_execution_profile,
    execution_environment,
    validate_execution_profile,
)
from .hot_ir import HOT_IR_VERSION, build_hot_callback_ir
from .market_snapshot import MARKET_SNAPSHOT_VERSION, capture_market_snapshot
from .reference_runtime import load_reference_leverage_tiers
from .run_registry import RunRegistry
from .strategy_ir import STRATEGY_IR_VERSION
from .vector_runtime import (
    VECTOR_PIPELINE_VERSION,
    load_strategy_analysis,
    prepare_vector_signals,
)
from .x7_adapter import (
    X7_ADAPTER_VERSION,
    build_x7_vector_manifest,
    x7_adapter_blockers,
)

RESEARCH_RUN_VERSION = "1.4.0"


def run_research_backtest(
    *,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    timerange: str,
    output_directory: str | Path,
    pairs: list[str] | None = None,
    workers: int | None = None,
    cache_directory: str | Path | None = None,
    profile_path: str | Path = ".nfi/execution-profile.json",
    resume: bool = False,
    prepare_only: bool = False,
    download_missing: bool = True,
    market_metadata_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    download_market_metadata: bool = True,
    execution_profile: dict[str, Any] | None = None,
    recalibrate: bool = False,
    history_coverage_policy: str = "strict",
    trace_engine_events: bool = False,
) -> dict[str, Any]:
    """Prepare an immutable X7 run and stop exactly at unsupported semantics."""
    pipeline_started_ns = time.perf_counter_ns()
    pipeline_started_at = _utc_now()
    stage_started_ns = pipeline_started_ns
    source = Path(strategy_path).resolve()
    config_file = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()) and not resume:
        raise BenchmarkError(
            f"research output directory must be empty: {output}; use --resume to continue it"
        )
    output.mkdir(parents=True, exist_ok=True)

    loaded = load_effective_config(config_file)
    effective_config = loaded["config"]
    pairlist = freeze_pairlist(effective_config, resolved_pairs=pairs)
    run_config = sanitize_config(effective_config)
    if not isinstance(run_config, dict):
        raise SpecValidationError("effective runtime config must be an object")
    run_exchange = run_config.get("exchange")
    if not isinstance(run_exchange, dict):
        raise SpecValidationError("effective runtime config exchange must be an object")
    run_exchange["pair_whitelist"] = pairlist["pairs"]
    analysis = load_strategy_analysis(
        source,
        class_name=class_name,
        cache_directory=cache_directory,
    )
    if not analysis["static_safe"]:
        first = next(item for item in analysis["diagnostics"] if item["severity"] == "error")
        location = first["location"]
        raise SpecValidationError(
            f"{location['path']}:{location['line']}:{location['column']}: "
            f"{first['code']}: {first['message']}"
        )
    hot_ir = build_hot_callback_ir(
        analysis,
        trading_mode=str(run_config.get("trading_mode", "spot")),
        run_mode="backtest",
        config=run_config,
    )
    if execution_profile is None:
        profile = ensure_execution_profile(profile_path, workspace=output)
    else:
        validate_execution_profile(execution_profile, current_hardware=None)
        profile = execution_profile
    resource_limits = current_resource_limits(profile)
    safe_workers = int(resource_limits["cpu_process_limit"])
    selected_workers = safe_workers if workers is None else workers
    if selected_workers <= 0:
        raise SpecValidationError("research worker count must be positive")
    if selected_workers > safe_workers:
        raise SpecValidationError(
            f"requested {selected_workers} workers exceeds the hardware profile limit "
            f"of {safe_workers}; recalibrate the profile instead of oversubscribing it"
        )
    selected_market_metadata = (
        Path(market_metadata_path).resolve() if market_metadata_path is not None else None
    )
    automatic_market_path = output / "market-metadata.json"
    if (
        not prepare_only
        and hot_ir["hot_loop_ready"]
        and selected_market_metadata is None
        and download_market_metadata
    ):
        if resume and automatic_market_path.is_file():
            selected_market_metadata = automatic_market_path
        else:
            tier_capture = None
            if run_config.get("trading_mode") == "futures":
                exchange_name = str(run_exchange.get("name", "")).lower()
                if exchange_name != "binance":
                    raise BenchmarkError(
                        "automatic futures leverage-tier capture currently requires "
                        "Binance; provide a sealed --markets snapshot for this exchange"
                    )
                tier_capture = load_reference_leverage_tiers(pairlist["pairs"])
            capture_market_snapshot(
                run_config,
                pairlist["pairs"],
                automatic_market_path,
                leverage_tiers=(
                    tier_capture["tiers"] if tier_capture is not None else None
                ),
                leverage_tier_source=(
                    tier_capture["source"] if tier_capture is not None else None
                ),
            )
            selected_market_metadata = automatic_market_path

    sealed_inputs = _seal_run_inputs(
        source=source,
        run_config=run_config,
        output=output,
        resume=resume,
    )
    identity = {
        "schema_version": RESEARCH_RUN_VERSION,
        "pipeline": {
            "package_version": __version__,
            "strategy_ir_version": STRATEGY_IR_VERSION,
            "hot_ir_version": HOT_IR_VERSION,
            "data_seal_version": DATA_SEAL_VERSION,
            "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            "market_snapshot_version": MARKET_SNAPSHOT_VERSION,
            "generic_adapter_version": GENERIC_ADAPTER_VERSION,
            "x7_adapter_version": X7_ADAPTER_VERSION,
        },
        "strategy": {
            "path": str(source),
            "class_name": class_name,
            "file_sha256": sha256_file(source),
            "analysis_sha256": analysis["source"]["sha256"],
            "capability_fingerprint": analysis["strategies"][0]["capability_fingerprint"],
            "sealed": sealed_inputs["strategy"],
        },
        "config": {
            "root_path": str(config_file),
            "source_effective_sha256": loaded["sha256"],
            "run_effective_sha256": config_sha256(run_config),
            "input_files": loaded["inputs"],
            "sealed": sealed_inputs["config"],
        },
        "pairlist_sha256": pairlist["sha256"],
        "data_directory": str(data_root),
        "timerange": timerange,
        "market_metadata": (
            {
                "path": str(selected_market_metadata),
                "sha256": sha256_file(selected_market_metadata),
            }
            if selected_market_metadata is not None and selected_market_metadata.is_file()
            else None
        ),
    }
    run_id = _identity_sha256(identity)
    identity_path = output / "identity.json"
    if resume and identity_path.is_file():
        existing = read_json(identity_path)
        if existing.get("run_id") != run_id or existing.get("identity") != identity:
            raise BenchmarkError("resume identity differs from the existing research run")
    elif resume and any(output.iterdir()):
        raise BenchmarkError("cannot resume output without a valid identity.json")

    write_json(identity_path, {"run_id": run_id, "identity": identity})
    write_json(output / "pairlist.json", pairlist)
    write_json(
        output / "effective-config.redacted.json",
        {
            "schema_version": loaded["schema_version"],
            "sha256": config_sha256(run_config),
            "config": run_config,
        },
    )
    write_json(output / "strategy-analysis.json", analysis)
    write_json(output / "hot-callback-ir.json", hot_ir)
    write_json(
        output / "execution-profile.json",
        {
            "source": str(Path(profile_path).resolve()),
            "hardware_fingerprint": profile["hardware_fingerprint"],
            "limits": profile["limits"],
            "runtime": profile["runtime"],
            "environment": profile["environment"],
        },
    )

    data_pairs = _required_data_pairs(pairlist, run_config)
    download_config = sanitize_config(run_config)
    if not isinstance(download_config, dict):
        raise SpecValidationError("download config must be an object")
    download_exchange = download_config.get("exchange")
    if not isinstance(download_exchange, dict):
        raise SpecValidationError("download config exchange must be an object")
    download_exchange["pair_whitelist"] = data_pairs
    download_config_path = output / "download-config.json"
    write_json(download_config_path, download_config)
    input_seconds = _elapsed_seconds(stage_started_ns)
    stage_started_ns = time.perf_counter_ns()
    data_seal_path = output / "data-seal.json"
    resumed_data_stage = False
    if resume and data_seal_path.is_file():
        data_seal = validate_data_seal(data_seal_path)
        resumed_data_stage = True
    else:
        raw_startup_candles = analysis["strategies"][0]["constants"].get(
            "startup_candle_count", 0
        )
        startup_candles = (
            raw_startup_candles
            if isinstance(raw_startup_candles, int)
            and not isinstance(raw_startup_candles, bool)
            else 0
        )
        data_seal = prepare_data(
            config_path=download_config_path,
            data_directory=data_root,
            timerange=timerange,
            timeframes=analysis["strategies"][0]["required_timeframes"],
            destination=data_seal_path,
            download_missing=download_missing,
            startup_candles=startup_candles,
            history_coverage_policy=history_coverage_policy,
        )
    data_seconds = _elapsed_seconds(stage_started_ns)
    stage_started_ns = time.perf_counter_ns()

    vector_directory = output / "vectors"
    vector_checkpoint = output / "checkpoints" / "vectors.json"
    vector_report = None
    resumed_vector_stage = False
    if resume and vector_checkpoint.is_file():
        candidate = read_json(vector_checkpoint)
        if _valid_vector_checkpoint(candidate, vector_directory):
            vector_report = candidate["report"]
            resumed_vector_stage = True
    if vector_report is None:
        _reset_owned_directory(vector_directory, root=output)
        with execution_environment(profile["environment"]):
            vector_report = prepare_vector_signals(
                strategy_path=source,
                class_name=class_name,
                config=run_config,
                pairs=pairlist["pairs"],
                data_directory=data_root,
                timerange=timerange,
                output_directory=vector_directory,
                workers=selected_workers,
                cache_directory=cache_directory,
                memory_cap_bytes=int(resource_limits["working_memory_bytes"]),
                hardware_fingerprint=profile["hardware_fingerprint"],
                calibration_directory=Path(profile_path).resolve().parent / "calibrations",
                recalibrate=recalibrate,
            )
        write_json(
            vector_checkpoint,
            {
                "schema_version": "1.0.0",
                "completed_at": _utc_now(),
                "report": vector_report,
            },
        )
    vector_seconds = _elapsed_seconds(stage_started_ns)
    stage_started_ns = time.perf_counter_ns()

    blockers = list(hot_ir["blockers"])
    has_strategy_callbacks = bool(
        analysis["strategies"][0].get(
            "strategy_callbacks",
            analysis["strategies"][0].get("hot_callbacks", []),
        )
    )
    if not blockers and not prepare_only:
        if has_strategy_callbacks:
            blockers.extend(
                x7_adapter_blockers(
                    analysis,
                    hot_ir,
                    run_config,
                    market_metadata_path=selected_market_metadata,
                )
            )
        else:
            blockers.extend(
                generic_adapter_blockers(
                    analysis,
                    run_config,
                    market_metadata_path=selected_market_metadata,
                )
            )
    if not blockers and not prepare_only and not has_strategy_callbacks:
        blockers.extend(generic_data_blockers(analysis, vector_report))
    capability_seconds = _elapsed_seconds(stage_started_ns)
    manifest_seconds = 0.0
    engine_seconds = 0.0
    surface_seconds = 0.0
    result_record = None
    if not blockers and not prepare_only:
        assert selected_market_metadata is not None
        simulation_input_path = output / "simulation-input.manifest.json"
        simulation_result_path = output / "simulation-result.json"
        engine_profile_path = output / "engine-profile.json"
        engine_events_path = (
            output / "engine-events.jsonl" if trace_engine_events else None
        )
        surface_path = output / "trade-surface.json"
        stage_started_ns = time.perf_counter_ns()
        if has_strategy_callbacks:
            build_x7_vector_manifest(
                analysis=analysis,
                hot_ir=hot_ir,
                config=run_config,
                vector_report=vector_report,
                market_metadata_path=selected_market_metadata,
                destination=simulation_input_path,
            )
        else:
            build_generic_vector_manifest(
                analysis=analysis,
                config=run_config,
                vector_report=vector_report,
                market_metadata_path=selected_market_metadata,
                destination=simulation_input_path,
            )
        manifest_seconds = _elapsed_seconds(stage_started_ns)
        stage_started_ns = time.perf_counter_ns()
        execution = run_engine(
            simulation_input_path,
            simulation_result_path,
            profile_path=profile_path,
            vector_manifest=True,
            engine_profile_path=engine_profile_path,
            events_path=engine_events_path,
        )
        engine_seconds = _elapsed_seconds(stage_started_ns)
        stage_started_ns = time.perf_counter_ns()
        strategy = analysis["strategies"][0]
        surface = generic_result_to_surface(
            result_path=simulation_result_path,
            strategy_name=class_name,
            config=run_config,
            timeframe=strategy["constants"]["timeframe"],
            timerange=timerange,
            stoploss_ratio=float(strategy["constants"]["stoploss"]),
            destination=surface_path,
        )
        surface_seconds = _elapsed_seconds(stage_started_ns)
        result_record = {
            "trade_count": len(surface["trades"]),
            "execution": execution,
            "simulation_input": _artifact_record(simulation_input_path),
            "simulation_result": _artifact_record(simulation_result_path),
            "trade_surface": _artifact_record(surface_path),
            "engine_events": (
                _artifact_record(engine_events_path)
                if engine_events_path is not None
                else None
            ),
            "summary": surface["summary"],
        }
    status = (
        "prepared" if prepare_only else "blocked_unsupported_semantics" if blockers else "complete"
    )
    vector_cache_hits = int(vector_report.get("cache_hits", 0))
    cold_pipeline = (
        not resumed_data_stage
        and not resumed_vector_stage
        and vector_cache_hits == 0
    )
    report = {
        "schema_version": RESEARCH_RUN_VERSION,
        "run_id": run_id,
        "status": status,
        "complete": status == "complete",
        "prepared_only": prepare_only,
        "pipeline_evidence": {
            "cold": cold_pipeline,
            "data_checkpoint_reused": resumed_data_stage,
            "vector_checkpoint_reused": resumed_vector_stage,
            "vector_cache_hits": vector_cache_hits,
            "definition": "no resumed data/vector checkpoint and zero vector cache hits",
        },
        "resumed_stages": [
            stage
            for stage, resumed in (
                ("data", resumed_data_stage),
                ("vectors", resumed_vector_stage),
            )
            if resumed
        ],
        "created_at": _utc_now(),
        "timings": {
            "started_at": pipeline_started_at,
            "pipeline_wall_time_seconds": _elapsed_seconds(pipeline_started_ns),
            "stages": {
                "input_preparation_seconds": input_seconds,
                "data_seconds": data_seconds,
                "vectors_seconds": vector_seconds,
                "capability_seconds": capability_seconds,
                "manifest_seconds": manifest_seconds,
                "engine_seconds": engine_seconds,
                "surface_seconds": surface_seconds,
            },
        },
        "inputs": identity,
        "execution": {
            "hardware_fingerprint": profile["hardware_fingerprint"],
            "indicator_workers": vector_report["worker_count"],
            "cpu_process_limit": safe_workers,
            "working_memory_bytes": resource_limits["working_memory_bytes"],
            "workload_calibration": vector_report.get("calibration"),
            "portfolio_simulator_threads": profile["runtime"][
                "portfolio_simulator_threads"
            ],
            "python_per_candle": False,
        },
        "data": {
            "aggregate_sha256": data_seal["aggregate_sha256"],
            "file_count": len(data_seal["files"]),
            "download_count": len(data_seal["downloads"]),
            "history_coverage_policy": data_seal["request"].get(
                "history_coverage_policy",
                "strict",
            ),
            "coverage_shortfall_count": len(
                data_seal.get("coverage_shortfalls", [])
            ),
        },
        "vectors": vector_report,
        "capability": {
            "strategy_static_safe": analysis["static_safe"],
            "hot_ir_fingerprint": hot_ir["fingerprint"],
            "hot_loop_ready": hot_ir["hot_loop_ready"],
            "blockers": blockers,
        },
        "result": result_record,
        "official_confirmation": {
            "required_for_finalist": True,
            "status": "not_run",
        },
    }
    write_json(output / "run.json", report)
    if registry_path is not None:
        with RunRegistry(registry_path) as registry:
            registry.record(report, output)
    return report


def _valid_vector_checkpoint(checkpoint: Any, vector_directory: Path) -> bool:
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != "1.0.0":
        return False
    report = checkpoint.get("report")
    if (
        not isinstance(report, dict)
        or report.get("pipeline_version") != VECTOR_PIPELINE_VERSION
        or not isinstance(report.get("outputs"), list)
    ):
        return False
    for artifact in report["outputs"]:
        if not isinstance(artifact, dict):
            return False
        path_value = artifact.get("path")
        expected_hash = artifact.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected_hash, str):
            return False
        path = Path(path_value).resolve()
        if not path.is_relative_to(vector_directory.resolve()) or not path.is_file():
            return False
        if sha256_file(path) != expected_hash:
            return False
    return len(report["outputs"]) == report.get("pair_count")


def _seal_run_inputs(
    *,
    source: Path,
    run_config: dict[str, Any],
    output: Path,
    resume: bool,
) -> dict[str, dict[str, Any]]:
    """Keep finalist inputs valid after the upstream strategy file changes."""
    sealed_directory = output / "sealed-inputs"
    sealed_directory.mkdir(parents=True, exist_ok=True)
    strategy_path = sealed_directory / "strategy.py"
    config_path = sealed_directory / "config.json"
    source_hash = sha256_file(source)
    if strategy_path.exists():
        if not resume or sha256_file(strategy_path) != source_hash:
            raise BenchmarkError("sealed strategy input differs from the requested source")
    else:
        shutil.copyfile(source, strategy_path)
    if config_path.exists():
        if not resume or read_json(config_path) != run_config:
            raise BenchmarkError("sealed config input differs from the effective config")
    else:
        write_json(config_path, run_config)
    return {
        "strategy": _relative_artifact_record(strategy_path, root=output),
        "config": _relative_artifact_record(config_path, root=output),
    }


def _reset_owned_directory(path: Path, *, root: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise BenchmarkError(f"refusing to reset unowned path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _identity_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _elapsed_seconds(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000_000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _relative_artifact_record(path: Path, *, root: Path) -> dict[str, Any]:
    record = _artifact_record(path)
    record["path"] = path.relative_to(root).as_posix()
    return record


def _required_data_pairs(
    pairlist: dict[str, Any],
    run_config: dict[str, Any],
) -> list[str]:
    pairs = list(pairlist["pairs"])
    stake = str(run_config.get("stake_currency", "USDT"))
    futures = run_config.get("trading_mode") in {"futures", "margin"}
    btc_pair = f"BTC/{stake}:{stake}" if futures else f"BTC/{stake}"
    if btc_pair not in pairs:
        pairs.append(btc_pair)
    return pairs
