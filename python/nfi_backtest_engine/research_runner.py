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
from .errors import BenchmarkError, SpecValidationError, StrategyAnalysisError
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
from .result_report import write_result_presentation
from .run_registry import RunRegistry
from .specs import validate_trade_surface
from .state_machine_ir import (
    STATE_MACHINE_PROGRAM_VERSION,
    compile_state_machine_program,
)
from .strategy_ir import STRATEGY_IR_VERSION
from .strategy_overrides import effective_stoploss_ratio
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

RESEARCH_RUN_VERSION = "1.5.0"
LEGACY_RESEARCH_RUN_VERSION = "1.4.0"
SIMULATION_CHECKPOINT_VERSION = "1.0.0"


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
    output_had_files = output.exists() and any(output.iterdir())
    if output_had_files and not resume:
        raise BenchmarkError(
            f"research output directory must be empty: {output}; use --resume to continue it"
        )
    output.mkdir(parents=True, exist_ok=True)
    resuming_existing = resume and output_had_files
    identity_path = output / "identity.json"
    existing_identity_document: dict[str, Any] | None = None
    if resuming_existing:
        if not identity_path.is_file():
            raise BenchmarkError("cannot resume output without a valid identity.json")
        candidate_identity = _read_json_object(identity_path, label="resume identity")
        existing_identity_document = candidate_identity

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
    state_machine_program: dict[str, Any] | None = None
    if not hot_ir["hot_loop_ready"]:
        try:
            candidate_program = compile_state_machine_program(
                source,
                class_name=class_name,
            )
        except StrategyAnalysisError:
            pass
        else:
            active_callbacks = {
                callback["name"]
                for callback in hot_ir["callbacks"]
                if callback["active_for_run"]
            }
            compiled_callbacks = set(candidate_program["entrypoints"])
            if active_callbacks and compiled_callbacks == active_callbacks:
                state_machine_program = candidate_program
    state_machine_ready = state_machine_program is not None
    native_callbacks_ready = hot_ir["hot_loop_ready"] or state_machine_ready
    adapter_lane = (
        "generic-state-machine"
        if state_machine_ready and not hot_ir["hot_loop_ready"]
        else "x7-legacy"
        if hot_ir["hot_loop_ready"]
        and bool(
            analysis["strategies"][0].get(
                "strategy_callbacks",
                analysis["strategies"][0].get("hot_callbacks", []),
            )
        )
        else "generic-signal"
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
        native_callbacks_ready
        and selected_market_metadata is None
        and download_market_metadata
    ):
        if resuming_existing:
            if not automatic_market_path.is_file():
                raise BenchmarkError(
                    "resume identity requires market-metadata.json, but the file is missing"
                )
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
        resume=resuming_existing,
    )
    identity = {
        "schema_version": RESEARCH_RUN_VERSION,
        "pipeline": {
            "package_version": __version__,
            "strategy_ir_version": STRATEGY_IR_VERSION,
            "hot_ir_version": HOT_IR_VERSION,
            "data_seal_version": DATA_SEAL_VERSION,
            "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            "simulation_checkpoint_version": SIMULATION_CHECKPOINT_VERSION,
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
        "data_policy": {
            "history_coverage_policy": history_coverage_policy,
        },
        "output_options": {
            "trace_engine_events": trace_engine_events,
        },
        "market_metadata": (
            {
                "path": str(selected_market_metadata),
                "sha256": sha256_file(selected_market_metadata),
            }
            if selected_market_metadata is not None and selected_market_metadata.is_file()
            else None
        ),
        **(
            {
                "state_machine": {
                    "schema_version": STATE_MACHINE_PROGRAM_VERSION,
                    "adapter_lane": adapter_lane,
                    "program": state_machine_program,
                }
            }
            if state_machine_program is not None
            else {}
        ),
    }
    run_id = _identity_sha256(identity)
    if existing_identity_document is not None:
        if (
            existing_identity_document.get("run_id") != run_id
            or existing_identity_document.get("identity") != identity
        ):
            legacy_identity = _legacy_resume_identity(identity)
            if legacy_identity is None:
                raise BenchmarkError(
                    "resume identity differs from the existing research run"
                )
            legacy_run_id = _identity_sha256(legacy_identity)
            if (
                existing_identity_document.get("run_id") != legacy_run_id
                or existing_identity_document.get("identity") != legacy_identity
            ):
                raise BenchmarkError(
                    "resume identity differs from the existing research run"
                )
            legacy_run = _load_existing_run(
                output,
                run_id=legacy_run_id,
                identity=legacy_identity,
            )
            if legacy_run is None or legacy_run["complete"] is not True:
                raise BenchmarkError(
                    "incomplete legacy run has no ordered simulation checkpoints; "
                    "refusing to modify its evidence"
                )
            _validate_completed_run_artifacts(
                legacy_run,
                output,
                require_simulation_checkpoint=False,
            )
            return legacy_run
        existing_run = _load_existing_run(output, run_id=run_id, identity=identity)
        if existing_run is not None and existing_run["complete"] is True:
            _validate_completed_run_artifacts(existing_run, output)
            return existing_run
    else:
        existing_run = None
        write_json(identity_path, {"run_id": run_id, "identity": identity})
    _write_or_validate_stage_json(
        output / "pairlist.json",
        pairlist,
        resuming=resuming_existing,
        label="pairlist",
    )
    _write_or_validate_stage_json(
        output / "effective-config.redacted.json",
        {
            "schema_version": loaded["schema_version"],
            "sha256": config_sha256(run_config),
            "config": run_config,
        },
        resuming=resuming_existing,
        label="effective config",
    )
    _write_or_validate_stage_json(
        output / "strategy-analysis.json",
        analysis,
        resuming=resuming_existing,
        label="strategy analysis",
    )
    _write_or_validate_stage_json(
        output / "hot-callback-ir.json",
        hot_ir,
        resuming=resuming_existing,
        label="hot callback IR",
    )
    if state_machine_program is not None:
        _write_or_validate_stage_json(
            output / "state-machine-ir.json",
            state_machine_program,
            resuming=resuming_existing,
            label="state machine IR",
        )
    _write_or_validate_stage_json(
        output / "execution-profile.json",
        {
            "source": str(Path(profile_path).resolve()),
            "hardware_fingerprint": profile["hardware_fingerprint"],
            "limits": profile["limits"],
            "runtime": profile["runtime"],
            "environment": profile["environment"],
        },
        resuming=resuming_existing,
        label="execution profile",
    )

    data_pairs = required_data_pairs(pairlist, run_config)
    download_config = sanitize_config(run_config)
    if not isinstance(download_config, dict):
        raise SpecValidationError("download config must be an object")
    download_exchange = download_config.get("exchange")
    if not isinstance(download_exchange, dict):
        raise SpecValidationError("download config exchange must be an object")
    download_exchange["pair_whitelist"] = data_pairs
    download_config_path = output / "download-config.json"
    _write_or_validate_stage_json(
        download_config_path,
        download_config,
        resuming=resuming_existing,
        label="download config",
    )
    input_seconds = _elapsed_seconds(stage_started_ns)
    stage_started_ns = time.perf_counter_ns()
    data_seal_path = output / "data-seal.json"
    resumed_data_stage = False
    if existing_run is not None and not data_seal_path.is_file():
        raise BenchmarkError(
            "resume run report claims completed data, but data-seal.json is missing"
        )
    if resuming_existing and data_seal_path.is_file():
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
    if existing_run is not None and not vector_checkpoint.is_file():
        raise BenchmarkError(
            "resume run report claims completed vectors, but their checkpoint is missing"
        )
    if resuming_existing and vector_checkpoint.is_file():
        candidate = _read_json_object(vector_checkpoint, label="vector checkpoint")
        if not _valid_vector_checkpoint(candidate, vector_directory):
            raise BenchmarkError("vector checkpoint or its artifacts failed validation")
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
    if (
        existing_run is not None
        and existing_run["prepared_only"] is True
        and prepare_only
    ):
        return existing_run

    blockers = [] if state_machine_ready else list(hot_ir["blockers"])
    if not blockers and not prepare_only:
        if adapter_lane == "x7-legacy":
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
                    state_machine_program=state_machine_program,
                )
            )
    if not blockers and not prepare_only and adapter_lane != "x7-legacy":
        blockers.extend(generic_data_blockers(analysis, vector_report))
    capability_seconds = _elapsed_seconds(stage_started_ns)
    manifest_seconds = 0.0
    engine_seconds = 0.0
    surface_seconds = 0.0
    resumed_manifest_stage = False
    resumed_engine_stage = False
    resumed_surface_stage = False
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
        simulation_checkpoint_path = output / "checkpoints" / "simulation.json"
        if resuming_existing and simulation_checkpoint_path.is_file():
            simulation_checkpoint = _load_simulation_checkpoint(
                simulation_checkpoint_path,
                output=output,
                run_id=run_id,
                simulation_input_path=simulation_input_path,
                simulation_result_path=simulation_result_path,
                surface_path=surface_path,
                engine_profile_path=engine_profile_path,
                engine_events_path=engine_events_path,
            )
        else:
            if resuming_existing:
                _reject_uncheckpointed_simulation_artifacts(
                    simulation_input_path,
                    simulation_result_path,
                    surface_path,
                    engine_profile_path,
                    engine_events_path,
                )
            simulation_checkpoint = {
                "schema_version": SIMULATION_CHECKPOINT_VERSION,
                "run_id": run_id,
                "stages": {},
            }
        simulation_stages = simulation_checkpoint["stages"]

        manifest_stage = simulation_stages.get("manifest")
        if isinstance(manifest_stage, dict):
            manifest_record = manifest_stage["artifact"]
            resumed_manifest_stage = True
        else:
            _require_absent(simulation_input_path, label="simulation input")
            stage_started_ns = time.perf_counter_ns()
            if adapter_lane == "x7-legacy":
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
                    state_machine_program=state_machine_program,
                )
            manifest_seconds = _elapsed_seconds(stage_started_ns)
            manifest_record = _artifact_record(simulation_input_path)
            simulation_stages["manifest"] = {"artifact": manifest_record}
            _write_simulation_checkpoint(
                simulation_checkpoint_path,
                simulation_checkpoint,
            )

        engine_stage = simulation_stages.get("engine")
        if isinstance(engine_stage, dict):
            execution = engine_stage["execution"]
            simulation_result_record = engine_stage["artifact"]
            engine_events_record = engine_stage.get("engine_events")
            resumed_engine_stage = True
        else:
            _require_absent(simulation_result_path, label="simulation result")
            _require_absent(engine_profile_path, label="engine profile")
            if engine_events_path is not None:
                _require_absent(engine_events_path, label="engine events")
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
            simulation_result_record = _artifact_record(simulation_result_path)
            engine_events_record = (
                _artifact_record(engine_events_path)
                if engine_events_path is not None
                else None
            )
            simulation_stages["engine"] = {
                "input_sha256": manifest_record["sha256"],
                "artifact": simulation_result_record,
                "execution": execution,
                "engine_profile": _artifact_record(engine_profile_path),
                "engine_events": engine_events_record,
            }
            _write_simulation_checkpoint(
                simulation_checkpoint_path,
                simulation_checkpoint,
            )

        surface_stage = simulation_stages.get("surface")
        if isinstance(surface_stage, dict):
            surface_record = surface_stage["artifact"]
            surface = _read_json_object(surface_path, label="trade surface")
            validate_trade_surface(surface)
            resumed_surface_stage = True
        else:
            _require_absent(surface_path, label="trade surface")
            stage_started_ns = time.perf_counter_ns()
            strategy = analysis["strategies"][0]
            surface = generic_result_to_surface(
                result_path=simulation_result_path,
                strategy_name=class_name,
                config=run_config,
                timeframe=strategy["constants"]["timeframe"],
                timerange=timerange,
                stoploss_ratio=effective_stoploss_ratio(
                    strategy["constants"],
                    run_config,
                ),
                destination=surface_path,
            )
            surface_seconds = _elapsed_seconds(stage_started_ns)
            surface_record = _artifact_record(surface_path)
            simulation_stages["surface"] = {
                "simulation_result_sha256": simulation_result_record["sha256"],
                "artifact": surface_record,
                "trade_count": len(surface["trades"]),
                "summary": surface["summary"],
            }
            _write_simulation_checkpoint(
                simulation_checkpoint_path,
                simulation_checkpoint,
            )
        result_record = {
            "trade_count": len(surface["trades"]),
            "execution": execution,
            "simulation_input": manifest_record,
            "simulation_result": simulation_result_record,
            "trade_surface": surface_record,
            "engine_events": engine_events_record,
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
            "manifest_checkpoint_reused": resumed_manifest_stage,
            "engine_checkpoint_reused": resumed_engine_stage,
            "surface_checkpoint_reused": resumed_surface_stage,
            "vector_cache_hits": vector_cache_hits,
            "definition": "no resumed data/vector checkpoint and zero vector cache hits",
        },
        "resumed_stages": [
            stage
            for stage, resumed in (
                ("data", resumed_data_stage),
                ("vectors", resumed_vector_stage),
                ("simulation_input", resumed_manifest_stage),
                ("simulation_result", resumed_engine_stage),
                ("trade_surface", resumed_surface_stage),
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
            "hot_loop_ready": native_callbacks_ready,
            **(
                {
                    "legacy_hot_loop_ready": hot_ir["hot_loop_ready"],
                    "adapter_lane": adapter_lane,
                    "state_machine_schema_version": STATE_MACHINE_PROGRAM_VERSION,
                }
                if state_machine_program is not None
                else {}
            ),
            "blockers": blockers,
        },
        "result": result_record,
        "official_confirmation": {
            "required_for_finalist": True,
            "status": "not_run",
        },
    }
    write_json(output / "run.json", report)
    write_result_presentation(output)
    if registry_path is not None:
        with RunRegistry(registry_path) as registry:
            registry.record(report, output)
    return report


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{label} must be a JSON object: {path}")
    return value


def _write_or_validate_stage_json(
    path: Path,
    value: dict[str, Any],
    *,
    resuming: bool,
    label: str,
) -> None:
    if resuming and path.exists():
        if _read_json_object(path, label=label) != value:
            raise BenchmarkError(f"{label} differs from the resumed run")
        return
    write_json(path, value)


def _load_existing_run(
    output: Path,
    *,
    run_id: str,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    path = output / "run.json"
    if not path.is_file():
        return None
    report = _read_json_object(path, label="resume run report")
    if report.get("schema_version") != identity.get("schema_version"):
        raise BenchmarkError("resume run report uses a different schema version")
    if report.get("run_id") != run_id or report.get("inputs") != identity:
        raise BenchmarkError("resume run report contradicts identity.json")
    complete = report.get("complete")
    prepared_only = report.get("prepared_only")
    status = report.get("status")
    if not isinstance(complete, bool) or not isinstance(prepared_only, bool):
        raise BenchmarkError("resume run report has invalid completion flags")
    if complete:
        if status != "complete" or prepared_only or not isinstance(report.get("result"), dict):
            raise BenchmarkError("resume run report has contradictory completed state")
        return report
    if report.get("result") is not None:
        raise BenchmarkError("incomplete resume run unexpectedly contains result artifacts")
    if prepared_only and status != "prepared":
        raise BenchmarkError("prepare-only resume run has contradictory status")
    if not prepared_only and status not in {"blocked_unsupported_semantics"}:
        raise BenchmarkError("incomplete resume run has an unsupported state")
    return report


def _validate_completed_run_artifacts(
    report: dict[str, Any],
    output: Path,
    *,
    require_simulation_checkpoint: bool = True,
) -> None:
    result = report["result"]
    required = (
        ("simulation_input", "simulation input", output / "simulation-input.manifest.json"),
        ("simulation_result", "simulation result", output / "simulation-result.json"),
        ("trade_surface", "trade surface", output / "trade-surface.json"),
    )
    for key, label, expected_path in required:
        record = result.get(key)
        if not isinstance(record, dict):
            raise BenchmarkError(f"completed run has no {label} artifact record")
        _validate_artifact_record(record, expected_path=expected_path, label=label)

    surface = _read_json_object(output / "trade-surface.json", label="trade surface")
    validate_trade_surface(surface)
    trades = surface.get("trades")
    if not isinstance(trades, list) or result.get("trade_count") != len(trades):
        raise BenchmarkError("completed run trade count contradicts the trade surface")
    if result.get("summary") != surface.get("summary"):
        raise BenchmarkError("completed run summary contradicts the trade surface")

    checkpoint_path = output / "checkpoints" / "simulation.json"
    if not checkpoint_path.is_file():
        if require_simulation_checkpoint:
            raise BenchmarkError("completed run is missing its simulation checkpoint")
        return
    output_options = report["inputs"].get("output_options")
    trace_enabled = (
        bool(output_options.get("trace_engine_events"))
        if isinstance(output_options, dict)
        else False
    )
    checkpoint = _load_simulation_checkpoint(
        checkpoint_path,
        output=output,
        run_id=report["run_id"],
        simulation_input_path=output / "simulation-input.manifest.json",
        simulation_result_path=output / "simulation-result.json",
        surface_path=output / "trade-surface.json",
        engine_profile_path=output / "engine-profile.json",
        engine_events_path=(output / "engine-events.jsonl" if trace_enabled else None),
    )
    stages = checkpoint["stages"]
    manifest = stages.get("manifest")
    engine = stages.get("engine")
    surface_stage = stages.get("surface")
    if (
        not isinstance(manifest, dict)
        or not isinstance(engine, dict)
        or not isinstance(surface_stage, dict)
        or manifest.get("artifact") != result["simulation_input"]
        or engine.get("artifact") != result["simulation_result"]
        or surface_stage.get("artifact") != result["trade_surface"]
        or engine.get("execution") != result.get("execution")
        or engine.get("engine_events") != result.get("engine_events")
    ):
        raise BenchmarkError("completed run contradicts its simulation checkpoint")


def _validate_artifact_record(
    record: dict[str, Any],
    *,
    expected_path: Path,
    label: str,
) -> dict[str, Any]:
    raw_path = record.get("path")
    expected_bytes = record.get("bytes")
    expected_sha256 = record.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
        or not isinstance(expected_sha256, str)
    ):
        raise BenchmarkError(f"{label} artifact record is malformed")
    recorded_path = Path(raw_path)
    if not recorded_path.is_absolute():
        recorded_path = expected_path.parent / recorded_path
    if recorded_path.resolve() != expected_path.resolve():
        raise BenchmarkError(f"{label} artifact path differs from the owned output path")
    if not expected_path.is_file():
        raise BenchmarkError(f"{label} artifact is missing: {expected_path}")
    if expected_path.stat().st_size != expected_bytes:
        raise BenchmarkError(f"{label} artifact bytes differ from its checkpoint")
    if sha256_file(expected_path) != expected_sha256:
        raise BenchmarkError(f"{label} artifact SHA-256 differs from its checkpoint")
    return record


def _load_simulation_checkpoint(
    path: Path,
    *,
    output: Path,
    run_id: str,
    simulation_input_path: Path,
    simulation_result_path: Path,
    surface_path: Path,
    engine_profile_path: Path,
    engine_events_path: Path | None,
) -> dict[str, Any]:
    checkpoint = _read_json_object(path, label="simulation checkpoint")
    if (
        checkpoint.get("schema_version") != SIMULATION_CHECKPOINT_VERSION
        or checkpoint.get("run_id") != run_id
        or not isinstance(checkpoint.get("stages"), dict)
    ):
        raise BenchmarkError("simulation checkpoint identity or schema is invalid")
    stages = checkpoint["stages"]
    manifest = stages.get("manifest")
    engine = stages.get("engine")
    surface = stages.get("surface")

    if manifest is None:
        if engine is not None or surface is not None:
            raise BenchmarkError("simulation checkpoint skips the manifest stage")
        return checkpoint
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifact"), dict):
        raise BenchmarkError("simulation manifest checkpoint is malformed")
    manifest_record = _validate_artifact_record(
        manifest["artifact"],
        expected_path=simulation_input_path,
        label="simulation input",
    )

    if engine is None:
        if surface is not None:
            raise BenchmarkError("simulation checkpoint skips the engine stage")
        _require_absent(simulation_result_path, label="uncheckpointed simulation result")
        _require_absent(engine_profile_path, label="uncheckpointed engine profile")
        if engine_events_path is not None:
            _require_absent(engine_events_path, label="uncheckpointed engine events")
        return checkpoint
    if (
        not isinstance(engine, dict)
        or engine.get("input_sha256") != manifest_record["sha256"]
        or not isinstance(engine.get("artifact"), dict)
        or not isinstance(engine.get("execution"), dict)
        or not isinstance(engine.get("engine_profile"), dict)
    ):
        raise BenchmarkError("simulation engine checkpoint is malformed or unbound")
    result_record = _validate_artifact_record(
        engine["artifact"],
        expected_path=simulation_result_path,
        label="simulation result",
    )
    _validate_artifact_record(
        engine["engine_profile"],
        expected_path=engine_profile_path,
        label="engine profile",
    )
    events_record = engine.get("engine_events")
    recorded_events_path = output / "engine-events.jsonl"
    if events_record is None:
        _require_absent(recorded_events_path, label="uncheckpointed engine events")
    elif isinstance(events_record, dict):
        _validate_artifact_record(
            events_record,
            expected_path=recorded_events_path,
            label="engine events",
        )
    else:
        raise BenchmarkError("simulation engine-events checkpoint is malformed")

    if surface is None:
        _require_absent(surface_path, label="uncheckpointed trade surface")
        return checkpoint
    if (
        not isinstance(surface, dict)
        or surface.get("simulation_result_sha256") != result_record["sha256"]
        or not isinstance(surface.get("artifact"), dict)
    ):
        raise BenchmarkError("trade-surface checkpoint is malformed or unbound")
    _validate_artifact_record(
        surface["artifact"],
        expected_path=surface_path,
        label="trade surface",
    )
    surface_document = _read_json_object(surface_path, label="trade surface")
    validate_trade_surface(surface_document)
    if (
        surface.get("trade_count") != len(surface_document["trades"])
        or surface.get("summary") != surface_document["summary"]
    ):
        raise BenchmarkError("trade-surface checkpoint contradicts its artifact")
    return checkpoint


def _write_simulation_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    write_json(path, checkpoint)


def _reject_uncheckpointed_simulation_artifacts(*paths: Path | None) -> None:
    existing = [path for path in paths if path is not None and path.exists()]
    if existing:
        rendered = ", ".join(path.name for path in existing)
        raise BenchmarkError(
            "resume found simulation artifacts without a valid checkpoint: "
            f"{rendered}; refusing to delete or overwrite them"
        )


def _require_absent(path: Path, *, label: str) -> None:
    if path.exists():
        raise BenchmarkError(f"{label} already exists; refusing to overwrite it: {path}")


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
        if resume:
            raise BenchmarkError("sealed strategy input is missing from the resumed run")
        shutil.copyfile(source, strategy_path)
    if config_path.exists():
        if not resume or read_json(config_path) != run_config:
            raise BenchmarkError("sealed config input differs from the effective config")
    else:
        if resume:
            raise BenchmarkError("sealed config input is missing from the resumed run")
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


def _legacy_resume_identity(identity: dict[str, Any]) -> dict[str, Any] | None:
    """Project default v1.5 inputs onto the immutable v1.4 identity contract."""
    if identity.get("data_policy") != {"history_coverage_policy": "strict"}:
        return None
    if identity.get("output_options") != {"trace_engine_events": False}:
        return None
    pipeline = identity.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    legacy_pipeline = dict(pipeline)
    legacy_pipeline.pop("simulation_checkpoint_version", None)
    legacy_identity = dict(identity)
    legacy_identity["schema_version"] = LEGACY_RESEARCH_RUN_VERSION
    legacy_identity["pipeline"] = legacy_pipeline
    legacy_identity.pop("data_policy", None)
    legacy_identity.pop("output_options", None)
    return legacy_identity


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


def required_data_pairs(
    pairlist: dict[str, Any],
    run_config: dict[str, Any],
) -> list[str]:
    """Return the current engine-required data universe for a traded pair list."""
    pairs = list(pairlist["pairs"])
    stake = str(run_config.get("stake_currency", "USDT"))
    futures = run_config.get("trading_mode") in {"futures", "margin"}
    btc_pair = f"BTC/{stake}:{stake}" if futures else f"BTC/{stake}"
    if btc_pair not in pairs:
        pairs.append(btc_pair)
    return pairs
