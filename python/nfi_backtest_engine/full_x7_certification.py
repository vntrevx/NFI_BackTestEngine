"""Release-grade Full X7 certification over the real research pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .branch_coverage import validate_fixture_coverage
from .cache import cache_key
from .canonical import write_json
from .certification_parts import measurements as _certification_measurements
from .certification_parts import probes as _certification_probes
from .certification_parts.inputs import (
    _validate_full_x7_timeframes,
    _validate_release_data_seal,
    validate_full_x7_inputs,
    verify_installed_wheel,
)
from .certification_parts.measurements import (
    _determinism,
    _engine_complete,
    _engine_reuse_complete,
    _engine_surface_sha,
    _public_run_record,
    _reference_complete,
    _reference_surface_sha,
    _relative_spread,
    _require_complete_baseline,
    _run_summary,
    _seal_preserved_vector_cache,
)
from .certification_parts.probes import _run_probes
from .engine_runtime import build_engine
from .errors import BenchmarkError
from .evidence_bundle import (
    public_engine_build_record,
    public_hardware_record,
    write_evidence_bundle,
)
from .fixture import sha256_file, validate_fixture
from .full_x7_resume import (
    import_reference_oracle,
    load_engine_measurement,
    load_reference_measurement,
    require_stage_available,
    validate_reference_oracle,
)
from .hardware import (
    current_resource_limits,
    inspect_hardware,
    load_execution_profile,
)
from .performance_gate import measure_cli_process
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_CERTIFICATION_REPETITIONS,
    TARGET_SCREENING_SPEEDUP,
)
from .release_inputs import (
    release_history_coverage_policy,
)
from .specs import FULL_X7_CERTIFICATION_V2_SCHEMA, validate_schema
from .vector_runtime import VECTOR_PIPELINE_VERSION

FULL_X7_CERTIFICATION_VERSION = "2.0.0"

__all__ = [
    "FULL_X7_CERTIFICATION_VERSION",
    "VECTOR_PIPELINE_VERSION",
    "_determinism",
    "_engine_complete",
    "_measure_engine",
    "_measure_reference",
    "_validate_full_x7_timeframes",
    "_validate_probe_matrix",
    "_validate_release_data_seal",
    "cache_key",
    "run_full_x7_certification",
    "validate_full_x7_inputs",
    "verify_installed_wheel",
]


def _measure_engine(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point retaining the patchable process measurer."""
    _certification_measurements.measure_cli_process = (  # pyright: ignore[reportPrivateImportUsage]
        measure_cli_process
    )
    return _certification_measurements._measure_engine(*args, **kwargs)


def _measure_reference(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point retaining patchable reference measurement."""
    _certification_measurements.measure_cli_process = (  # pyright: ignore[reportPrivateImportUsage]
        measure_cli_process
    )
    _certification_measurements._reference_surface_sha = (  # pyright: ignore[reportPrivateImportUsage]
        _reference_surface_sha
    )
    return _certification_measurements._measure_reference(*args, **kwargs)


def _validate_probe_matrix(
    *args: Any,
    **kwargs: Any,
) -> list[tuple[Path, dict[str, Any]]]:
    """Compatibility entry point retaining patchable fixture validation."""
    _certification_probes.validate_fixture = (  # pyright: ignore[reportPrivateImportUsage]
        validate_fixture
    )
    _certification_probes.validate_fixture_coverage = (  # pyright: ignore[reportPrivateImportUsage]
        validate_fixture_coverage
    )
    return _certification_probes._validate_probe_matrix(*args, **kwargs)


def run_full_x7_certification(
    release_lock_path: str | Path,
    output_directory: str | Path,
    *,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    reference_market_snapshot: str | Path | None,
    wheel_path: str | Path,
    execution_profile_path: str | Path,
    state_probe_manifests: list[str | Path],
    repetitions: int = MIN_CERTIFICATION_REPETITIONS,
    timeout_seconds: int,
    swap_cap_bytes: int | None = None,
    official_oracle_directory: str | Path | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Certify one cold seed, one official oracle, and repeated vector reuse.

    The cold seed proves the complete strategy-to-vector pipeline and publishes
    a content-addressed cache. Candidate timing repeats start fresh processes
    and simulations while reusing only those hash-verified vectors. Repeating
    the memory-heavy five-year Oracle adds no accuracy evidence, so it runs once;
    small full-state probes still execute both lanes once.
    """
    if repetitions < MIN_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"Full X7 certification requires at least {MIN_CERTIFICATION_REPETITIONS} runs"
        )
    if repetitions > MAX_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"Full X7 certification permits at most {MAX_CERTIFICATION_REPETITIONS} runs"
        )
    if swap_cap_bytes is None or swap_cap_bytes <= 0:
        raise BenchmarkError("Full X7 certification requires a positive sealed swap cap")
    output = Path(output_directory).resolve()
    if output.exists() and any(output.iterdir()) and not resume:
        raise BenchmarkError(f"Full X7 output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    inputs = validate_full_x7_inputs(
        release_lock_path=release_lock_path,
        strategy_path=strategy_path,
        class_name=class_name,
        config_path=config_path,
        data_directory=data_directory,
        engine_market_snapshot=engine_market_snapshot,
        reference_market_snapshot=reference_market_snapshot,
    )
    build = build_engine()
    wheel = verify_installed_wheel(wheel_path, build)
    profile = load_execution_profile(execution_profile_path)
    probes = _validate_probe_matrix(
        state_probe_manifests,
        contract=inputs["contract"],
        expected_upstream_commit=inputs["lock"]["strategy"]["upstream_commit"],
    )

    warmup_root = output / "warmups"
    baseline = (
        load_engine_measurement(
            warmup_root / "engine",
            validator=lambda measurement: _engine_complete(
                measurement,
                inputs["lock"],
            ),
            allow_report_fallback=True,
            allow_incomplete=True,
        )
        if resume
        else None
    )
    if baseline is None:
        if not resume:
            require_stage_available(warmup_root / "engine", stage="native warmup")
        baseline = _measure_engine(
            inputs,
            warmup_root / "engine",
            profile_path=Path(execution_profile_path).resolve(),
            timeout_seconds=timeout_seconds,
            resume=resume,
        )
    _require_complete_baseline(baseline, inputs["lock"])
    reference_output = warmup_root / "reference"
    reference_warmup = load_reference_measurement(reference_output) if resume else None
    if reference_warmup is not None:
        if _reference_complete(reference_warmup):
            validate_reference_oracle(
                reference_warmup,
                baseline=baseline,
                inputs=inputs,
                validator=_reference_complete,
            )
        else:
            # A copy can be interrupted after all immutable oracle bytes land
            # but before local parity metadata is reconciled. The importer
            # below resumes only a byte-identical copy and rejects every other
            # non-empty state.
            reference_warmup = None
    if reference_warmup is None:
        if official_oracle_directory is not None:
            if not resume:
                require_stage_available(reference_output, stage="official oracle")
            reference_warmup = import_reference_oracle(
                official_oracle_directory,
                reference_output,
                baseline=baseline,
                inputs=inputs,
                validator=_reference_complete,
            )
        else:
            require_stage_available(reference_output, stage="official oracle")
            reference_warmup = _measure_reference(
                baseline["output_directory"],
                inputs["reference_market_snapshot"],
                reference_output,
                timeout_seconds=timeout_seconds,
                swap_cap_bytes=swap_cap_bytes,
            )
    reference_markets = inputs["reference_market_snapshot"]
    if reference_markets is None:
        captured = Path(reference_warmup["output_directory"]) / "reference-markets.json"
        if not captured.is_file():
            raise BenchmarkError(
                "official Full X7 warmup did not produce a reference market snapshot"
            )
        reference_markets = captured.resolve()
        inputs["reference_market_snapshot"] = reference_markets
        inputs["public"]["reference_market_snapshot_sha256"] = sha256_file(reference_markets)
    if not _reference_complete(reference_warmup):
        raise BenchmarkError(
            "official Full X7 warmup did not complete exact parity; "
            "inspect warmups/reference/run.json"
        )

    preserved_cache = _seal_preserved_vector_cache(
        baseline,
        pairs=inputs["lock"]["pairlist"]["pairs"],
        destination=output / "preserved-vector-cache.json",
    )
    engine_runs: list[dict[str, Any]] = []
    target_repetitions = repetitions
    while len(engine_runs) < target_repetitions:
        run_number = len(engine_runs) + 1
        run_output = output / "measurements" / f"engine-{run_number:02d}"
        measured = (
            load_engine_measurement(
                run_output,
                validator=lambda measurement: _engine_reuse_complete(
                    measurement,
                    inputs["lock"],
                ),
                allow_report_fallback=False,
                allow_incomplete=True,
            )
            if resume
            else None
        )
        if measured is None:
            if not resume:
                require_stage_available(
                    run_output,
                    stage=f"native measurement {run_number}",
                )
            measured = _measure_engine(
                inputs,
                run_output,
                profile_path=Path(execution_profile_path).resolve(),
                timeout_seconds=timeout_seconds,
                resume=resume,
                vector_cache=preserved_cache["root"],
                recalibrate=False,
            )
        engine_runs.append(measured)
        if (
            len(engine_runs) == repetitions
            and repetitions < MAX_CERTIFICATION_REPETITIONS
            and _relative_spread(engine_runs) > CERTIFICATION_SPREAD_THRESHOLD
        ):
            target_repetitions = MAX_CERTIFICATION_REPETITIONS

    probe_reports = _run_probes(
        probes,
        output / "state-probes",
        execution_profile_path=execution_profile_path,
        timeout_seconds=timeout_seconds,
        resume=resume,
    )
    engine_summary = _run_summary(engine_runs, lane="engine")
    cold_summary = _run_summary([baseline], lane="engine")
    reference_summary = _run_summary([reference_warmup], lane="reference")
    speedup = (
        reference_summary["wall_time_seconds"]["median"]
        / engine_summary["wall_time_seconds"]["median"]
    )
    baseline_hash = _engine_surface_sha(baseline)
    determinism = _determinism(
        baseline_hash,
        engine_runs,
        [reference_warmup],
    )
    cold_complete = _engine_complete(baseline, inputs["lock"])
    engine_complete = all(_engine_reuse_complete(run, inputs["lock"]) for run in engine_runs)
    reference_complete = _reference_complete(reference_warmup)
    profile_memory = current_resource_limits(profile)["working_memory_bytes"]
    observed_peak = max(
        cold_summary["peak_rss_bytes"]["maximum"],
        engine_summary["peak_rss_bytes"]["maximum"],
    )
    memory_met = observed_peak <= profile_memory
    swap_gate = _swap_gate(
        cold_summary,
        engine_summary,
        reference_summary,
        limit_bytes=swap_cap_bytes,
    )
    probe_met = all(
        item["complete"]
        and item["trade_surface_equal"]
        and item["full_state_equal"]
        and item["coverage_met"]
        for item in probe_reports
    )
    gates = {
        "input_lock": {"met": True, "identity_sha256": inputs["lock"]["identity_sha256"]},
        "installed_wheel": {
            "met": wheel["installed_extension_equal"],
            **{key: value for key, value in wheel.items() if key != "path"},
        },
        "native_pipeline": {
            "met": cold_complete and engine_complete,
            "cold_seed_met": cold_complete,
            "preserved_vector_reuse_met": engine_complete,
            "rule": (
                "one cold seed and every measured preserved-vector reuse run are complete, "
                f"{release_history_coverage_policy(inputs['lock'])}, "
                "callback-blocker free, and fully content-addressed"
            ),
        },
        "official_parity": {
            "met": reference_complete,
            "rule": "one continuous official Freqtrade oracle completes exact surface parity",
        },
        "determinism": determinism,
        "speed": {
            "met": speedup >= TARGET_SCREENING_SPEEDUP,
            "target_speedup": TARGET_SCREENING_SPEEDUP,
            "observed_speedup": speedup,
            "lane": "preserved-vector-reuse",
            "cold_seed_speedup": (
                reference_summary["wall_time_seconds"]["median"]
                / cold_summary["wall_time_seconds"]["median"]
            ),
        },
        "memory": {
            "met": memory_met,
            "limit_bytes": profile_memory,
            "observed_peak_bytes": observed_peak,
            "cold_seed_peak_bytes": cold_summary["peak_rss_bytes"]["maximum"],
            "reuse_peak_bytes": engine_summary["peak_rss_bytes"]["maximum"],
        },
        "swap": swap_gate,
        "state_probes": {
            "met": probe_met,
            "required_kinds": sorted(inputs["contract"].required_probe_kinds),
            "required_protection_methods": sorted(inputs["contract"].required_protection_methods),
            "completed": sum(1 for item in probe_reports if item["complete"]),
        },
    }
    release_certified = all(bool(gate["met"]) for gate in gates.values())
    contract = inputs["contract"]
    report = {
        "schema_version": FULL_X7_CERTIFICATION_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "certified" if release_certified else "failed",
        "release_certified": release_certified,
        "claim_scope": {
            "strategy": class_name,
            "upstream_commit": inputs["lock"]["strategy"]["upstream_commit"],
            **contract.scope_fields(),
            "timerange": inputs["lock"]["scope"]["timerange"],
            "pair_count": inputs["lock"]["scope"]["pair_count"],
            "timeframes": inputs["lock"]["scope"]["timeframes"],
            "continuous_timerange": True,
            "history_coverage_policy": release_history_coverage_policy(inputs["lock"]),
            "evidence": "continuous-oracle-plus-official-full-state-probes",
        },
        "inputs": inputs["public"],
        "environment": {
            "hardware": public_hardware_record(inspect_hardware()),
            "execution_profile": {
                "hardware_fingerprint": profile["hardware_fingerprint"],
                "working_memory_bytes": profile_memory,
                "swap_cap_bytes": swap_cap_bytes,
            },
            "package_version": __version__,
            "engine_build": public_engine_build_record(build),
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_lane": "preserved-vector-reuse",
            "native_initial_repetitions": repetitions,
            "native_measured_repetitions": len(engine_runs),
            "native_maximum_repetitions": MAX_CERTIFICATION_REPETITIONS,
            "native_spread_threshold": CERTIFICATION_SPREAD_THRESHOLD,
            "engine_relative_spread": _relative_spread(engine_runs),
            "cold_seed_repetitions": 1,
            "preserved_vector_cache": preserved_cache["record"],
            "official_reference_repetitions": 1,
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": resume,
        },
        "runs": {
            "engine": [_public_run_record(run, root=output) for run in engine_runs],
            "cold_seed": _public_run_record(baseline, root=output),
            "official_reference": _public_run_record(reference_warmup, root=output),
            "engine_summary": engine_summary,
            "cold_seed_summary": cold_summary,
            "official_reference_summary": reference_summary,
        },
        "state_probes": probe_reports,
        "gates": gates,
    }
    report_path = output / "full-x7-certification.json"
    validate_schema(report, FULL_X7_CERTIFICATION_V2_SCHEMA)
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        output,
        evidence_id=inputs["lock"]["identity_sha256"],
        release_certified=release_certified,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path, preserved_cache["manifest"]],
    )
    result = {**report, "bundle": bundle}
    write_json(output / "full-x7-result.json", result)
    return result


def _swap_gate(
    cold_summary: dict[str, Any],
    engine_summary: dict[str, Any],
    reference_summary: dict[str, Any],
    *,
    limit_bytes: int,
) -> dict[str, Any]:
    native_complete = bool(
        cold_summary["peak_swap_bytes"]["measurements_complete"]
        and engine_summary["peak_swap_bytes"]["measurements_complete"]
    )
    official_complete = bool(
        reference_summary["peak_swap_bytes"]["measurements_complete"]
    )
    native_peak = max(
        int(cold_summary["peak_swap_bytes"]["maximum"] or 0),
        int(engine_summary["peak_swap_bytes"]["maximum"] or 0),
    )
    official_peak = int(reference_summary["peak_swap_bytes"]["maximum"] or 0)
    return {
        "met": (
            native_complete
            and official_complete
            and native_peak <= limit_bytes
            and official_peak <= limit_bytes
        ),
        "limit_bytes": limit_bytes,
        "native_measurement_complete": native_complete,
        "official_measurement_complete": official_complete,
        "native_observed_peak_bytes": native_peak,
        "official_observed_peak_bytes": official_peak,
        "rule": (
            "Native process-tree and Official cgroup swap peaks must not exceed "
            "the sealed cap"
        ),
    }
