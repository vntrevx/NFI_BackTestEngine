"""Release-grade Full X7 certification over the real research pipeline."""

from __future__ import annotations

import hashlib
import json
import statistics
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .branch_coverage import validate_fixture_coverage
from .cache import cache_key
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
    validate_reference_oracle,
    write_measurement_checkpoint,
)
from .hardware import (
    current_resource_limits,
    inspect_hardware,
    load_execution_profile,
)
from .market_snapshot import validate_release_market_snapshot
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
from .release_contract import (
    ReleaseModeContract,
    release_contract_for_config,
    release_contract_for_scope,
)
from .release_inputs import (
    LEGACY_RELEASE_INPUT_LOCK_VERSION,
    RELEASE_INPUT_LOCK_VERSION,
    release_data_history_coverage_policy,
    release_history_coverage_policy,
    validate_listing_aware_market_snapshot,
    validate_release_data_roles,
    validate_release_input_lock,
)
from .research_reference import (
    official_backtest_config,
    validate_reference_market_snapshot,
)
from .result_report import write_result_presentation
from .specs import FULL_X7_CERTIFICATION_V2_SCHEMA, validate_schema
from .timerange import parse_timerange_milliseconds
from .vector_runtime import VECTOR_PIPELINE_VERSION

FULL_X7_CERTIFICATION_VERSION = "2.0.0"


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
    engine_complete = all(
        _engine_reuse_complete(run, inputs["lock"]) for run in engine_runs
    )
    reference_complete = _reference_complete(reference_warmup)
    profile_memory = current_resource_limits(profile)["working_memory_bytes"]
    observed_peak = max(
        cold_summary["peak_rss_bytes"]["maximum"],
        engine_summary["peak_rss_bytes"]["maximum"],
    )
    memory_met = observed_peak <= profile_memory
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
        "state_probes": {
            "met": probe_met,
            "required_kinds": sorted(inputs["contract"].required_probe_kinds),
            "required_protection_methods": sorted(
                inputs["contract"].required_protection_methods
            ),
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
            "history_coverage_policy": release_history_coverage_policy(
                inputs["lock"]
            ),
            "evidence": "continuous-oracle-plus-official-full-state-probes",
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


def verify_installed_wheel(
    wheel_path: str | Path,
    build: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bind every installed package member to the exact candidate wheel bytes."""
    wheel = Path(wheel_path).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise BenchmarkError(f"release wheel does not exist: {wheel}")
    if build.get("kind") != "pyo3-extension":
        raise BenchmarkError("Full X7 certification must run an installed native wheel")
    suffixes = (".pyd", ".so", ".dylib")
    with zipfile.ZipFile(wheel) as archive:
        package_members = sorted(
            name
            for name in archive.namelist()
            if name.startswith("nfi_backtest_engine/") and not name.endswith("/")
        )
        if not package_members:
            raise BenchmarkError("release wheel has no nfi_backtest_engine package files")
        candidates = sorted(
            name
            for name in package_members
            if name.startswith("nfi_backtest_engine/_rust") and name.endswith(suffixes)
        )
        if len(candidates) != 1:
            raise BenchmarkError(
                f"release wheel must contain exactly one native extension; found {len(candidates)}"
            )
        member_sha = hashlib.sha256(archive.read(candidates[0])).hexdigest()
        installed_root = (
            Path(package_root).resolve()
            if package_root is not None
            else Path(__file__).resolve().parent
        )
        member_records: list[tuple[str, str]] = []
        portable_member_records: list[tuple[str, str]] = []
        for name in package_members:
            relative = Path(name).relative_to("nfi_backtest_engine")
            installed = installed_root / relative
            wheel_sha = hashlib.sha256(archive.read(name)).hexdigest()
            if not installed.is_file() or sha256_file(installed) != wheel_sha:
                raise BenchmarkError(
                    f"installed package file does not match the candidate wheel: {relative}"
                )
            member_records.append((name, wheel_sha))
            if name != candidates[0]:
                portable_member_records.append((name, wheel_sha))
    installed_sha = build.get("binary_sha256")
    equal = member_sha == installed_sha
    if not equal:
        raise BenchmarkError("imported native extension does not match the candidate wheel")
    package_identity = hashlib.sha256(
        json.dumps(
            member_records,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    portable_package_identity = hashlib.sha256(
        json.dumps(
            portable_member_records,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "path": str(wheel),
        "sha256": sha256_file(wheel),
        "bytes": wheel.stat().st_size,
        "native_member": candidates[0],
        "native_member_sha256": member_sha,
        "installed_extension_sha256": installed_sha,
        "installed_extension_equal": equal,
        "installed_package_files": len(member_records),
        "installed_package_sha256": package_identity,
        "portable_package_files": len(portable_member_records),
        "portable_package_sha256": portable_package_identity,
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
    contract = release_contract_for_scope(
        lock["scope"],
        legacy_spot=lock["schema_version"] == LEGACY_RELEASE_INPUT_LOCK_VERSION,
    )
    _validate_full_x7_timeframes(lock["scope"]["timeframes"])
    return _resolve_full_x7_inputs(
        lock_path=lock_path,
        lock=lock,
        contract=contract,
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
    contract: ReleaseModeContract,
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
    config_contract = release_contract_for_config(loaded["config"])
    if config_contract.contract_id != contract.contract_id:
        raise SpecValidationError("selected config mode differs from the release input lock")
    seal_path = lock_path.parent / "data-seal.json"
    seal = validate_data_seal(seal_path)
    _validate_release_data_seal(
        lock,
        seal,
        data_directory=data_root,
        contract=contract,
    )
    engine_market_document = read_json(engine_markets)
    validate_release_market_snapshot(
        engine_market_document,
        contract=contract,
        pairs=lock["pairlist"]["pairs"],
    )
    validate_listing_aware_market_snapshot(lock, engine_market_document)
    if reference_markets is not None:
        validate_reference_market_snapshot(
            read_json(reference_markets),
            expected_exchange=contract.exchange,
            expected_trading_mode=contract.trading_mode,
            required_pairs=lock["pairlist"]["pairs"],
        )
    start_ms, end_ms = parse_timerange_milliseconds(lock["scope"]["timerange"])
    actual_days = (end_ms - start_ms) // 86_400_000
    if actual_days < MIN_RELEASE_BACKTEST_DAYS:
        raise SpecValidationError(
            f"Full X7 timerange has {actual_days} days; {MIN_RELEASE_BACKTEST_DAYS} required"
        )
    return {
        "lock": lock,
        "contract": contract,
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
            "mode_contract": contract.contract_id,
            "reference": lock["reference"],
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
    contract: ReleaseModeContract,
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
        lock.get("schema_version") == RELEASE_INPUT_LOCK_VERSION
        and seal["coverage_shortfalls"] != data["coverage_shortfalls"]
    ):
        raise SpecValidationError(
            "data seal coverage shortfalls differ from the release input lock"
        )
    if (
        request["pairs"] != lock["pairlist"]["pairs"]
        or request["timerange"] != scope["timerange"]
        or request["timeframes"] != scope["timeframes"]
        or request["history_coverage_policy"]
        != release_data_history_coverage_policy(lock)
        or request["startup_coverage_policy"] != data["startup_coverage_policy"]
    ):
        raise SpecValidationError("data seal request differs from the release input lock")
    role_counts = validate_release_data_roles(
        seal,
        contract=contract,
        history_coverage_policy=release_history_coverage_policy(lock),
        market_onboarding_ms=data.get("market_onboarding_ms"),
    )
    locked_role_counts = data.get("role_counts")
    if (
        lock.get("schema_version") != LEGACY_RELEASE_INPUT_LOCK_VERSION
        and role_counts != locked_role_counts
    ):
        raise SpecValidationError("data seal roles differ from the release input lock")


def _validate_probe_matrix(
    manifests: list[str | Path],
    *,
    contract: ReleaseModeContract,
    expected_upstream_commit: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    probes: list[tuple[Path, dict[str, Any]]] = []
    kinds: set[str] = set()
    coverage_by_kind: dict[str, list[dict[str, Any]]] = {}
    all_coverage: list[dict[str, Any]] = []
    for value in manifests:
        path = Path(value).resolve()
        manifest = validate_fixture(path)
        if manifest["schema_version"] != "3.0.0":
            raise SpecValidationError("Full X7 probes must use fixture manifest v3")
        config_inputs = [
            item
            for item in manifest["inputs"]
            if isinstance(item, dict) and item.get("role") == "config"
        ]
        if len(config_inputs) != 1:
            raise SpecValidationError("Full X7 probe must seal exactly one config")
        probe_config = read_json(path.parent / config_inputs[0]["path"])
        if not isinstance(probe_config, dict):
            raise SpecValidationError("Full X7 probe config must be an object")
        probe_contract = release_contract_for_config(probe_config)
        if probe_contract.contract_id != contract.contract_id:
            raise SpecValidationError(
                "Full X7 probe mode differs from the release input lock"
            )
        provenance = manifest.get("strategy_provenance")
        if expected_upstream_commit is not None and (
            not isinstance(provenance, dict)
            or provenance.get("upstream_commit") != expected_upstream_commit
        ):
            raise SpecValidationError(
                "Full X7 probe upstream commit differs from the release input lock"
            )
        coverage = validate_fixture_coverage(path, manifest)
        observed = coverage["observed"]
        kind = manifest["probe_kind"]
        kinds.add(kind)
        coverage_by_kind.setdefault(kind, []).append(observed)
        all_coverage.append(observed)
        probes.append((path, manifest))
    missing = sorted(contract.required_probe_kinds - kinds)
    if missing:
        raise SpecValidationError("Full X7 probe matrix is incomplete: " + ", ".join(missing))
    for requirement in contract.probe_evidence:
        observed = _merge_probe_coverage(
            coverage_by_kind.get(requirement.probe_kind, [])
        )
        missing_evidence = requirement.missing_from(observed)
        if missing_evidence:
            raise SpecValidationError(
                f"Full X7 {requirement.probe_kind} probe evidence is incomplete: "
                + ", ".join(missing_evidence)
            )
    aggregate = _merge_probe_coverage(all_coverage)
    missing_protections = sorted(
        contract.required_protection_methods
        - set(aggregate["protection_methods"])
    )
    if missing_protections:
        raise SpecValidationError(
            "Full X7 protection probe matrix is incomplete: " + ", ".join(missing_protections)
        )
    if (
        contract.require_rejected_locked_entry
        and not aggregate["rejected_locked_entry"]
    ):
        raise SpecValidationError(
            "Full X7 protection probe matrix did not reject a locked entry"
        )
    return probes


def _merge_probe_coverage(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine independent official probes without weakening any observation."""

    list_fields = (
        "callbacks",
        "entry_tags",
        "compound_tags",
        "protection_methods",
        "exit_reasons",
        "sides",
        "leverages",
    )
    return {
        **{
            field: sorted(
                {
                    value
                    for observed in observations
                    for value in observed.get(field, [])
                }
            )
            for field in list_fields
        },
        "lock_count": sum(
            int(observed.get("lock_count", 0)) for observed in observations
        ),
        "funded_trades": sum(
            int(observed.get("funded_trades", 0)) for observed in observations
        ),
        "rejected_locked_entry": any(
            observed.get("rejected_locked_entry") is True
            for observed in observations
        ),
    }


def _measure_engine(
    inputs: dict[str, Any],
    output: Path,
    *,
    profile_path: Path,
    timeout_seconds: int,
    resume: bool = False,
    vector_cache: Path | None = None,
    recalibrate: bool = True,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    pairs = inputs["lock"]["pairlist"]["pairs"]
    cache_directory = (
        vector_cache.resolve()
        if vector_cache is not None
        else (output / "cold-vector-cache").resolve()
    )
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
        "--cache-dir",
        str(cache_directory),
        "--markets",
        str(inputs["engine_market_snapshot"]),
        "--no-market-download",
        "--registry",
        str(output / "runs.sqlite"),
        "--profile",
        str(profile_path),
        "--no-download",
        "--history-coverage",
        release_data_history_coverage_policy(inputs["lock"]),
    ]
    if recalibrate:
        arguments.append("--recalibrate")
    for pair in pairs:
        arguments.extend(["--pair", pair])
    if resume:
        arguments.append("--resume")
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
    if report_path.is_file():
        write_result_presentation(output)
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
            "Full X7 warmup/baseline did not complete its locked native history "
            "contract; "
            "inspect warmups/engine/run.json"
        )


def _engine_complete(
    measurement: dict[str, Any],
    lock: dict[str, Any],
    *,
    require_cold: bool = True,
) -> bool:
    report = measurement.get("report")
    return bool(
        measurement.get("exit_code") == 0
        and isinstance(report, dict)
        and report.get("complete") is True
        and (not require_cold or report.get("pipeline_evidence", {}).get("cold") is True)
        and report.get("data", {}).get("history_coverage_policy")
        == release_data_history_coverage_policy(lock)
        and report.get("data", {}).get("coverage_shortfall_count")
        == lock["data"].get("coverage_shortfall_count", 0)
        and report.get("data", {}).get("aggregate_sha256") == lock["data"]["aggregate_sha256"]
        and not report.get("capability", {}).get("blockers")
        and isinstance(measurement.get("result_sha256"), str)
    )


def _engine_reuse_complete(
    measurement: dict[str, Any],
    lock: dict[str, Any],
) -> bool:
    """Require a fresh simulation over every content-addressed vector cache hit."""
    if not _engine_complete(measurement, lock, require_cold=False):
        return False
    report = measurement["report"]
    evidence = report.get("pipeline_evidence")
    vectors = report.get("vectors")
    pairs = lock["pairlist"]["pairs"]
    outputs = vectors.get("outputs") if isinstance(vectors, dict) else None
    if (
        not isinstance(evidence, dict)
        or evidence.get("cold") is not False
        or evidence.get("vector_cache_hits") != len(pairs)
        or any(
            evidence.get(field) is not False
            for field in (
                "data_checkpoint_reused",
                "vector_checkpoint_reused",
                "manifest_checkpoint_reused",
                "engine_checkpoint_reused",
                "surface_checkpoint_reused",
            )
        )
        or report.get("resumed_stages") != []
        or not isinstance(vectors, dict)
        or vectors.get("pair_count") != len(pairs)
        or vectors.get("cache_hits") != len(pairs)
        or not isinstance(outputs, list)
        or [item.get("pair") for item in outputs if isinstance(item, dict)] != pairs
    ):
        return False
    return all(
        isinstance(item, dict)
        and item.get("cache_hit") is True
        and isinstance(item.get("cache_key"), str)
        and isinstance(item.get("sha256"), str)
        for item in outputs
    )


def _seal_preserved_vector_cache(
    baseline: dict[str, Any],
    *,
    pairs: list[str],
    destination: Path,
) -> dict[str, Any]:
    """Bind the reusable cache to the cold baseline's exact 80 vector artifacts."""
    report = baseline.get("report")
    output = baseline.get("output_directory")
    if not isinstance(report, dict):
        raise BenchmarkError("cold seed has no complete run report")
    vectors = report.get("vectors")
    if not isinstance(vectors, dict):
        raise BenchmarkError("cold seed has no vector report")
    records = vectors.get("outputs")
    if (
        not isinstance(output, Path)
        or not isinstance(records, list)
        or [item.get("pair") for item in records if isinstance(item, dict)] != pairs
    ):
        raise BenchmarkError("cold seed has no complete ordered vector cache records")
    cache_root = (output / "cold-vector-cache").resolve()
    if not cache_root.is_dir():
        raise BenchmarkError("cold seed vector cache does not exist")

    entries: list[dict[str, Any]] = []
    for pair, record in zip(pairs, records, strict=True):
        if not isinstance(record, dict):
            raise BenchmarkError(f"cold seed vector record is invalid: {pair}")
        vector_key = record.get("cache_key")
        vector_sha = record.get("sha256")
        vector_bytes = record.get("bytes")
        if (
            not isinstance(vector_key, str)
            or not vector_key.startswith("vectors-")
            or not isinstance(vector_sha, str)
            or not isinstance(vector_bytes, int)
        ):
            raise BenchmarkError(f"cold seed vector identity is invalid: {pair}")
        _validate_preserved_cache_entry(
            cache_root,
            vector_key,
            expected_bytes=vector_bytes,
            expected_sha256=vector_sha,
        )
        record_key = cache_key(
            "vector-records",
            {
                "vector_cache_key": vector_key,
                "vector_pipeline_version": VECTOR_PIPELINE_VERSION,
            },
        )
        record_payload = _validate_preserved_cache_entry(cache_root, record_key)
        try:
            cached_record = json.loads(record_payload.read_bytes())
        except (OSError, ValueError) as exc:
            raise BenchmarkError(f"cold seed cached vector metadata is invalid: {pair}") from exc
        if not isinstance(cached_record, dict) or any(
            cached_record.get(field) != record.get(field)
            for field in (
                "pair",
                "base_timeframe",
                "bytes",
                "sha256",
                "input_sha256",
                "strategy_sha256",
                "config_sha256",
            )
        ):
            raise BenchmarkError(f"cold seed cached vector metadata differs: {pair}")
        entries.append(
            {
                "pair": pair,
                "vector_key": vector_key,
                "vector_bytes": vector_bytes,
                "vector_sha256": vector_sha,
                "record_key": record_key,
                "record_bytes": record_payload.stat().st_size,
                "record_sha256": sha256_file(record_payload),
            }
        )

    encoded_entries = json.dumps(
        entries,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest = {
        "schema_version": "1.0.0",
        "source_run_id": report.get("run_id"),
        "vector_pipeline_version": vectors.get("pipeline_version"),
        "strategy_sha256": vectors.get("strategy_sha256"),
        "config_sha256": vectors.get("config_sha256"),
        "pair_count": len(pairs),
        "entries_sha256": hashlib.sha256(encoded_entries).hexdigest(),
        "entries": entries,
    }
    if destination.is_file():
        if read_json(destination) != manifest:
            raise BenchmarkError("preserved vector cache seal differs from the cold seed")
    else:
        if destination.exists():
            raise BenchmarkError("preserved vector cache seal path is not a file")
        write_json(destination, manifest)
    return {
        "root": cache_root,
        "manifest": destination,
        "record": artifact_record(destination, relative_to=destination.parent),
    }


def _validate_preserved_cache_entry(
    cache_root: Path,
    key: str,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> Path:
    entry = (cache_root / key).resolve()
    if not entry.is_relative_to(cache_root):
        raise BenchmarkError("preserved vector cache key escapes its root")
    payload = entry / "payload"
    metadata_path = entry / "metadata.json"
    if not payload.is_file() or not metadata_path.is_file():
        raise BenchmarkError(f"preserved vector cache entry is incomplete: {key}")
    try:
        metadata = read_json(metadata_path)
    except (OSError, ValueError) as exc:
        raise BenchmarkError(
            f"preserved vector cache metadata is invalid: {key}"
        ) from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("key") != key
        or metadata.get("bytes") != payload.stat().st_size
        or metadata.get("sha256") != sha256_file(payload)
    ):
        raise BenchmarkError(f"preserved vector cache entry failed its metadata seal: {key}")
    if (
        expected_bytes is not None
        and metadata["bytes"] != expected_bytes
        or expected_sha256 is not None
        and metadata["sha256"] != expected_sha256
    ):
        raise BenchmarkError(f"preserved vector cache entry differs from the cold seed: {key}")
    return payload


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
