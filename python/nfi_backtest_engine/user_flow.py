"""Auditable preflight, consent, verification, and result-summary user flow."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import psutil

from .cache_policy import resolve_cache_budget
from .canonical import read_json, write_json
from .data_seal import validate_data_seal
from .docker_resources import derive_docker_policy, inspect_docker_daemon
from .errors import BenchmarkError, NfiBacktestError, SpecValidationError
from .fixture import sha256_file
from .hardware import derive_tuning, inspect_hardware
from .project_config import ProjectSettings
from .reference_runtime import ensure_docker_config
from .verification_ledger import VerificationLedger, create_verification_record

RUN_PREFLIGHT_VERSION = "1.1.0"
OFFICIAL_VERIFICATION_DIRECTORY = "official-verification"
SPINNER_FRAMES = ("◐", "◓", "◑", "◒")


OFFICIAL_FALLBACK_DIRECTORY = "official-fallback"

Prompt = Callable[[str], str]


class RunProgress:
    """Render one live rotating status line without flooding the terminal."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        interactive: bool | None = None,
        interval_seconds: float = 0.08,
    ) -> None:
        self._stream = sys.stdout if stream is None else stream
        self._interactive = (
            self._stream.isatty() if interactive is None else interactive
        )
        self._interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._percent = 0
        self._label = "Starting backtest"
        self._started_at = time.monotonic()
        self._spinner_index = 0
        self._rendered = False

    def __enter__(self) -> RunProgress:
        if self._interactive:
            with self._lock:
                self._render_locked()
            self._thread = threading.Thread(
                target=self._heartbeat,
                name="nfi-run-progress",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def update(self, percent: int, label: str) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("run progress percentage must be between 0 and 100")
        with self._lock:
            self._percent = percent
            self._label = label
            self._render_locked()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval_seconds * 2, 0.1))
        with self._lock:
            if self._interactive and self._rendered:
                self._stream.write("\n")
                self._stream.flush()
                self._rendered = False

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            with self._lock:
                self._render_locked()

    def _render_locked(self) -> None:
        elapsed = max(0, int(time.monotonic() - self._started_at))
        minutes, seconds = divmod(elapsed, 60)
        if self._interactive:
            marker = "✓" if self._percent == 100 else SPINNER_FRAMES[self._spinner_index]
            self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
            line = (
                f"  {marker}  {self._percent:3d}%  {self._label}  "
                f"{minutes:02d}:{seconds:02d}"
            )
            self._stream.write(f"\r\033[2K{line}")
            self._rendered = True
        else:
            line = (
                f"[{self._percent:3d}%] {self._label} "
                f"({minutes:02d}:{seconds:02d} elapsed)"
            )
            self._stream.write(f"{line}\n")
        self._stream.flush()


def inspect_run_preflight(
    settings: ProjectSettings,
    *,
    resume: bool,
    download_missing: bool,
) -> dict[str, Any]:
    """Measure host and known disk requirements without changing run evidence."""
    hardware = inspect_hardware(settings.workspace)
    cpu_worker_limit = int(derive_tuning(hardware)["cpu_process_limit"])
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
    cache_budget = resolve_cache_budget(settings.cache_directory)
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
            "physical_cpu_count": hardware["physical_cpu_count"],
            "logical_cpu_count": hardware["logical_cpu_count"],
            "affinity_cpu_count": hardware["affinity_cpu_count"],
            "cpu_worker_limit": cpu_worker_limit,
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
            "cache_max_bytes": cache_budget.max_bytes,
            "cache_budget_source": cache_budget.source,
            "cache_filesystem_path": str(cache_budget.filesystem_path),
            "cache_filesystem_available_bytes": cache_budget.available_bytes,
            "cache_filesystem_total_bytes": cache_budget.total_bytes,
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
    """Render the successful preflight as one scan-friendly status line."""
    host = _mapping(report, "host")
    docker = _mapping(report, "docker")
    disk = _mapping(report, "disk")
    docker_label = (
        "Docker ready"
        if docker.get("status") == "available"
        else f"Docker {docker.get('detail', 'unavailable')}"
    )
    return (
        "  ✓  System ready"
        f"  ·  {host.get('cpu_worker_limit')} CPU workers"
        f" ({host.get('affinity_cpu_count')} logical visible)"
        f"  ·  {_human_bytes(host.get('available_memory_bytes'))} RAM"
        f"  ·  {_human_bytes(disk.get('available_bytes'))} disk"
        f"  ·  {docker_label}"
    )


def format_run_banner(settings: ProjectSettings, *, resume: bool) -> str:
    """Render a compact product banner and the immutable run selection."""
    if settings.pairs is None:
        pair_label = "all pairs"
    else:
        pair_count = len(settings.pairs)
        pair_label = f"{pair_count} pair{'s' if pair_count != 1 else ''}"
    resume_label = "  ↻  Resuming hash-valid checkpoints" if resume else ""
    return "\n".join(
        [
            " _   _  _____  ___",
            r"| \ | ||  ___||_ _|",
            r"|  \| || |_    | |",
            r"| |\  ||  _|   | |",
            r"|_| \_||_|    |___|  BACKTEST ENGINE",
            "",
            (
                f"  {settings.class_name}  ·  "
                f"{_display_timerange(settings.timerange)}  ·  {pair_label}"
            ),
            resume_label,
        ]
    ).rstrip()


def _display_timerange(value: str) -> str:
    parts = value.split("-", maxsplit=1)
    if len(parts) != 2:
        return value

    def date_token(token: str) -> str:
        if len(token) >= 8 and token[:8].isdigit():
            return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
        return token or "open"

    return f"{date_token(parts[0])} → {date_token(parts[1])}"


def _human_bytes(value: Any) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable byte unit")


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


def inspect_official_fallback_preflight(
    run_directory: str | Path,
) -> dict[str, Any]:
    """Recheck Docker, memory, and input-derived disk headroom."""

    root = Path(run_directory).resolve()
    run = _load_blocked_run(root)
    docker = _inspect_optional_docker()
    seal = read_json(root / "data-seal.json")
    files = seal.get("files") if isinstance(seal, Mapping) else None
    file_sizes = (
        [
            int(item["bytes"])
            for item in files
            if isinstance(item, Mapping)
            and isinstance(item.get("bytes"), int)
            and not isinstance(item["bytes"], bool)
            and item["bytes"] >= 0
        ]
        if isinstance(files, list)
        else []
    )
    input_bytes = sum(file_sizes)
    largest_input_bytes = max(file_sizes, default=0)
    existing_output_bytes = _tree_usage(root)["logical_bytes"]
    # The official lane mounts candle data read-only. Reserve an output envelope
    # derived from the larger of the existing run evidence and the largest sealed
    # input; no pair, timerange, strategy, or fixed GiB constant participates.
    output_envelope_bytes = max(existing_output_bytes, largest_input_bytes)
    disk_path = _existing_parent(root)
    disk = psutil.disk_usage(str(disk_path))
    memory = psutil.virtual_memory()
    passed = docker.get("status") == "available" and disk.free >= output_envelope_bytes
    return {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "run_id": run["run_id"],
        "docker": docker,
        "memory": {
            "available_bytes": int(memory.available),
            "docker_limit_bytes": docker.get("container_memory_limit_bytes"),
        },
        "disk": {
            "filesystem_path": str(disk_path),
            "available_bytes": int(disk.free),
            "sealed_input_bytes": input_bytes,
            "largest_sealed_input_bytes": largest_input_bytes,
            "existing_output_bytes": existing_output_bytes,
            "required_free_bytes": output_envelope_bytes,
            "sufficient": disk.free >= output_envelope_bytes,
            "estimate_policy": (
                "one output envelope derived from existing evidence and the "
                "largest sealed input; candle data remains read-only"
            ),
        },
        "storage_mode": "spooled",
        "passed": passed,
    }


def run_official_fallback(
    run_directory: str | Path,
    *,
    timeout_seconds: int | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Reuse a complete fallback or append one immutable official attempt."""

    root = Path(run_directory).resolve()
    source_run = _load_blocked_run(root)
    preflight = inspect_official_fallback_preflight(root)
    write_json(root / "official-fallback-preflight.json", preflight)
    if preflight["docker"].get("status") != "available":
        raise BenchmarkError(
            "official fallback requires an available Docker daemon: "
            f"{preflight['docker'].get('detail', 'unavailable')}"
        )
    if preflight["disk"].get("sufficient") is not True:
        disk = preflight["disk"]
        raise BenchmarkError(
            "official fallback disk preflight failed: "
            f"{disk['required_free_bytes']} bytes required, "
            f"{disk['available_bytes']} bytes available"
        )

    fallback_root = root / OFFICIAL_FALLBACK_DIRECTORY
    reusable = _find_reusable_fallback(fallback_root, source_run, root)
    if reusable is not None:
        from .selected_result import write_official_selection

        write_official_selection(root, reusable[1])
        return reusable[0], reusable[1], True

    attempt = _next_attempt_directory(fallback_root)
    from .research_reference import run_research_reference

    try:
        report = run_research_reference(
            root,
            attempt,
            timeout_seconds=timeout_seconds,
            purpose="fallback",
            reference_storage_mode="spooled",
        )
    except BenchmarkError as exc:
        attempt.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "1.4.0",
            "run_id": source_run["run_id"],
            "purpose": "fallback",
            "started_at": preflight["created_at"],
            "ended_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "exit_code": None,
            "timed_out": False,
            "complete": False,
            "exact_parity": None,
            "difference": None,
            "error": str(exc),
        }
        write_json(attempt / "run.json", report)
    report_path = attempt / "run.json"
    if report.get("complete") is True:
        from .selected_result import write_official_selection

        write_official_selection(root, report_path)
    return report, report_path, False


def record_official_completion(
    ledger_path: str | Path,
    run_directory: str | Path,
    report: Mapping[str, Any],
    report_path: str | Path,
) -> int:
    """Record official usability without upgrading Native verification."""

    root = Path(run_directory).resolve()
    run = _load_blocked_run(root)
    proof_path = Path(report_path).resolve()
    from .selected_result import validate_official_fallback

    validate_official_fallback(root, proof_path)
    evidence = [
        _evidence_record("native_blocked_run", root / "run.json"),
        _evidence_record("data_seal", root / "data-seal.json"),
        _evidence_record("selected_result", root / "selected-result.json"),
        _evidence_record("official_fallback", proof_path),
        _evidence_record(
            "official_trade_surface",
            proof_path.parent / "official-trade-surface.json",
        ),
    ]
    record = create_verification_record(
        subject_kind="run",
        subject_id=str(run["run_id"]),
        state="official_complete",
        outcome="success",
        fingerprint=_verification_fingerprint(root, run, reference=report),
        evidence=evidence,
        recorded_at=str(report["ended_at"]),
    )
    with VerificationLedger(ledger_path) as ledger:
        return ledger.append(record)


def record_native_blocker(
    ledger_path: str | Path,
    run_directory: str | Path,
) -> int:
    """Append the Native fail-closed outcome before any official selection."""

    root = Path(run_directory).resolve()
    run = _load_blocked_run(root)
    blockers = _mapping(run, "capability").get("blockers")
    first = (
        blockers[0]
        if isinstance(blockers, list) and blockers and isinstance(blockers[0], Mapping)
        else {}
    )
    record = create_verification_record(
        subject_kind="run",
        subject_id=str(run["run_id"]),
        state="blocked_unsupported_semantics",
        outcome="failure",
        fingerprint=_verification_fingerprint(root, run, reference=None),
        evidence=[
            _evidence_record("native_blocked_run", root / "run.json"),
            _evidence_record("data_seal", root / "data-seal.json"),
        ],
        failure={
            "code": str(first.get("code", "NATIVE_UNSUPPORTED_SEMANTICS")),
            "message": str(first.get("message", "Native semantics are unsupported")),
        },
        recorded_at=str(run["created_at"]),
    )
    with VerificationLedger(ledger_path) as ledger:
        return ledger.append(record)


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
    interactive: bool,
    include_breakdowns: bool,
    fallback_policy: str = "ask",
    fallback_timeout_seconds: int | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Complete consented post-run actions and print one final result view."""
    root = settings.output_directory.resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        if verification is True:
            raise BenchmarkError("the requested verification needs a durable run.json result")
        return native_status
    run = read_json(run_path)
    if not isinstance(run, dict):
        raise BenchmarkError(f"run report is not an object: {run_path}")
    complete = run.get("complete") is True and run.get("status") == "complete"
    ledger_path = settings.project_path.parent / "verification-ledger.sqlite"
    final_status = native_status

    if complete:
        from .result_report import write_result_presentation

        write_result_presentation(root)
        record_native_completion(ledger_path, root)
        verify_now = resolve_consent(
            verification,
            interactive=interactive,
            question="Run official Freqtrade verification? Docker replay is slower",
        )
        if verify_now:
            report, proof_path, reused = run_quick_official_verification(
                root,
                timeout_seconds=verification_timeout_seconds,
            )
            write_result_presentation(
                root,
                verification=report,
                verification_path=proof_path,
            )
            if report.get("complete") is True and report.get("exact_parity") is True:
                record_quick_verification(
                    ledger_path,
                    root,
                    report,
                    proof_path,
                )
                emit(
                    "  ✓  Official verification"
                    f"  ·  exact parity  ·  {'reused' if reused else 'new'} proof"
                )
            else:
                final_status = 1
                record_quick_failure(
                    ledger_path,
                    root,
                    report,
                    proof_path,
                )
                emit(f"  ✗  Official verification failed  ·  {proof_path}")
    elif verification is True:
        raise BenchmarkError("official quick verification requires a completed Native run")
    else:
        final_status = finish_official_fallback(
            root,
            ledger_path=ledger_path,
            native_status=native_status,
            fallback_policy=fallback_policy,
            timeout_seconds=fallback_timeout_seconds,
            interactive=interactive,
            registry_path=settings.registry_path,
            emit=emit,
        )

    summary_path = root / "summary.json"
    if summary_path.is_file():
        from .result_report import format_terminal_summary, load_result_summary

        emit("")
        emit(
            format_terminal_summary(
                load_result_summary(root),
                root,
                include_breakdowns=include_breakdowns,
            )
        )

    return final_status


def finish_official_fallback(
    run_directory: str | Path,
    *,
    ledger_path: str | Path,
    native_status: int,
    fallback_policy: str,
    timeout_seconds: int | None,
    interactive: bool,
    registry_path: str | Path | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Resolve explicit fallback policy for one durable blocked Native run."""

    if fallback_policy not in {"ask", "official", "disabled"}:
        raise BenchmarkError("fallback policy must be ask, official, or disabled")
    root = Path(run_directory).resolve()
    run_path = root / "run.json"
    if not run_path.is_file():
        return native_status
    run = read_json(run_path)
    if (
        not isinstance(run, Mapping)
        or run.get("status") != "blocked_unsupported_semantics"
        or run.get("complete") is not False
    ):
        return native_status
    blocker_sequence = record_native_blocker(ledger_path, root)
    capability = _mapping(run, "capability")
    blockers = capability.get("blockers")
    first_blocker = (
        blockers[0]
        if isinstance(blockers, list) and blockers and isinstance(blockers[0], Mapping)
        else {}
    )
    emit(
        "Native execution stopped safely: "
        f"{first_blocker.get('code', 'NATIVE_UNSUPPORTED_SEMANTICS')} — "
        f"{first_blocker.get('message', 'active strategy semantics are unsupported')}. "
        "No approximation was used."
    )
    emit(f"verification ledger: sequence={blocker_sequence}, state=blocked_unsupported_semantics")

    if fallback_policy == "official":
        fallback_now = True
    elif fallback_policy == "disabled":
        fallback_now = False
    else:
        fallback_now = resolve_consent(
            None,
            interactive=interactive,
            question=(
                "Native stopped on unsupported semantics. Run the pinned official "
                "Freqtrade fallback now"
            ),
        )
    if not fallback_now:
        emit(
            "official fallback: skipped; Native remains blocked "
            "(use --fallback official to run non-interactively)"
        )
        return 1

    emit(
        "official fallback: approved; checking preflight and reusable evidence before "
        "starting pinned Freqtrade. It may take much longer than Native. The Native "
        "run remains unchanged, and this official-only result does not claim parity."
    )
    report, proof_path, reused = run_official_fallback(
        root,
        timeout_seconds=timeout_seconds,
    )
    if report.get("complete") is not True:
        reason = (
            "timed out"
            if report.get("timed_out") is True
            else f"failed with exit code {report.get('exit_code')}"
        )
        emit(f"official fallback: {reason}; Native remains blocked -> {proof_path}")
        return 1

    from .result_report import write_result_presentation

    write_result_presentation(
        root,
        verification=report,
        verification_path=proof_path,
    )
    sequence = record_official_completion(
        ledger_path,
        root,
        report,
        proof_path,
    )
    if registry_path is not None:
        from .run_registry import RunRegistry

        with RunRegistry(registry_path) as registry:
            registry.record_selection(root)
    emit(
        "official fallback: completed "
        f"({'reused' if reused else 'new'} result), Native remains blocked, "
        f"ledger sequence={sequence}, state=official_complete -> {proof_path}"
    )
    return 0


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
        raise BenchmarkError(f"completed Native run failed its sealed identity: {identity_path}")
    identity_document = read_json(identity_path)
    inputs = document.get("inputs")
    if (
        not isinstance(identity_document, dict)
        or not isinstance(inputs, dict)
        or identity_document.get("identity") != inputs
        or identity_document.get("run_id") != document["run_id"]
        or _canonical_sha256(inputs) != document["run_id"]
    ):
        raise BenchmarkError(f"completed Native run failed its sealed identity: {identity_path}")
    surface_path = root / "trade-surface.json"
    result = _mapping(document, "result")
    surface_record = _mapping(result, "trade_surface")
    _validate_file_record(
        surface_record,
        expected_path=surface_path,
        label="completed Native run trade surface",
    )
    return document


def _load_blocked_run(root: Path) -> dict[str, Any]:
    run_path = root / "run.json"
    if not run_path.is_file():
        raise BenchmarkError(f"official fallback requires a durable Native run: {run_path}")
    document = read_json(run_path)
    capability = document.get("capability") if isinstance(document, Mapping) else None
    blockers = capability.get("blockers") if isinstance(capability, Mapping) else None
    if (
        not isinstance(document, dict)
        or document.get("complete") is not False
        or document.get("status") != "blocked_unsupported_semantics"
        or not isinstance(document.get("run_id"), str)
        or not isinstance(blockers, list)
        or not blockers
    ):
        raise BenchmarkError("official fallback requires unsupported Native semantics")
    identity_path = root / "identity.json"
    if not identity_path.is_file():
        raise BenchmarkError(f"blocked Native run failed its sealed identity: {identity_path}")
    identity_document = read_json(identity_path)
    inputs = document.get("inputs")
    if (
        not isinstance(identity_document, dict)
        or not isinstance(inputs, dict)
        or identity_document.get("identity") != inputs
        or identity_document.get("run_id") != document["run_id"]
        or _canonical_sha256(inputs) != document["run_id"]
    ):
        raise BenchmarkError(f"blocked Native run failed its sealed identity: {identity_path}")
    validate_data_seal(root / "data-seal.json")
    return document


def _find_reusable_fallback(
    fallback_root: Path,
    source_run: Mapping[str, Any],
    run_root: Path,
) -> tuple[dict[str, Any], Path] | None:
    if not fallback_root.exists():
        return None
    if not fallback_root.is_dir():
        raise BenchmarkError(f"official fallback path is not a directory: {fallback_root}")
    reusable = None
    from .selected_result import validate_official_fallback

    for attempt in sorted(fallback_root.glob("attempt-*")):
        if not attempt.is_dir():
            raise BenchmarkError(f"official fallback attempt is not a directory: {attempt}")
        report_path = attempt / "run.json"
        if not report_path.is_file():
            continue
        report = read_json(report_path)
        if not isinstance(report, dict):
            raise BenchmarkError(f"official fallback report is not an object: {report_path}")
        if report.get("complete") is not True:
            continue
        if report.get("run_id") != source_run.get("run_id"):
            raise BenchmarkError("official fallback belongs to a different run")
        validate_official_fallback(run_root, report_path)
        reusable = (report, report_path)
    return reusable


def _find_reusable_verification(
    verification_root: Path,
    source_run: Mapping[str, Any],
    run_root: Path,
) -> tuple[dict[str, Any], Path] | None:
    if not verification_root.exists():
        return None
    if not verification_root.is_dir():
        raise BenchmarkError(f"official verification path is not a directory: {verification_root}")
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
