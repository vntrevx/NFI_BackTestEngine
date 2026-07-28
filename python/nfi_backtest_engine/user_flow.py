"""Auditable preflight, consent, verification, and report-opening user flow."""

from __future__ import annotations

import hashlib
import json
import shutil
import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .canonical import read_json, write_json
from .docker_resources import derive_docker_policy, inspect_docker_daemon
from .errors import BenchmarkError, NfiBacktestError, SpecValidationError
from .fixture import sha256_file
from .hardware import inspect_hardware
from .project_config import ProjectSettings
from .reference_runtime import ensure_docker_config
from .verification_ledger import VerificationLedger, create_verification_record

RUN_PREFLIGHT_VERSION = "1.0.0"
OFFICIAL_VERIFICATION_DIRECTORY = "official-verification"
REPORT_FILENAME = "report.html"

Prompt = Callable[[str], str]


def inspect_run_preflight(
    settings: ProjectSettings,
    *,
    resume: bool,
    download_missing: bool,
) -> dict[str, Any]:
    """Measure host and known disk requirements without changing run evidence."""
    hardware = inspect_hardware(settings.workspace)
    data_usage = _tree_usage(settings.data_directory)
    output_usage = _tree_usage(settings.output_directory)
    control_paths = (settings.strategy_path, settings.config_path, settings.project_path)
    control_bytes = sum(path.stat().st_size for path in control_paths if path.is_file())
    largest_known_input = max(control_bytes, data_usage["largest_file_bytes"])
    known_input_bytes = data_usage["logical_bytes"] + control_bytes

    # A fresh run reserves one known-input-sized work copy and an equally sized
    # input-derived envelope. A resume receives credit only against the work copy;
    # the whole envelope remains reserved for checkpoint and report finalization.
    # No strategy, pair, timerange, or fixed GiB assumption participates.
    remaining_work_bytes = max(
        0,
        known_input_bytes - output_usage["logical_bytes"] if resume else known_input_bytes,
    )
    safety_margin_bytes = max(known_input_bytes, largest_known_input)
    required_free_bytes = remaining_work_bytes + safety_margin_bytes
    disk_path = _existing_parent(settings.output_directory)
    disk = psutil.disk_usage(str(disk_path))
    sufficient = disk.free >= required_free_bytes
    download_growth_bounded = data_usage["file_count"] > 0 or not download_missing

    return {
        "schema_version": RUN_PREFLIGHT_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "project": str(settings.project_path),
        "output_directory": str(settings.output_directory),
        "resume": resume,
        "host": {
            "system": hardware["system"],
            "machine": hardware["machine"],
            "affinity_cpu_count": hardware["affinity_cpu_count"],
            "available_memory_bytes": hardware["memory"]["available_bytes"],
        },
        "docker": _inspect_optional_docker(),
        "disk": {
            "filesystem_path": str(disk_path),
            "available_bytes": disk.free,
            "known_input_bytes": known_input_bytes,
            "known_data_logical_bytes": data_usage["logical_bytes"],
            "known_data_allocated_bytes": data_usage["allocated_bytes"],
            "known_data_file_count": data_usage["file_count"],
            "existing_output_bytes": output_usage["logical_bytes"],
            "estimated_remaining_work_bytes": remaining_work_bytes,
            "safety_margin_bytes": safety_margin_bytes,
            "required_free_bytes": required_free_bytes,
            "download_growth_bounded": download_growth_bounded,
            "sufficient": sufficient,
            "estimate_policy": (
                "remaining known-input work plus one input-derived safety envelope; "
                "existing owned output is credited only for resume"
            ),
        },
        "passed": sufficient,
    }


def write_run_preflight(
    settings: ProjectSettings,
    *,
    resume: bool,
    download_missing: bool,
) -> tuple[dict[str, Any], Path]:
    """Persist the latest measured preflight outside the immutable run directory."""
    report = inspect_run_preflight(
        settings,
        resume=resume,
        download_missing=download_missing,
    )
    destination = settings.project_path.parent / "run-preflight.json"
    write_json(destination, report)
    if not report["passed"]:
        disk = report["disk"]
        raise SpecValidationError(
            "disk preflight failed: "
            f"{disk['required_free_bytes']} bytes required including the measured "
            f"safety margin, {disk['available_bytes']} bytes available at "
            f"{disk['filesystem_path']}"
        )
    return report, destination


def format_run_preflight(report: Mapping[str, Any], destination: Path) -> str:
    """Render a compact, factual preflight summary."""
    host = _mapping(report, "host")
    docker = _mapping(report, "docker")
    disk = _mapping(report, "disk")
    docker_detail = (
        f"{docker.get('container_memory_limit_bytes')} byte limit"
        if docker.get("status") == "available"
        else str(docker.get("detail", "unavailable"))
    )
    bounded = (
        "bounded from local inputs"
        if disk.get("download_growth_bounded") is True
        else "download growth not yet bounded"
    )
    return (
        "run preflight: "
        f"cpu={host.get('affinity_cpu_count')}, "
        f"memory={host.get('available_memory_bytes')} bytes, "
        f"disk={disk.get('available_bytes')} available / "
        f"{disk.get('required_free_bytes')} required ({bounded}), "
        f"docker={docker_detail} -> {destination}"
    )


def resolve_consent(
    explicit: bool | None,
    *,
    interactive: bool,
    question: str,
    prompt: Prompt | None = None,
) -> bool:
    """Require an explicit flag or an interactive yes; default is always no."""
    if explicit is not None:
        return explicit
    if not interactive:
        return False
    ask = input if prompt is None else prompt
    try:
        answer = ask(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def run_quick_official_verification(
    run_directory: str | Path,
    *,
    timeout_seconds: int | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Reuse a valid exact proof or append one immutable official attempt."""
    root = Path(run_directory).resolve()
    source_run = _load_completed_run(root)
    verification_root = root / OFFICIAL_VERIFICATION_DIRECTORY
    existing = _find_reusable_verification(verification_root, source_run, root)
    if existing is not None:
        return existing[0], existing[1], True

    attempt = _next_attempt_directory(verification_root)
    from .research_reference import run_research_reference

    report = run_research_reference(
        root,
        attempt,
        timeout_seconds=timeout_seconds,
    )
    report_path = attempt / "run.json"
    _validate_official_verification(
        report,
        source_run,
        root,
        report_path=report_path,
        require_exact=False,
    )
    return report, report_path, False


def record_native_completion(
    ledger_path: str | Path,
    run_directory: str | Path,
) -> int:
    """Append the source-bound native-complete state idempotently."""
    root = Path(run_directory).resolve()
    run = _load_completed_run(root)
    fingerprint = _verification_fingerprint(root, run, reference=None)
    record = create_verification_record(
        subject_kind="run",
        subject_id=str(run["run_id"]),
        state="native_complete",
        outcome="success",
        fingerprint=fingerprint,
        evidence=_run_evidence(root),
        recorded_at=str(run["created_at"]),
    )
    with VerificationLedger(ledger_path) as ledger:
        return ledger.append(record)


def record_quick_verification(
    ledger_path: str | Path,
    run_directory: str | Path,
    report: Mapping[str, Any],
    report_path: str | Path,
) -> tuple[int, int]:
    """Append strategy and run quick-verified states from one exact proof."""
    root = Path(run_directory).resolve()
    run = _load_completed_run(root)
    _validate_official_verification(report, run, root, report_path=Path(report_path))
    fingerprint = _verification_fingerprint(root, run, reference=report)
    evidence = [
        *_run_evidence(root),
        _evidence_record("official_verification", Path(report_path)),
        _evidence_record(
            "official_trade_surface",
            Path(report_path).parent / "official-trade-surface.json",
        ),
    ]
    recorded_at = str(report["ended_at"])
    strategy_sha = str(fingerprint["strategy_source_sha256"])
    strategy_record = create_verification_record(
        subject_kind="strategy_revision",
        subject_id=strategy_sha,
        state="quick_verified",
        outcome="success",
        fingerprint=fingerprint,
        evidence=evidence,
        recorded_at=recorded_at,
    )
    run_record = create_verification_record(
        subject_kind="run",
        subject_id=str(run["run_id"]),
        state="quick_verified",
        outcome="success",
        fingerprint=fingerprint,
        evidence=evidence,
        recorded_at=recorded_at,
    )
    with VerificationLedger(ledger_path) as ledger:
        return ledger.append(strategy_record), ledger.append(run_record)


def record_quick_failure(
    ledger_path: str | Path,
    run_directory: str | Path,
    report: Mapping[str, Any],
    report_path: str | Path,
) -> int:
    """Append a failed official attempt without replacing prior successes."""
    root = Path(run_directory).resolve()
    run = _load_completed_run(root)
    proof_path = Path(report_path)
    _validate_official_verification(
        report,
        run,
        root,
        report_path=proof_path,
        require_exact=False,
    )
    if report.get("timed_out") is True:
        code = "OFFICIAL_VERIFICATION_TIMEOUT"
        message = "the pinned official verification timed out"
    elif report.get("exact_parity") is False and report.get("official_trade_surface") is not None:
        code = "OFFICIAL_PARITY_MISMATCH"
        message = "the pinned official result differed from the Native trade surface"
    else:
        code = "OFFICIAL_VERIFICATION_INCOMPLETE"
        message = "the pinned official verification did not complete"
    record = create_verification_record(
        subject_kind="run",
        subject_id=str(run["run_id"]),
        state="failed",
        outcome="failure",
        fingerprint=_verification_fingerprint(root, run, reference=report),
        evidence=_quick_evidence(root, proof_path),
        failure={"code": code, "message": message},
        recorded_at=str(report["ended_at"]),
    )
    with VerificationLedger(ledger_path) as ledger:
        return ledger.append(record)


def finish_one_line_run(
    settings: ProjectSettings,
    *,
    native_status: int,
    verification: bool | None,
    verification_timeout_seconds: int | None,
    open_report: bool | None,
    interactive: bool,
    include_breakdowns: bool,
    emit: Callable[[str], None] = print,
) -> int:
    """Complete consented post-run actions and print one final result view."""
    root = settings.output_directory.resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        if verification is True or open_report is True:
            raise BenchmarkError(
                "the requested post-run action needs a durable run.json result"
            )
        return native_status
    run = read_json(run_path)
    if not isinstance(run, dict):
        raise BenchmarkError(f"run report is not an object: {run_path}")
    complete = run.get("complete") is True and run.get("status") == "complete"
    ledger_path = settings.project_path.parent / "verification-ledger.sqlite"
    final_status = native_status

    if complete:
        native_sequence = record_native_completion(ledger_path, root)
        emit(
            "verification ledger: "
            f"sequence={native_sequence}, state=native_complete -> {ledger_path}"
        )
        verify_now = resolve_consent(
            verification,
            interactive=interactive,
            question=(
                "Run pinned official quick-level verification now? "
                "It replays the selected timerange in Docker and may take much "
                "longer than Native"
            ),
        )
        if verify_now:
            report, proof_path, reused = run_quick_official_verification(
                root,
                timeout_seconds=verification_timeout_seconds,
            )
            from .result_report import write_result_presentation

            write_result_presentation(
                root,
                verification=report,
                verification_path=proof_path,
            )
            if report.get("complete") is True and report.get("exact_parity") is True:
                strategy_sequence, run_sequence = record_quick_verification(
                    ledger_path,
                    root,
                    report,
                    proof_path,
                )
                emit(
                    "official quick verification: exact parity "
                    f"({'reused' if reused else 'new'} proof), "
                    f"ledger sequences={strategy_sequence},{run_sequence} -> "
                    f"{proof_path}"
                )
            else:
                final_status = 1
                failure_sequence = record_quick_failure(
                    ledger_path,
                    root,
                    report,
                    proof_path,
                )
                emit(
                    "official quick verification: exact parity failed, "
                    f"ledger sequence={failure_sequence} -> {proof_path}"
                )
        else:
            emit("official quick verification: skipped (no explicit consent)")
    elif verification is True:
        raise BenchmarkError(
            "official quick verification requires a completed Native run"
        )

    summary_path = root / "summary.json"
    if summary_path.is_file():
        from .result_report import format_terminal_summary, load_result_summary

        emit(
            format_terminal_summary(
                load_result_summary(root),
                root,
                include_breakdowns=include_breakdowns,
            )
        )

    html_path = root / REPORT_FILENAME
    if html_path.is_file():
        open_now = resolve_consent(
            open_report,
            interactive=interactive,
            question="Open the HTML report in the default browser",
        )
        if open_now:
            uri = open_html_report(html_path)
            emit(f"HTML report opened: {uri}")
        else:
            emit("HTML report opening: skipped (no explicit consent)")
    elif open_report is True:
        raise BenchmarkError(f"requested HTML report does not exist: {html_path}")
    return final_status


def report_uri(report_path: str | Path) -> str:
    """Return the platform-native file URI used by the default browser."""
    path = Path(report_path).resolve()
    if not path.is_file():
        raise BenchmarkError(f"HTML report does not exist: {path}")
    return path.as_uri()


def open_html_report(
    report_path: str | Path,
    *,
    opener: Callable[[str], bool] | None = None,
) -> str:
    """Open a report only after its caller has resolved explicit consent."""
    uri = report_uri(report_path)
    open_uri = webbrowser.open_new_tab if opener is None else opener
    if not open_uri(uri):
        raise BenchmarkError(f"the default browser did not accept the HTML report: {uri}")
    return uri


def _inspect_optional_docker() -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {
            "status": "unavailable",
            "detail": "Docker CLI is not on PATH; Native execution remains available",
        }
    try:
        config = ensure_docker_config()
        daemon = inspect_docker_daemon(docker_config=config)
        policy = derive_docker_policy(daemon)
    except (NfiBacktestError, OSError) as exc:
        return {
            "status": "unavailable",
            "detail": str(exc),
        }
    return {
        "status": "available",
        "server_version": daemon["server_version"],
        "cpu_count": daemon["cpu_count"],
        "total_memory_bytes": daemon["total_memory_bytes"],
        "active_container_count": daemon["active_container_count"],
        "container_memory_limit_bytes": policy["container_memory_limit_bytes"],
        "maximum_parallel_containers": policy["maximum_parallel_containers"],
    }


def _tree_usage(root: Path) -> dict[str, int]:
    if not root.exists():
        return {
            "logical_bytes": 0,
            "allocated_bytes": 0,
            "file_count": 0,
            "largest_file_bytes": 0,
        }
    if not root.is_dir():
        raise SpecValidationError(f"disk preflight path is not a directory: {root}")
    logical = 0
    allocated = 0
    count = 0
    largest = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        logical += stat.st_size
        allocated += _allocated_bytes(stat)
        count += 1
        largest = max(largest, stat.st_size)
    return {
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "file_count": count,
        "largest_file_bytes": largest,
    }


def _allocated_bytes(stat: Any) -> int:
    blocks = getattr(stat, "st_blocks", None)
    return int(blocks) * 512 if isinstance(blocks, int) and blocks >= 0 else int(stat.st_size)


def _existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise SpecValidationError(f"cannot resolve an existing disk parent for {path}")
        candidate = parent
    return candidate


def _load_completed_run(root: Path) -> dict[str, Any]:
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"quick verification requires a completed Native run: {run_path}")
    document = read_json(run_path)
    if (
        not isinstance(document, dict)
        or document.get("complete") is not True
        or document.get("status") != "complete"
        or not isinstance(document.get("run_id"), str)
    ):
        raise BenchmarkError(f"quick verification requires a completed Native run: {run_path}")
    identity_path = root / "identity.json"
    if not identity_path.is_file():
        raise BenchmarkError(
            f"completed Native run failed its sealed identity: {identity_path}"
        )
    identity_document = read_json(identity_path)
    inputs = document.get("inputs")
    if (
        not isinstance(identity_document, dict)
        or not isinstance(inputs, dict)
        or identity_document.get("identity") != inputs
        or identity_document.get("run_id") != document["run_id"]
        or _canonical_sha256(inputs) != document["run_id"]
    ):
        raise BenchmarkError(
            f"completed Native run failed its sealed identity: {identity_path}"
        )
    surface_path = root / "trade-surface.json"
    result = _mapping(document, "result")
    surface_record = _mapping(result, "trade_surface")
    _validate_file_record(
        surface_record,
        expected_path=surface_path,
        label="completed Native run trade surface",
    )
    return document


def _find_reusable_verification(
    verification_root: Path,
    source_run: Mapping[str, Any],
    run_root: Path,
) -> tuple[dict[str, Any], Path] | None:
    if not verification_root.exists():
        return None
    if not verification_root.is_dir():
        raise BenchmarkError(
            f"official verification path is not a directory: {verification_root}"
        )
    reusable: tuple[dict[str, Any], Path] | None = None
    for attempt in sorted(verification_root.glob("attempt-*")):
        if not attempt.is_dir():
            raise BenchmarkError(f"official verification attempt is not a directory: {attempt}")
        report_path = attempt / "run.json"
        if not report_path.is_file():
            continue
        report = read_json(report_path)
        if not isinstance(report, dict):
            raise BenchmarkError(f"official verification report is not an object: {report_path}")
        if report.get("complete") is not True:
            continue
        _validate_official_verification(
            report,
            source_run,
            run_root,
            report_path=report_path,
        )
        reusable = (report, report_path)
    return reusable


def _next_attempt_directory(verification_root: Path) -> Path:
    verification_root.mkdir(parents=True, exist_ok=True)
    numbers = []
    for path in verification_root.glob("attempt-*"):
        suffix = path.name.removeprefix("attempt-")
        if not suffix.isdigit():
            raise BenchmarkError(f"invalid official verification attempt name: {path.name}")
        numbers.append(int(suffix))
    return verification_root / f"attempt-{max(numbers, default=0) + 1:04d}"


def _validate_official_verification(
    report: Mapping[str, Any],
    source_run: Mapping[str, Any],
    run_root: Path,
    *,
    report_path: Path,
    require_exact: bool = True,
) -> None:
    if not report_path.is_file():
        raise BenchmarkError(f"official verification report is missing: {report_path}")
    if require_exact and (
        report.get("complete") is not True or report.get("exact_parity") is not True
    ):
        raise BenchmarkError(
            f"official quick verification did not reach exact parity: {report_path}"
        )
    if report.get("run_id") != source_run.get("run_id"):
        raise BenchmarkError("official verification belongs to a different Native run")
    inputs = _mapping(report, "inputs")
    engine_surface = _mapping(inputs, "engine_trade_surface")
    engine_surface_path = run_root / "trade-surface.json"
    _validate_file_record(
        engine_surface,
        expected_path=engine_surface_path,
        label="official verification is bound to a different trade surface",
    )
    expected_surface_sha = sha256_file(engine_surface_path)
    official_surface_value = report.get("official_trade_surface")
    if official_surface_value is not None:
        if not isinstance(official_surface_value, Mapping):
            raise BenchmarkError("official verification trade surface record is invalid")
        _validate_file_record(
            official_surface_value,
            expected_path=report_path.parent / "official-trade-surface.json",
            label="official verification trade surface",
        )
    if report.get("exact_parity") is True:
        official_surface = _mapping(report, "official_trade_surface")
        if official_surface.get("sha256") != expected_surface_sha:
            raise BenchmarkError("official verification surface identity differs from Native")


def _verification_fingerprint(
    root: Path,
    run: Mapping[str, Any],
    *,
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    inputs = _mapping(run, "inputs")
    strategy = _mapping(inputs, "strategy")
    config = _mapping(inputs, "config")
    market = inputs.get("market_metadata")
    market_sha = market.get("sha256") if isinstance(market, Mapping) else None
    capability = _mapping(run, "capability")
    result = _mapping(run, "result")
    execution = _mapping(result, "execution")
    build = _mapping(execution, "build")
    effective = read_json(root / "effective-config.redacted.json")
    if not isinstance(effective, dict):
        raise BenchmarkError("effective config evidence is not an object")
    effective_config = _mapping(effective, "config")
    trading_mode = str(effective_config.get("trading_mode", "spot"))
    margin_mode = effective_config.get("margin_mode")
    mode_contract = (
        f"{trading_mode}:{margin_mode}"
        if isinstance(margin_mode, str) and margin_mode
        else trading_mode
    )
    reference_identity = _mapping(reference, "reference") if reference is not None else {}
    return {
        "upstream_repository": None,
        "upstream_commit": None,
        "strategy_version": None,
        "strategy_source_sha256": _required_sha(strategy, "file_sha256"),
        "strategy_ir_sha256": _optional_sha(strategy.get("capability_fingerprint")),
        "hot_callback_ir_sha256": _optional_sha(capability.get("hot_ir_fingerprint")),
        "config_sha256": _optional_sha(config.get("run_effective_sha256")),
        "pairlist_sha256": _optional_sha(inputs.get("pairlist_sha256")),
        "data_seal_sha256": sha256_file(root / "data-seal.json"),
        "market_snapshot_sha256": _optional_sha(market_sha),
        "timerange": str(inputs["timerange"]),
        "mode_contract": mode_contract,
        "reference_version": reference_identity.get("version"),
        "reference_image_index_digest": reference_identity.get("image_index_digest"),
        "reference_image_platform_digest": reference_identity.get("image_platform_digest"),
        "reference_platform": reference_identity.get("platform"),
        "package_sha256": None,
        "wheel_sha256": None,
        "native_binary_sha256": _optional_sha(build.get("binary_sha256")),
    }


def _run_evidence(root: Path) -> list[dict[str, Any]]:
    return [
        _evidence_record("native_run", root / "run.json"),
        _evidence_record("native_trade_surface", root / "trade-surface.json"),
        _evidence_record("data_seal", root / "data-seal.json"),
    ]


def _quick_evidence(root: Path, report_path: Path) -> list[dict[str, Any]]:
    evidence = [
        *_run_evidence(root),
        _evidence_record("official_verification", report_path),
    ]
    official_surface = report_path.parent / "official-trade-surface.json"
    if official_surface.is_file():
        evidence.append(_evidence_record("official_trade_surface", official_surface))
    return evidence


def _evidence_record(kind: str, path: Path) -> dict[str, Any]:
    target = path.resolve()
    if not target.is_file():
        raise BenchmarkError(f"verification evidence does not exist: {target}")
    return {
        "kind": kind,
        "location": str(target),
        "bytes": target.stat().st_size,
        "sha256": sha256_file(target),
    }


def _validate_file_record(
    record: Mapping[str, Any],
    *,
    expected_path: Path,
    label: str,
) -> None:
    target = expected_path.resolve()
    if (
        not target.is_file()
        or not isinstance(record.get("path"), str)
        or Path(str(record["path"])).resolve() != target
        or record.get("bytes") != target.stat().st_size
        or record.get("sha256") != sha256_file(target)
    ):
        raise BenchmarkError(f"{label} failed its recorded identity")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _required_sha(owner: Mapping[str, Any], key: str) -> str:
    value = owner.get(key)
    if not isinstance(value, str):
        raise BenchmarkError(f"verification fingerprint is missing {key}")
    validated = _optional_sha(value)
    if validated is None:
        raise BenchmarkError(f"verification fingerprint is missing {key}")
    return validated


def _optional_sha(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BenchmarkError(f"verification fingerprint contains an invalid SHA-256: {value!r}")
    return value


def _mapping(owner: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = owner.get(key) if owner is not None else None
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"verification evidence is missing object {key!r}")
    return value
