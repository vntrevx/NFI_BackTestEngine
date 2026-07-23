"""Release-grade Full X7 certification over the real research pipeline."""

from __future__ import annotations

import hashlib
import statistics
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .branch_coverage import validate_fixture_coverage
from .canonical import read_json, write_json
from .config_loader import config_sha256, load_effective_config
from .data_seal import validate_data_seal
from .engine_runtime import build_engine
from .errors import BenchmarkError, SpecValidationError
from .evidence_bundle import (
    artifact_record,
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
    write_measurement_checkpoint,
)
from .hardware import (
    current_resource_limits,
    inspect_hardware,
    load_execution_profile,
)
from .performance_gate import measure_cli_process, run_performance_gate
from .product_contract import (
    CERTIFICATION_SPREAD_THRESHOLD,
    FULL_X7_RELEASE_TIMEFRAMES,
    MAX_CERTIFICATION_REPETITIONS,
    MIN_CERTIFICATION_REPETITIONS,
    MIN_RELEASE_BACKTEST_DAYS,
    MIN_RELEASE_PAIR_COUNT,
    TARGET_SCREENING_SPEEDUP,
)
from .release_inputs import validate_release_input_lock
from .research_reference import official_backtest_config
from .specs import FULL_X7_CERTIFICATION_SCHEMA, validate_schema
from .timerange import parse_timerange_milliseconds

FULL_X7_CERTIFICATION_VERSION = "1.1.0"
REQUIRED_PROBE_KINDS = frozenset(
    {
        "tag-121",
        "protections-locks",
        "liquidation",
        "compound-tags",
        "variable-leverage",
    }
)
REQUIRED_PROTECTION_METHODS = frozenset(
    {
        "CooldownPeriod",
        "StoplossGuard",
        "MaxDrawdown",
        "LowProfitPairs",
    }
)


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
    """Run one continuous official oracle and repeat only the native candidate.

    The official five-year run proves exactness. Repeating that memory-heavy
    oracle does not improve the native timing distribution, so release timing
    repeats apply only to fresh candidate-wheel executions. Small full-state
    probes still execute both lanes once.
    """
    if repetitions < MIN_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"Full X7 certification requires at least {MIN_CERTIFICATION_REPETITIONS} runs"
        )
    if repetitions > MAX_CERTIFICATION_REPETITIONS:
        raise BenchmarkError(
            f"Full X7 certification permits at most {MAX_CERTIFICATION_REPETITIONS} runs"
        )
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
        )
        if resume
        else None
    )
    if baseline is None:
        require_stage_available(warmup_root / "engine", stage="native warmup")
        baseline = _measure_engine(
            inputs,
            warmup_root / "engine",
            profile_path=Path(execution_profile_path).resolve(),
            timeout_seconds=timeout_seconds,
        )
    _require_complete_baseline(baseline, inputs["lock"])
    reference_warmup = load_reference_measurement(warmup_root / "reference") if resume else None
    if reference_warmup is None:
        require_stage_available(warmup_root / "reference", stage="official oracle")
        if official_oracle_directory is not None:
            reference_warmup = import_reference_oracle(
                official_oracle_directory,
                warmup_root / "reference",
                baseline=baseline,
                inputs=inputs,
                validator=_reference_complete,
            )
        else:
            reference_warmup = _measure_reference(
                baseline["output_directory"],
                inputs["reference_market_snapshot"],
                warmup_root / "reference",
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

    engine_runs: list[dict[str, Any]] = []
    target_repetitions = repetitions
    while len(engine_runs) < target_repetitions:
        run_number = len(engine_runs) + 1
        run_output = output / "measurements" / f"engine-{run_number:02d}"
        measured = (
            load_engine_measurement(
                run_output,
                validator=lambda measurement: _engine_complete(
                    measurement,
                    inputs["lock"],
                ),
                allow_report_fallback=False,
            )
            if resume
            else None
        )
        if measured is None:
            require_stage_available(
                run_output,
                stage=f"native measurement {run_number}",
            )
            measured = _measure_engine(
                inputs,
                run_output,
                profile_path=Path(execution_profile_path).resolve(),
                timeout_seconds=timeout_seconds,
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
    engine_complete = all(_engine_complete(run, inputs["lock"]) for run in engine_runs)
    reference_complete = _reference_complete(reference_warmup)
    profile_memory = current_resource_limits(profile)["working_memory_bytes"]
    memory_met = engine_summary["peak_rss_bytes"]["maximum"] <= profile_memory
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
            "met": engine_complete,
            "rule": "every measured run is cold, complete, strict, and callback-blocker free",
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
        },
        "memory": {
            "met": memory_met,
            "limit_bytes": profile_memory,
            "observed_peak_bytes": engine_summary["peak_rss_bytes"]["maximum"],
        },
        "state_probes": {
            "met": probe_met,
            "required_kinds": sorted(REQUIRED_PROBE_KINDS),
            "completed": sum(1 for item in probe_reports if item["complete"]),
        },
    }
    release_certified = all(bool(gate["met"]) for gate in gates.values())
    report = {
        "schema_version": FULL_X7_CERTIFICATION_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "certified" if release_certified else "failed",
        "release_certified": release_certified,
        "claim_scope": {
            "strategy": class_name,
            "upstream_commit": inputs["lock"]["strategy"]["upstream_commit"],
            "trading_mode": "spot",
            "timerange": inputs["lock"]["scope"]["timerange"],
            "pair_count": inputs["lock"]["scope"]["pair_count"],
            "timeframes": inputs["lock"]["scope"]["timeframes"],
            "continuous_timerange": True,
            "futures_evidence": "official-full-state-probes",
        },
        "inputs": inputs["public"],
        "environment": {
            "hardware": public_hardware_record(inspect_hardware()),
            "execution_profile": {
                "hardware_fingerprint": profile["hardware_fingerprint"],
                "working_memory_bytes": profile_memory,
            },
            "package_version": __version__,
            "engine_build": public_engine_build_record(build),
        },
        "measurement": {
            "native_warmups_excluded": 1,
            "native_initial_repetitions": repetitions,
            "native_measured_repetitions": len(engine_runs),
            "native_maximum_repetitions": MAX_CERTIFICATION_REPETITIONS,
            "native_spread_threshold": CERTIFICATION_SPREAD_THRESHOLD,
            "engine_relative_spread": _relative_spread(engine_runs),
            "official_reference_repetitions": 1,
            "official_reference_role": "single-continuous-exact-parity-oracle",
            "resumed": resume,
        },
        "runs": {
            "engine": [_public_run_record(run, root=output) for run in engine_runs],
            "official_reference": _public_run_record(reference_warmup, root=output),
            "engine_summary": engine_summary,
            "official_reference_summary": reference_summary,
        },
        "state_probes": probe_reports,
        "gates": gates,
    }
    report_path = output / "full-x7-certification.json"
    validate_schema(report, FULL_X7_CERTIFICATION_SCHEMA)
    write_json(report_path, report)
    bundle = write_evidence_bundle(
        output,
        evidence_id=inputs["lock"]["identity_sha256"],
        release_certified=release_certified,
        archive_name="full-x7-certification-bundle.zip",
        include_paths=[report_path],
    )
    result = {**report, "bundle": bundle}
    write_json(output / "full-x7-result.json", result)
    return result


def verify_installed_wheel(
    wheel_path: str | Path,
    build: dict[str, Any],
) -> dict[str, Any]:
    """Bind the imported native extension to the exact candidate wheel bytes."""
    wheel = Path(wheel_path).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise BenchmarkError(f"release wheel does not exist: {wheel}")
    if build.get("kind") != "pyo3-extension":
        raise BenchmarkError("Full X7 certification must run an installed native wheel")
    suffixes = (".pyd", ".so", ".dylib")
    with zipfile.ZipFile(wheel) as archive:
        candidates = sorted(
            name
            for name in archive.namelist()
            if name.startswith("nfi_backtest_engine/_rust") and name.endswith(suffixes)
        )
        if len(candidates) != 1:
            raise BenchmarkError(
                f"release wheel must contain exactly one native extension; found {len(candidates)}"
            )
        member_sha = hashlib.sha256(archive.read(candidates[0])).hexdigest()
    installed_sha = build.get("binary_sha256")
    equal = member_sha == installed_sha
    if not equal:
        raise BenchmarkError("imported native extension does not match the candidate wheel")
    return {
        "path": str(wheel),
        "sha256": sha256_file(wheel),
        "bytes": wheel.stat().st_size,
        "native_member": candidates[0],
        "native_member_sha256": member_sha,
        "installed_extension_sha256": installed_sha,
        "installed_extension_equal": equal,
    }


def validate_full_x7_inputs(
    *,
    release_lock_path: str | Path,
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    reference_market_snapshot: str | Path | None,
) -> dict[str, Any]:
    lock_path = Path(release_lock_path).resolve()
    lock = read_json(lock_path)
    validate_release_input_lock(lock, required_pair_count=MIN_RELEASE_PAIR_COUNT)
    _validate_full_x7_timeframes(lock["scope"]["timeframes"])
    return _resolve_full_x7_inputs(
        lock_path=lock_path,
        lock=lock,
        strategy_path=strategy_path,
        class_name=class_name,
        config_path=config_path,
        data_directory=data_directory,
        engine_market_snapshot=engine_market_snapshot,
        reference_market_snapshot=reference_market_snapshot,
    )


def _validate_full_x7_timeframes(timeframes: Any) -> None:
    actual_timeframes = tuple(timeframes) if isinstance(timeframes, list) else ()
    if actual_timeframes != FULL_X7_RELEASE_TIMEFRAMES:
        raise SpecValidationError(
            "Full X7 release timeframes differ from the certified contract: "
            f"expected {list(FULL_X7_RELEASE_TIMEFRAMES)!r}, "
            f"got {list(actual_timeframes)!r}"
        )


def _resolve_full_x7_inputs(
    *,
    lock_path: Path,
    lock: dict[str, Any],
    strategy_path: str | Path,
    class_name: str,
    config_path: str | Path,
    data_directory: str | Path,
    engine_market_snapshot: str | Path,
    reference_market_snapshot: str | Path | None,
) -> dict[str, Any]:
    source = Path(strategy_path).resolve()
    config = Path(config_path).resolve()
    data_root = Path(data_directory).resolve()
    engine_markets = Path(engine_market_snapshot).resolve()
    reference_markets = (
        Path(reference_market_snapshot).resolve() if reference_market_snapshot is not None else None
    )
    required_files = [
        (source, "strategy"),
        (config, "config"),
        (engine_markets, "engine market snapshot"),
    ]
    if reference_markets is not None:
        required_files.append((reference_markets, "reference market snapshot"))
    for path, label in required_files:
        if not path.is_file():
            raise BenchmarkError(f"Full X7 {label} does not exist: {path}")
    if not data_root.is_dir():
        raise BenchmarkError(f"Full X7 data directory does not exist: {data_root}")
    if class_name != lock["strategy"]["class_name"]:
        raise SpecValidationError("strategy class differs from the release input lock")
    if sha256_file(source) != lock["strategy"]["source_sha256"]:
        raise SpecValidationError("strategy source differs from the release input lock")
    loaded = load_effective_config(config)
    if config_sha256(loaded["config"]) != lock["config"]["selected_sha256"]:
        raise SpecValidationError("selected config differs from the release input lock")
    seal_path = lock_path.parent / "data-seal.json"
    seal = validate_data_seal(seal_path)
    _validate_release_data_seal(
        lock,
        seal,
        data_directory=data_root,
    )
    start_ms, end_ms = parse_timerange_milliseconds(lock["scope"]["timerange"])
    actual_days = (end_ms - start_ms) // 86_400_000
    if actual_days < MIN_RELEASE_BACKTEST_DAYS:
        raise SpecValidationError(
            f"Full X7 timerange has {actual_days} days; {MIN_RELEASE_BACKTEST_DAYS} required"
        )
    return {
        "lock": lock,
        "strategy_path": source,
        "config_path": config,
        "data_directory": data_root,
        "engine_market_snapshot": engine_markets,
        "reference_market_snapshot": reference_markets,
        "public": {
            "release_lock": {
                "sha256": sha256_file(lock_path),
                "identity_sha256": lock["identity_sha256"],
            },
            "strategy_sha256": sha256_file(source),
            "config_sha256": loaded["sha256"],
            "official_reference_config_sha256": config_sha256(
                official_backtest_config(loaded["config"])
            ),
            "data_aggregate_sha256": seal["aggregate_sha256"],
            "engine_market_snapshot_sha256": sha256_file(engine_markets),
            "reference_market_snapshot_sha256": (
                sha256_file(reference_markets) if reference_markets is not None else None
            ),
        },
    }


def _validate_release_data_seal(
    lock: dict[str, Any],
    seal: dict[str, Any],
    *,
    data_directory: Path,
) -> None:
    """Bind the machine-local data seal to every portable lock invariant."""
    request = seal["request"]
    data = lock["data"]
    scope = lock["scope"]
    if Path(seal["data_root"]).resolve() != data_directory:
        raise SpecValidationError("selected data directory differs from the release data seal")
    if (
        seal["aggregate_sha256"] != data["aggregate_sha256"]
        or len(seal["files"]) != data["file_count"]
        or len(seal["coverage_shortfalls"]) != data["coverage_shortfall_count"]
        or len(seal["startup_shortfalls"]) != data["startup_shortfall_count"]
    ):
        raise SpecValidationError("data seal differs from the release input lock")
    if (
        request["pairs"] != lock["pairlist"]["pairs"]
        or request["timerange"] != scope["timerange"]
        or request["timeframes"] != scope["timeframes"]
        or request["history_coverage_policy"] != "strict"
        or request["startup_coverage_policy"] != data["startup_coverage_policy"]
    ):
        raise SpecValidationError("data seal request differs from the release input lock")


def _validate_probe_matrix(
    manifests: list[str | Path],
    *,
    expected_upstream_commit: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    probes: list[tuple[Path, dict[str, Any]]] = []
    kinds: set[str] = set()
    protection_methods: set[str] = set()
    for value in manifests:
        path = Path(value).resolve()
        manifest = validate_fixture(path)
        if manifest["schema_version"] != "3.0.0":
            raise SpecValidationError("Full X7 probes must use fixture manifest v3")
        provenance = manifest.get("strategy_provenance")
        if expected_upstream_commit is not None and (
            not isinstance(provenance, dict)
            or provenance.get("upstream_commit") != expected_upstream_commit
        ):
            raise SpecValidationError(
                "Full X7 probe upstream commit differs from the release input lock"
            )
        validate_fixture_coverage(path, manifest)
        kinds.add(manifest["probe_kind"])
        protection_methods.update(manifest["required_coverage"]["protection_methods"])
        probes.append((path, manifest))
    missing = sorted(REQUIRED_PROBE_KINDS - kinds)
    if missing:
        raise SpecValidationError("Full X7 probe matrix is incomplete: " + ", ".join(missing))
    missing_protections = sorted(REQUIRED_PROTECTION_METHODS - protection_methods)
    if missing_protections:
        raise SpecValidationError(
            "Full X7 protection probe matrix is incomplete: " + ", ".join(missing_protections)
        )
    return probes


def _measure_engine(
    inputs: dict[str, Any],
    output: Path,
    *,
    profile_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    pairs = inputs["lock"]["pairlist"]["pairs"]
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
        inputs["lock"]["scope"]["timerange"],
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
    measurement = measure_cli_process(
        arguments,
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    measurement["report"] = read_json(report_path) if report_path.is_file() else None
    measurement["output_directory"] = output
    measurement["result_sha256"] = _engine_surface_sha(measurement)
    write_measurement_checkpoint(output, measurement)
    return measurement


def _measure_reference(
    baseline_directory: Path,
    market_snapshot: Path | None,
    output: Path,
    *,
    timeout_seconds: int,
    swap_cap_bytes: int | None,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        "reference",
        "research",
        str(baseline_directory),
        "--output-dir",
        str(output),
        "--timeout",
        str(timeout_seconds),
        "--memory-mode",
        "certification-swap",
        "--storage-mode",
        "spooled",
    ]
    if market_snapshot is not None:
        arguments.extend(
            [
                "--markets",
                str(market_snapshot),
                "--no-market-capture",
            ]
        )
    if swap_cap_bytes is not None:
        arguments.extend(["--swap-cap-gib", str(swap_cap_bytes / 1024**3)])
    measurement = measure_cli_process(
        arguments,
        output.parent / f"{output.name}.stdout.log",
        output.parent / f"{output.name}.stderr.log",
        timeout_seconds=timeout_seconds,
    )
    report_path = output / "run.json"
    measurement["report"] = read_json(report_path) if report_path.is_file() else None
    measurement["output_directory"] = output
    measurement["result_sha256"] = _reference_surface_sha(measurement)
    write_measurement_checkpoint(output, measurement)
    return measurement


def _run_probes(
    probes: list[tuple[Path, dict[str, Any]]],
    output: Path,
    *,
    execution_profile_path: str | Path,
    timeout_seconds: int,
    resume: bool,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for index, (manifest_path, manifest) in enumerate(probes, start=1):
        probe_output = output / f"{index:02d}-{manifest['probe_kind']}"
        performance_path = probe_output / "performance.json"
        if resume and performance_path.is_file():
            performance = read_json(performance_path)
            if performance.get("fixture_id") != manifest["fixture_id"]:
                raise BenchmarkError(
                    f"resumed state probe differs from its manifest: {probe_output}"
                )
        else:
            require_stage_available(
                probe_output,
                stage=f"state probe {manifest['fixture_id']}",
            )
            performance = run_performance_gate(
                manifest_path,
                probe_output,
                profile_path=execution_profile_path,
                verification_level="full",
                repetitions=1,
                timeout_seconds=timeout_seconds,
            )
        reports.append(
            {
                "fixture_id": manifest["fixture_id"],
                "probe_kind": manifest["probe_kind"],
                "manifest_sha256": sha256_file(manifest_path),
                "complete": performance["complete"],
                "trade_surface_equal": performance["gates"]["parity"]["met"],
                "full_state_equal": _performance_full_state_equal(performance),
                "coverage_met": True,
                "performance_report": artifact_record(
                    probe_output / "performance.json",
                    relative_to=output.parent,
                ),
            }
        )
    return reports


def _require_complete_baseline(
    measurement: dict[str, Any],
    lock: dict[str, Any],
) -> None:
    if not _engine_complete(measurement, lock):
        raise BenchmarkError(
            "Full X7 warmup/baseline did not complete a cold strict native run; "
            "inspect warmups/engine/run.json"
        )


def _engine_complete(measurement: dict[str, Any], lock: dict[str, Any]) -> bool:
    report = measurement.get("report")
    return bool(
        measurement.get("exit_code") == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and report.get("pipeline_evidence", {}).get("cold") is True
        and report.get("data", {}).get("history_coverage_policy") == "strict"
        and report.get("data", {}).get("coverage_shortfall_count") == 0
        and report.get("data", {}).get("aggregate_sha256") == lock["data"]["aggregate_sha256"]
        and not report.get("capability", {}).get("blockers")
        and isinstance(measurement.get("result_sha256"), str)
    )


def _reference_complete(measurement: dict[str, Any]) -> bool:
    report = measurement.get("report")
    memory = report.get("container_memory") if isinstance(report, dict) else None
    return bool(
        measurement.get("exit_code") == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and report.get("exact_parity") is True
        and isinstance(measurement.get("result_sha256"), str)
        and isinstance(memory, dict)
        and memory.get("verdict") not in {"oom_killed", "possible_oom"}
    )


def _engine_surface_sha(measurement: dict[str, Any]) -> str | None:
    report = measurement.get("report")
    result = report.get("result") if isinstance(report, dict) else None
    surface = result.get("trade_surface") if isinstance(result, dict) else None
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _reference_surface_sha(measurement: dict[str, Any]) -> str | None:
    report = measurement.get("report")
    surface = report.get("official_trade_surface") if isinstance(report, dict) else None
    value = surface.get("sha256") if isinstance(surface, dict) else None
    return value if isinstance(value, str) else None


def _determinism(
    baseline_hash: str | None,
    engine_runs: list[dict[str, Any]],
    reference_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    hashes = [
        baseline_hash,
        *(run.get("result_sha256") for run in engine_runs),
        *(run.get("result_sha256") for run in reference_runs),
    ]
    valid = all(isinstance(value, str) for value in hashes)
    unique = sorted({value for value in hashes if isinstance(value, str)})
    return {
        "met": valid and len(unique) == 1,
        "result_sha256": unique,
        "rule": "warmup, native, and official surfaces must have one identical SHA-256",
    }


def _run_summary(runs: list[dict[str, Any]], *, lane: str) -> dict[str, Any]:
    wall = [float(run["wall_time_seconds"]) for run in runs]
    peaks = []
    for run in runs:
        peak = int(run["peak_rss_bytes"])
        report = run.get("report")
        if lane == "engine" and isinstance(report, dict):
            result = report.get("result")
            execution = result.get("execution") if isinstance(result, dict) else None
            native_peak = execution.get("peak_rss_bytes") if isinstance(execution, dict) else None
            if isinstance(native_peak, int):
                peak = max(peak, native_peak)
        elif lane == "reference" and isinstance(report, dict):
            memory = report.get("container_memory")
            container_peak = memory.get("peak_bytes") if isinstance(memory, dict) else None
            if isinstance(container_peak, int):
                peak = max(peak, container_peak)
        peaks.append(peak)
    return {
        "wall_time_seconds": {
            "minimum": min(wall),
            "median": statistics.median(wall),
            "maximum": max(wall),
        },
        "peak_rss_bytes": {
            "minimum": min(peaks),
            "maximum": max(peaks),
        },
    }


def _public_run_record(
    measurement: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    """Project one raw run to the small, path-safe release evidence surface."""
    output = measurement.get("output_directory")
    report_path = Path(output) / "run.json" if isinstance(output, Path) else None
    record: dict[str, Any] = {
        "wall_time_seconds": measurement["wall_time_seconds"],
        "peak_rss_bytes": measurement["peak_rss_bytes"],
        "exit_code": measurement["exit_code"],
        "timed_out": measurement["timed_out"],
        "result_sha256": measurement.get("result_sha256"),
    }
    if report_path is not None and report_path.is_file():
        record["run_report"] = artifact_record(report_path, relative_to=root)
    else:
        record["run_report"] = None
    for stream in ("stdout", "stderr"):
        raw = measurement.get(stream)
        stream_path = Path(raw["path"]) if isinstance(raw, dict) else None
        record[stream] = (
            artifact_record(stream_path, relative_to=root)
            if stream_path is not None and stream_path.is_file()
            else None
        )
    return record


def _relative_spread(runs: list[dict[str, Any]]) -> float:
    values = [float(run["wall_time_seconds"]) for run in runs]
    median = statistics.median(values)
    return (max(values) - min(values)) / median if median > 0 else 0.0


def _performance_full_state_equal(performance: dict[str, Any]) -> bool:
    for lane in ("engine", "reference"):
        runs = performance.get(lane, {}).get("runs", [])
        if not runs:
            return False
        for run in runs:
            report = run.get("report")
            state = (
                report.get("parity", {}).get("state_trace") if isinstance(report, dict) else None
            )
            if not isinstance(state, dict) or state.get("equal") is not True:
                return False
    return True
