#!/usr/bin/env python3
"""Record and validate identity-bound CI step timing artifacts."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeGuard

REPORT_SCHEMA_VERSION = "1.0.0"
AGGREGATE_SCHEMA_VERSION = "1.0.0"
COMPARISON_SCHEMA_VERSION = "1.0.0"
COMPLETED = "completed"
SKIPPED = "skipped"
TIMED_OUT = "timed_out"
INTERRUPTED = "interrupted"
FAILED = "failed"
VALID_STEP_STATUSES = {COMPLETED, SKIPPED, TIMED_OUT, INTERRUPTED, FAILED}
SHA256_LENGTH = 64


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"malformed timing JSON: {source}") from exc


def _write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _canonical_sha256(value: Mapping[str, Any] | Sequence[Any]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _worktree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError("unable to determine worktree state")
    return bool(completed.stdout)


def _finite_number(
    value: Any, *, positive: bool = False
) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def _is_sha(value: Any, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _timing_contract(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    timing = contract.get("timing")
    if not isinstance(timing, Mapping):
        raise ValueError("CI contract is missing timing policy")
    return timing


def _report_specs(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    reports = _timing_contract(contract).get("reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("CI contract timing reports must be a non-empty array")
    if any(not isinstance(report, Mapping) for report in reports):
        raise ValueError("CI contract timing report entry must be an object")
    return reports


def _report_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    identity = value.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("timing report identity must be an object")
    fields = ("job", "os", "python", "suite")
    if any(not isinstance(identity.get(field), str) for field in fields):
        raise ValueError("timing report identity fields must be strings")
    return tuple(str(identity[field]) for field in fields)  # type: ignore[return-value]


def _spec_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    fields = ("job", "os", "python", "suite")
    if any(not isinstance(value.get(field), str) for field in fields):
        raise ValueError("CI timing report specification identity is invalid")
    return tuple(str(value[field]) for field in fields)  # type: ignore[return-value]


def _spec_report_id(value: Mapping[str, Any]) -> str:
    return "-".join(_spec_key(value))


def _selected_report_specs(
    contract: Mapping[str, Any],
    expected_report_ids: Sequence[str] | None,
) -> list[Mapping[str, Any]]:
    specs = _report_specs(contract)
    report_ids = [_spec_report_id(spec) for spec in specs]
    if len(set(report_ids)) != len(report_ids):
        raise ValueError("CI timing report identities are duplicated")
    if expected_report_ids is None:
        return specs
    requested = list(expected_report_ids)
    if (
        not requested
        or any(not isinstance(report_id, str) or not report_id for report_id in requested)
        or len(set(requested)) != len(requested)
        or requested != [report_id for report_id in report_ids if report_id in requested]
    ):
        raise ValueError("selected timing report identities are invalid or unordered")
    unknown = sorted(set(requested) - set(report_ids))
    if unknown:
        raise ValueError(f"unknown selected timing reports: {unknown!r}")
    return [
        spec
        for spec, report_id in zip(specs, report_ids, strict=True)
        if report_id in requested
    ]


def _expected_build_identities(
    contract: Mapping[str, Any],
    *,
    candidate_commit: str,
    expected_report_ids: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    identities = []
    for spec in _selected_report_specs(contract, expected_report_ids):
        key = _spec_key(spec)
        identities.append(
            {
                "report_id": "-".join(key),
                "commit_sha": candidate_commit,
                "target": f"{key[1]}-{key[2]}",
            }
        )
    return sorted(identities, key=lambda value: value["report_id"])


def trusted_candidate_identity(
    contract: Mapping[str, Any],
    *,
    repository: str,
    workflow: str,
    workflow_ref: str,
    baseline_id: str,
    candidate_commit: str,
    cache_lock_sha256: str,
    expected_report_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Derive candidate/build roots from trusted contract and invocation inputs."""
    timing = _timing_contract(contract)
    if (
        repository != timing.get("repository")
        or workflow != timing.get("workflow_name")
        or workflow_ref != timing.get("workflow_ref")
        or baseline_id != timing.get("baseline_id")
        or not _is_sha(candidate_commit, length=40)
        or not _is_sha(cache_lock_sha256, length=SHA256_LENGTH)
    ):
        raise ValueError("trusted comparison invocation identity mismatch")
    artifact_inputs = {
        "baseline_id": baseline_id,
        "repository": repository,
        "workflow": workflow,
        "workflow_ref": workflow_ref,
        "candidate_commit": candidate_commit,
        "cache_lock_sha256": cache_lock_sha256,
        "rust_compiler_cache": timing["rust_compiler_cache"],
    }
    build_identities = _expected_build_identities(
        contract,
        candidate_commit=candidate_commit,
        expected_report_ids=expected_report_ids,
    )
    return {
        "commit_sha": candidate_commit,
        "artifact_identity_root": _canonical_sha256(artifact_inputs),
        "build_identity_root": _canonical_sha256(build_identities),
        "build_identities": build_identities,
    }


def _step_names(spec: Mapping[str, Any]) -> list[str]:
    steps = spec.get("required_steps")
    if (
        not isinstance(steps, list)
        or not steps
        or any(not isinstance(step, str) or not step for step in steps)
        or len(set(steps)) != len(steps)
    ):
        raise ValueError("CI timing required_steps must be unique non-empty strings")
    return steps


def initialize_report(
    *,
    output: str | Path,
    contract: Mapping[str, Any],
    job: str,
    os_name: str,
    python_version: str,
    suite: str,
    workflow: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    commit_sha: str,
    cache_file: str | Path,
    source_dirty: bool,
) -> dict[str, Any]:
    """Initialize all expected steps as skipped before any measured command runs."""
    timing = _timing_contract(contract)
    key = (job, os_name, python_version, suite)
    matching = [spec for spec in _report_specs(contract) if _spec_key(spec) == key]
    if len(matching) != 1:
        raise ValueError(f"unknown CI timing report identity: {key!r}")
    if not _is_sha(commit_sha, length=40):
        raise ValueError("commit SHA must be 40 lowercase hexadecimal characters")
    lock_sha = _sha256(cache_file)
    spec = matching[0]
    timeout_seconds = spec.get("step_timeout_seconds")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise ValueError("CI timing step timeout must be an integer")
    report_id = "-".join(key)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "baseline_id": timing["baseline_id"],
        "identity": {
            "report_id": report_id,
            "workflow": workflow,
            "workflow_ref": timing["workflow_ref"],
            "repository": repository,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "commit_sha": commit_sha,
            "job": job,
            "os": os_name,
            "python": python_version,
            "suite": suite,
            "cache": {
                "key": f"uv-lock-{lock_sha}",
                "lock_sha256": lock_sha,
                "rust_compiler_cache": timing["rust_compiler_cache"],
            },
            "build": {
                "commit_sha": commit_sha,
                "target": f"{os_name}-{python_version}",
            },
            "source_dirty": source_dirty,
        },
        "steps": [
            {
                "name": name,
                "category": timing["step_categories"][name],
                "status": SKIPPED,
                "duration_seconds": 0.0,
                "timeout_seconds": timeout_seconds,
                "exit_code": None,
                "started_at": None,
                "completed_at": None,
            }
            for name in _step_names(spec)
        ],
    }
    _write_json(output, report)
    return report


class WindowsJobApi(Protocol):
    def create_kill_on_close_job(self) -> object: ...

    def create_suspended(self, command: list[str], cwd: Path | None) -> object: ...

    def assign(self, job: object, process: object) -> None: ...

    def resume(self, process: object) -> None: ...

    def wait_root(self, process: object, timeout_seconds: int) -> int | None: ...

    def terminate_job(self, job: object, exit_code: int) -> None: ...

    def wait_job_empty(self, job: object) -> None: ...

    def terminate_process(self, process: object, exit_code: int) -> None: ...

    def close(self, job: object, process: object | None) -> None: ...


class WindowsJobLifecycle:
    """Run a suspended root inside a kill-on-close Job Object."""

    def __init__(self, api: WindowsJobApi) -> None:
        self._api = api

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None,
        timeout_seconds: int,
    ) -> tuple[int, str]:
        job = self._api.create_kill_on_close_job()
        process: object | None = None
        assigned = False
        try:
            process = self._api.create_suspended(
                list(command),
                None if cwd is None else Path(cwd),
            )
            self._api.assign(job, process)
            assigned = True
            self._api.resume(process)
            exit_code = self._api.wait_root(process, timeout_seconds)
            final_code = 124 if exit_code is None else exit_code
            self._api.terminate_job(job, final_code)
            self._api.wait_job_empty(job)
            return (
                (124, TIMED_OUT)
                if exit_code is None
                else (exit_code, "completed")
            )
        except BaseException:
            if process is not None:
                if assigned:
                    self._api.terminate_job(job, 130)
                    self._api.wait_job_empty(job)
                else:
                    self._api.terminate_process(process, 130)
            raise
        finally:
            self._api.close(job, process)


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _AssociateCompletionPort(ctypes.Structure):
    _fields_ = [
        ("CompletionKey", ctypes.c_void_p),
        ("CompletionPort", ctypes.c_void_p),
    ]


class _WindowsJob:
    def __init__(self, handle: int, completion_port: int) -> None:
        self.handle = handle
        self.completion_port = completion_port


class _WindowsProcess:
    def __init__(self, process_handle: int, thread_handle: int) -> None:
        self.process_handle = process_handle
        self.thread_handle = thread_handle


class CtypesWindowsJobApi:
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
    JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    def __init__(self, *, kernel32: Any | None = None) -> None:
        if kernel32 is not None:
            self._kernel32 = kernel32
            return
        loader: Any = ctypes.WinDLL
        self._kernel32: Any = loader("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.CreateIoCompletionPort.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_ulong,
        ]
        self._kernel32.CreateIoCompletionPort.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self._kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        self._kernel32.ResumeThread.restype = ctypes.c_ulong
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        self._kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._kernel32.GetQueuedCompletionStatus.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
        ]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def _check(self, succeeded: Any, operation: str) -> None:
        if not succeeded:
            get_last_error: Any = getattr(ctypes, "get_last_error", lambda: 0)
            raise OSError(get_last_error(), operation)

    def create_kill_on_close_job(self) -> object:
        with contextlib.ExitStack() as ownership:
            handle = self._kernel32.CreateJobObjectW(None, None)
            self._check(handle, "CreateJobObjectW failed")
            ownership.callback(self._kernel32.CloseHandle, handle)
            completion_port = self._kernel32.CreateIoCompletionPort(
                ctypes.c_void_p(-1), None, 0, 1
            )
            self._check(completion_port, "CreateIoCompletionPort failed")
            ownership.callback(self._kernel32.CloseHandle, completion_port)
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            self._check(
                self._kernel32.SetInformationJobObject(
                    handle,
                    self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                ),
                "SetInformationJobObject kill-on-close failed",
            )
            association = _AssociateCompletionPort(
                ctypes.c_void_p(handle), ctypes.c_void_p(completion_port)
            )
            self._check(
                self._kernel32.SetInformationJobObject(
                    handle,
                    self.JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION,
                    ctypes.byref(association),
                    ctypes.sizeof(association),
                ),
                "SetInformationJobObject completion-port failed",
            )
            job = _WindowsJob(int(handle), int(completion_port))
            ownership.pop_all()
            return job

    def create_suspended(self, command: list[str], cwd: Path | None) -> object:
        startup = _StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        information = _ProcessInformation()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        self._check(
            self._kernel32.CreateProcessW(
                None,
                command_line,
                None,
                None,
                False,
                self.CREATE_SUSPENDED | self.CREATE_NEW_PROCESS_GROUP,
                None,
                None if cwd is None else str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            ),
            "CreateProcessW suspended failed",
        )
        with contextlib.ExitStack() as ownership:
            ownership.callback(self._kernel32.CloseHandle, information.hProcess)
            ownership.callback(self._kernel32.CloseHandle, information.hThread)
            process = _WindowsProcess(
                int(information.hProcess),
                int(information.hThread),
            )
            ownership.pop_all()
            return process

    def assign(self, job: object, process: object) -> None:
        assert isinstance(job, _WindowsJob)
        assert isinstance(process, _WindowsProcess)
        self._check(
            self._kernel32.AssignProcessToJobObject(
                job.handle, process.process_handle
            ),
            "AssignProcessToJobObject failed",
        )

    def resume(self, process: object) -> None:
        assert isinstance(process, _WindowsProcess)
        result = self._kernel32.ResumeThread(process.thread_handle)
        if result == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "ResumeThread failed")

    def wait_root(self, process: object, timeout_seconds: int) -> int | None:
        assert isinstance(process, _WindowsProcess)
        result = self._kernel32.WaitForSingleObject(
            process.process_handle, timeout_seconds * 1000
        )
        if result == self.WAIT_TIMEOUT:
            return None
        exit_code = ctypes.c_ulong()
        self._check(
            self._kernel32.GetExitCodeProcess(
                process.process_handle, ctypes.byref(exit_code)
            ),
            "GetExitCodeProcess failed",
        )
        return int(exit_code.value)

    def terminate_job(self, job: object, exit_code: int) -> None:
        assert isinstance(job, _WindowsJob)
        self._check(
            self._kernel32.TerminateJobObject(job.handle, exit_code),
            "TerminateJobObject failed",
        )

    def wait_job_empty(self, job: object) -> None:
        assert isinstance(job, _WindowsJob)
        while True:
            message = ctypes.c_ulong()
            key = ctypes.c_size_t()
            overlapped = ctypes.c_void_p()
            self._check(
                self._kernel32.GetQueuedCompletionStatus(
                    job.completion_port,
                    ctypes.byref(message),
                    ctypes.byref(key),
                    ctypes.byref(overlapped),
                    30_000,
                ),
                "waiting for Windows Job Object cleanup failed",
            )
            if message.value == self.JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO:
                return

    def terminate_process(self, process: object, exit_code: int) -> None:
        assert isinstance(process, _WindowsProcess)
        self._check(
            self._kernel32.TerminateProcess(process.process_handle, exit_code),
            "TerminateProcess failed",
        )
        self._kernel32.WaitForSingleObject(process.process_handle, 30_000)

    def close(self, job: object, process: object | None) -> None:
        assert isinstance(job, _WindowsJob)
        if isinstance(process, _WindowsProcess):
            self._kernel32.CloseHandle(process.thread_handle)
            self._kernel32.CloseHandle(process.process_handle)
        self._kernel32.CloseHandle(job.completion_port)
        self._kernel32.CloseHandle(job.handle)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    running = process.poll() is None
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if running:
        process.wait()


def _run_bounded_command(
    command: Sequence[str], *, cwd: str | Path | None, timeout_seconds: int
) -> tuple[int, str]:
    if os.name == "nt":
        return WindowsJobLifecycle(CtypesWindowsJobApi()).run(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout_seconds), "completed"
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        return 124, TIMED_OUT
    except BaseException:
        _terminate_process_tree(process)
        raise


def run_step(
    *,
    report_path: str | Path,
    step_name: str,
    timeout_seconds: int,
    command: Sequence[str],
    cwd: str | Path | None = None,
) -> int:
    """Run one bounded command and persist its outcome before returning its status."""
    if timeout_seconds <= 0:
        raise ValueError("step timeout must be positive")
    if not command:
        raise ValueError("timed command must not be empty")
    value = _read_json(report_path)
    if not isinstance(value, dict):
        raise ValueError("timing report must be an object")
    steps = value.get("steps")
    if not isinstance(steps, list):
        raise ValueError("timing report steps must be an array")
    matching = [step for step in steps if isinstance(step, dict) and step.get("name") == step_name]
    if len(matching) != 1:
        raise ValueError(f"timing step is not declared exactly once: {step_name}")
    step = matching[0]
    declared_timeout = step.get("timeout_seconds")
    if not isinstance(declared_timeout, int) or timeout_seconds > declared_timeout:
        raise ValueError(
            f"step timeout {timeout_seconds} exceeds contract limit {declared_timeout}"
        )
    started_at = _utc_now()
    started = time.monotonic_ns()
    interrupted: BaseException | None = None
    try:
        exit_code, execution_state = _run_bounded_command(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        status = (
            TIMED_OUT
            if execution_state == TIMED_OUT
            else COMPLETED if exit_code == 0 else FAILED
        )
    except BaseException as exc:
        interrupted = exc
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        status = INTERRUPTED if isinstance(exc, KeyboardInterrupt) else FAILED
    duration = (time.monotonic_ns() - started) / 1_000_000_000
    step.update(
        {
            "status": status,
            "duration_seconds": round(duration, 6),
            "timeout_seconds": timeout_seconds,
            "exit_code": exit_code,
            "started_at": started_at,
            "completed_at": _utc_now(),
        }
    )
    _write_json(report_path, value)
    if interrupted is not None:
        raise interrupted
    return exit_code


def _validate_identity(
    report: Mapping[str, Any],
    *,
    timing: Mapping[str, Any],
    expected_run_id: str,
    expected_run_attempt: str,
    expected_commit_sha: str,
    expected_lock_sha256: str | None,
) -> Mapping[str, Any]:
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("timing report schema version mismatch")
    if report.get("baseline_id") != timing.get("baseline_id"):
        raise ValueError("timing baseline identity mismatch")
    identity = report.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("timing report identity must be an object")
    report_key = _report_key(report)
    if identity.get("report_id") != "-".join(report_key):
        raise ValueError("report identity mismatch")
    if identity.get("workflow") != timing.get("workflow_name"):
        raise ValueError("workflow identity mismatch")
    if identity.get("workflow_ref") != timing.get("workflow_ref"):
        raise ValueError("workflow reference identity mismatch")
    if identity.get("repository") != timing.get("repository"):
        raise ValueError("repository identity mismatch")
    if identity.get("run_id") != expected_run_id:
        raise ValueError("run identity mismatch")
    if identity.get("run_attempt") != expected_run_attempt:
        raise ValueError("run attempt identity mismatch")
    if identity.get("commit_sha") != expected_commit_sha:
        raise ValueError("commit identity mismatch")
    if identity.get("source_dirty") is not False:
        raise ValueError("dirty worktree timing evidence is not admissible")
    cache = identity.get("cache")
    if not isinstance(cache, Mapping):
        raise ValueError("cache identity must be an object")
    lock_sha = cache.get("lock_sha256")
    if not _is_sha(lock_sha, length=SHA256_LENGTH):
        raise ValueError("cache lock identity is invalid")
    if cache.get("key") != f"uv-lock-{lock_sha}":
        raise ValueError("stale cache identity")
    if expected_lock_sha256 is not None and lock_sha != expected_lock_sha256:
        raise ValueError("stale cache identity")
    if cache.get("rust_compiler_cache") != timing.get("rust_compiler_cache"):
        raise ValueError("Rust compiler cache identity mismatch")
    build = identity.get("build")
    if not isinstance(build, Mapping):
        raise ValueError("build identity must be an object")
    if build.get("commit_sha") != expected_commit_sha:
        raise ValueError("stale build identity")
    expected_target = f"{identity.get('os')}-{identity.get('python')}"
    if build.get("target") != expected_target:
        raise ValueError("stale build identity")
    return identity


def validate_timing_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any],
    expected_run_id: str,
    expected_commit_sha: str,
    expected_run_attempt: str = "1",
    expected_lock_sha256: str | None = None,
    expected_report_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate exact job/OS/step coverage and return a flattened aggregate."""
    timing = _timing_contract(contract)
    specs = _selected_report_specs(contract, expected_report_ids)
    expected = {_spec_key(spec): spec for spec in specs}
    observed: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for report in reports:
        key = _report_key(report)
        if key in observed:
            raise ValueError(f"duplicate timing report: {key!r}")
        observed[key] = report
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    if missing:
        raise ValueError(f"missing timing reports: {missing!r}")
    if unexpected:
        raise ValueError(f"unexpected timing reports: {unexpected!r}")

    flattened: list[dict[str, Any]] = []
    validated_identities: list[Mapping[str, Any]] = []
    for key in sorted(expected):
        report = observed[key]
        identity = _validate_identity(
            report,
            timing=timing,
            expected_run_id=expected_run_id,
            expected_run_attempt=expected_run_attempt,
            expected_commit_sha=expected_commit_sha,
            expected_lock_sha256=expected_lock_sha256,
        )
        validated_identities.append(identity)
        expected_names = _step_names(expected[key])
        steps = report.get("steps")
        if not isinstance(steps, list) or any(not isinstance(step, Mapping) for step in steps):
            raise ValueError(f"timing steps must be objects for {key!r}")
        names = [str(step.get("name")) for step in steps]
        missing_steps = [name for name in expected_names if name not in names]
        if missing_steps:
            raise ValueError(
                f"missing required timing steps for {key!r}: {missing_steps!r}"
            )
        if names != expected_names:
            raise ValueError(f"timing step ordering or membership mismatch for {key!r}")
        limit = expected[key].get("step_timeout_seconds")
        for step in steps:
            name = step.get("name")
            if step.get("category") != timing["step_categories"].get(name):
                raise ValueError(f"timing step category mismatch: {name}")
            if step.get("status") not in VALID_STEP_STATUSES:
                raise ValueError(f"invalid timing step status: {name}")
            duration = step.get("duration_seconds")
            if not _finite_number(duration):
                raise ValueError(f"invalid timing step duration: {name}")
            timeout = step.get("timeout_seconds")
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or timeout <= 0
                or not isinstance(limit, int)
                or timeout > limit
            ):
                raise ValueError(f"invalid timing step timeout: {name}")
            if step.get("status") != COMPLETED or step.get("exit_code") != 0:
                raise ValueError(
                    f"required timing step not completed: {key!r} / {name} "
                    f"({step.get('status')})"
                )
            flattened.append(
                {
                    "report_id": identity.get("report_id", "-".join(key)),
                    "job": key[0],
                    "os": key[1],
                    "python": key[2],
                    "suite": key[3],
                    "name": name,
                    "category": step["category"],
                    "status": step["status"],
                    "duration_seconds": duration,
                    "timeout_seconds": timeout,
                }
            )

    lock_shas = {
        str(identity["cache"]["lock_sha256"])
        for identity in validated_identities
    }
    if len(lock_shas) != 1:
        raise ValueError("cache identity mismatch across timing reports")
    build_identities = sorted(
        (
            {
                "report_id": str(identity["report_id"]),
                "commit_sha": str(identity["build"]["commit_sha"]),
                "target": str(identity["build"]["target"]),
            }
            for identity in validated_identities
        ),
        key=lambda value: value["report_id"],
    )
    expected_build_identities = _expected_build_identities(
        contract,
        candidate_commit=expected_commit_sha,
        expected_report_ids=expected_report_ids,
    )
    if build_identities != expected_build_identities:
        raise ValueError("build identities do not match trusted contract inputs")
    candidate_identity = trusted_candidate_identity(
        contract,
        repository=str(timing["repository"]),
        workflow=str(timing["workflow_name"]),
        workflow_ref=str(timing["workflow_ref"]),
        baseline_id=str(timing["baseline_id"]),
        candidate_commit=expected_commit_sha,
        cache_lock_sha256=next(iter(lock_shas)),
        expected_report_ids=expected_report_ids,
    )
    category_totals: dict[str, float] = {}
    for step in flattened:
        category = str(step["category"])
        category_totals[category] = round(
            category_totals.get(category, 0.0) + float(step["duration_seconds"]),
            6,
        )
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "baseline_id": timing["baseline_id"],
        "identity": {
            "workflow": timing["workflow_name"],
            "workflow_ref": timing["workflow_ref"],
            "repository": timing["repository"],
            "run_id": expected_run_id,
            "run_attempt": expected_run_attempt,
            "commit_sha": expected_commit_sha,
            "cache_lock_sha256": next(iter(lock_shas)),
            "rust_compiler_cache": timing["rust_compiler_cache"],
            "candidate": candidate_identity,
        },
        "passed": True,
        "report_count": len(reports),
        "step_count": len(flattened),
        "category_duration_seconds": dict(sorted(category_totals.items())),
        "steps": flattened,
    }


def validate_pytest_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    timing_reports: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate actionable nodeid/resource evidence for every full Python job."""
    timing = _timing_contract(contract)
    expected_sources = {
        str(report["identity"]["report_id"]): report["identity"]
        for report in timing_reports
        if report["identity"]["job"] == "python"
        and report["identity"]["suite"] == "full"
    }
    observed: dict[str, Mapping[str, Any]] = {}
    all_tests: list[dict[str, Any]] = []
    slowest_count = timing.get("pytest_slowest_count")
    owners = timing.get("pytest_ownership_groups")
    if not isinstance(slowest_count, int) or slowest_count <= 0:
        raise ValueError("pytest slowest-test policy is invalid")
    if not isinstance(owners, list) or any(not isinstance(owner, str) for owner in owners):
        raise ValueError("pytest ownership policy is invalid")
    for report in reports:
        if (
            report.get("schema_version") != "1.0.0"
            or report.get("kind") != "pytest-test-timing"
        ):
            raise ValueError("pytest timing report schema mismatch")
        identity = report.get("identity")
        if not isinstance(identity, Mapping):
            raise ValueError("pytest timing identity must be an object")
        report_id = identity.get("report_id")
        if not isinstance(report_id, str) or report_id in observed:
            raise ValueError("pytest timing report identity is invalid or duplicated")
        observed[report_id] = report
        source = expected_sources.get(report_id)
        if source is None:
            raise ValueError(f"unexpected pytest timing report: {report_id}")
        for field in (
            "workflow",
            "workflow_ref",
            "repository",
            "run_id",
            "run_attempt",
            "commit_sha",
            "os",
            "python",
            "suite",
            "cache",
            "build",
        ):
            if identity.get(field) != source.get(field):
                raise ValueError(f"pytest timing identity mismatch: {report_id} / {field}")
        tests = report.get("tests")
        if not isinstance(tests, list) or not tests:
            raise ValueError(f"pytest timing report has no tests: {report_id}")
        nodeids: list[str] = []
        for record in tests:
            if not isinstance(record, Mapping):
                raise ValueError("pytest timing test record must be an object")
            nodeid = record.get("nodeid")
            test_id = record.get("test_id")
            duration = record.get("duration_seconds")
            resources = record.get("resources")
            if not _finite_number(duration) or (
                isinstance(resources, Mapping)
                and not _finite_number(resources.get("cpu_seconds"))
            ):
                raise ValueError(
                    f"non-finite pytest timing metric: {report_id} / {nodeid}"
                )
            if (
                not isinstance(nodeid, str)
                or not nodeid
                or test_id != hashlib.sha256(nodeid.encode("utf-8")).hexdigest()
                or record.get("owner") not in owners
                or record.get("outcome")
                not in {"passed", "failed", "skipped", "xfailed", "xpassed", "error"}
                or not isinstance(resources, Mapping)
                or not isinstance(resources.get("peak_rss_bytes"), int)
                or isinstance(resources.get("peak_rss_bytes"), bool)
                or resources["peak_rss_bytes"] <= 0
            ):
                raise ValueError(f"invalid actionable pytest timing record: {report_id}")
            nodeids.append(nodeid)
            all_tests.append(
                {
                    "report_id": report_id,
                    "os": identity["os"],
                    "python": identity["python"],
                    **dict(record),
                }
            )
        if nodeids != sorted(nodeids) or len(nodeids) != len(set(nodeids)):
            raise ValueError(f"pytest timing ordering or nodeid duplication: {report_id}")
        expected_slowest = sorted(
            tests,
            key=lambda record: (
                -float(record["duration_seconds"]),
                str(record["nodeid"]),
            ),
        )[:slowest_count]
        canonical_slowest = [
            {
                "nodeid": record["nodeid"],
                "test_id": record["test_id"],
                "owner": record["owner"],
                "duration_seconds": record["duration_seconds"],
                "resources": record["resources"],
            }
            for record in expected_slowest
        ]
        if report.get("slowest_tests") != canonical_slowest:
            raise ValueError(f"pytest slowest-test evidence mismatch: {report_id}")
        expected_outcomes: dict[str, int] = {}
        for record in tests:
            outcome = str(record["outcome"])
            expected_outcomes[outcome] = expected_outcomes.get(outcome, 0) + 1
        expected_resources = {
            "cpu_seconds": round(
                sum(float(record["resources"]["cpu_seconds"]) for record in tests),
                6,
            ),
            "peak_rss_bytes": max(
                int(record["resources"]["peak_rss_bytes"]) for record in tests
            ),
        }
        supplied_resources = report.get("resources")
        if isinstance(supplied_resources, Mapping) and (
            not _finite_number(supplied_resources.get("cpu_seconds"))
            or not _finite_number(
                supplied_resources.get("peak_rss_bytes"), positive=True
            )
        ):
            raise ValueError(f"non-finite pytest timing metric: {report_id} / summary")
        if (
            report.get("test_count") != len(tests)
            or report.get("outcomes") != dict(sorted(expected_outcomes.items()))
            or supplied_resources != expected_resources
        ):
            raise ValueError(f"pytest derived summary mismatch: {report_id}")
    missing = sorted(set(expected_sources) - set(observed))
    if missing:
        raise ValueError(f"missing pytest timing reports: {missing!r}")
    ordered_tests = sorted(
        all_tests,
        key=lambda record: (
            str(record["report_id"]),
            str(record["nodeid"]),
        ),
    )
    slowest_tests = sorted(
        all_tests,
        key=lambda record: (
            -float(record["duration_seconds"]),
            str(record["report_id"]),
            str(record["nodeid"]),
        ),
    )[:slowest_count]
    return {
        "report_count": len(reports),
        "test_count": len(ordered_tests),
        "tests": ordered_tests,
        "slowest_tests": slowest_tests,
    }


def compare_three_runs(
    reports: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any],
    expected_repository: str,
    expected_workflow: str,
    expected_workflow_ref: str,
    expected_run_attempt: str,
    expected_commit_sha: str,
    expected_cache_sha256: str,
    expected_candidate_commit: str,
    expected_candidate_artifact_root: str,
    expected_build_identity_root: str,
    expected_baseline_id: str,
) -> dict[str, Any]:
    """Validate three aggregates against trusted inputs, then compare timings."""
    if len(reports) != 3:
        raise ValueError("three-run comparison requires exactly three aggregate reports")
    timing = _timing_contract(contract)
    specs = _report_specs(contract)
    expected_steps = {
        (*_spec_key(spec), step)
        for spec in specs
        for step in _step_names(spec)
    }
    trusted_candidate = trusted_candidate_identity(
        contract,
        repository=expected_repository,
        workflow=expected_workflow,
        workflow_ref=expected_workflow_ref,
        baseline_id=expected_baseline_id,
        candidate_commit=expected_candidate_commit,
        cache_lock_sha256=expected_cache_sha256,
    )
    if (
        expected_candidate_commit != expected_commit_sha
        or trusted_candidate["artifact_identity_root"]
        != expected_candidate_artifact_root
        or trusted_candidate["build_identity_root"]
        != expected_build_identity_root
    ):
        raise ValueError("trusted comparison invocation identity mismatch")
    trusted_identity = {
        "repository": expected_repository,
        "workflow": expected_workflow,
        "workflow_ref": expected_workflow_ref,
        "rust_compiler_cache": timing["rust_compiler_cache"],
        "run_attempt": expected_run_attempt,
        "cache_lock_sha256": expected_cache_sha256,
        "candidate": trusted_candidate,
        "commit_sha": expected_commit_sha,
    }
    identities: list[Mapping[str, Any]] = []
    for report in reports:
        if report.get("schema_version") != AGGREGATE_SCHEMA_VERSION:
            raise ValueError("comparison input schema mismatch")
        if (
            report.get("passed") is not True
            or report.get("report_count") != len(specs)
            or report.get("step_count") != len(expected_steps)
        ):
            raise ValueError("comparison input is not a complete successful aggregate")
        identity = report.get("identity")
        if not isinstance(identity, Mapping):
            raise ValueError("comparison input identity is invalid")
        if identity.get("run_attempt") != expected_run_attempt:
            raise ValueError("run attempt identity mismatch")
        observed_identity = {
            field: identity.get(field)
            for field in trusted_identity
        }
        if (
            report.get("baseline_id") != expected_baseline_id
            or observed_identity != trusted_identity
        ):
            raise ValueError("trusted comparison identity mismatch")
        identities.append(identity)
    run_ids = [identity.get("run_id") for identity in identities]
    if any(not isinstance(run_id, str) for run_id in run_ids) or len(set(run_ids)) != 3:
        raise ValueError("three-run comparison requires three distinct run identities")
    step_maps: list[dict[tuple[str, str, str, str, str], float]] = []
    for report in reports:
        steps = report.get("steps")
        if not isinstance(steps, list):
            raise ValueError("comparison steps must be an array")
        current: dict[tuple[str, str, str, str, str], float] = {}
        for step in steps:
            if not isinstance(step, Mapping):
                raise ValueError("comparison step must be an object")
            key = (
                str(step.get("job")),
                str(step.get("os")),
                str(step.get("python")),
                str(step.get("suite")),
                str(step.get("name")),
            )
            duration = step.get("duration_seconds")
            if (
                key in current
                or key not in expected_steps
                or step.get("status") != COMPLETED
                or not _finite_number(duration)
            ):
                raise ValueError("comparison step identity, state, or duration is invalid")
            current[key] = float(duration)
        if set(current) != expected_steps:
            raise ValueError("comparison input has incomplete timing step coverage")
        step_maps.append(current)
    if not all(set(step_maps[0]) == set(current) for current in step_maps[1:]):
        raise ValueError("three-run comparison step coverage mismatch")
    comparisons = []
    for key in sorted(step_maps[0]):
        durations = [current[key] for current in step_maps]
        comparisons.append(
            {
                "job": key[0],
                "os": key[1],
                "python": key[2],
                "suite": key[3],
                "name": key[4],
                "duration_seconds": durations,
                "min_seconds": min(durations),
                "median_seconds": statistics.median(durations),
                "max_seconds": max(durations),
            }
        )
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "baseline_id": reports[0]["baseline_id"],
        "identity": trusted_identity,
        "commit_sha": identities[0]["commit_sha"],
        "run_ids": run_ids,
        "steps": comparisons,
    }


def _load_contract(path: str | Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError("CI contract must be an object")
    return value


def _validation_plan_report_ids(
    raw: str,
    contract: Mapping[str, Any],
) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("validation plan must be valid JSON") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "affected-validation-plan-v1"
        or value.get("mode") not in {"affected", "full"}
    ):
        raise ValueError("validation plan identity is invalid")
    selected_jobs = value.get("selected_jobs")
    report_ids = value.get("timing_reports")
    if (
        not isinstance(selected_jobs, list)
        or any(not isinstance(job, str) for job in selected_jobs)
        or len(set(selected_jobs)) != len(selected_jobs)
        or "timing" not in selected_jobs
        or not isinstance(report_ids, list)
    ):
        raise ValueError("validation plan timing selection is invalid")
    specs = _selected_report_specs(contract, report_ids)
    if any(spec["job"] not in selected_jobs for spec in specs):
        raise ValueError("validation plan selects timing evidence for an unselected job")
    if value["mode"] == "full" and len(specs) != len(_report_specs(contract)):
        raise ValueError("full validation plan must select every timing report")
    return list(report_ids)


def _load_reports(
    directory: str | Path, *, pattern: str
) -> list[Mapping[str, Any]]:
    root = Path(directory)
    paths = sorted(path for path in root.rglob(pattern) if path.is_file())
    if not paths:
        raise ValueError(f"no timing JSON reports found: {root}")
    reports: list[Mapping[str, Any]] = []
    for path in paths:
        value = _read_json(path)
        if not isinstance(value, Mapping):
            raise ValueError(f"timing report must be an object: {path}")
        reports.append(value)
    return reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=Path(".github/ci-contract.json"))
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init")
    initialize.add_argument("--output", type=Path, required=True)
    initialize.add_argument("--job", required=True)
    initialize.add_argument("--os", dest="os_name", required=True)
    initialize.add_argument("--python", dest="python_version", required=True)
    initialize.add_argument("--suite", required=True)
    initialize.add_argument("--workflow", required=True)
    initialize.add_argument("--repository", required=True)
    initialize.add_argument("--run-id", required=True)
    initialize.add_argument("--run-attempt", required=True)
    initialize.add_argument("--commit-sha", required=True)
    initialize.add_argument("--cache-file", type=Path, required=True)
    initialize.add_argument("--source-dirty", choices=("true", "false"))

    run = commands.add_parser("run-step")
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--step", required=True)
    run.add_argument("--timeout-seconds", type=int, required=True)
    run.add_argument("--cwd", type=Path)
    run.add_argument("timed_command", nargs=argparse.REMAINDER)

    validate = commands.add_parser("validate")
    validate.add_argument("--reports", type=Path, required=True)
    validate.add_argument("--expected-repository", required=True)
    validate.add_argument("--expected-workflow", required=True)
    validate.add_argument("--expected-workflow-ref", required=True)
    validate.add_argument("--expected-baseline-id", required=True)
    validate.add_argument("--expected-run-id", required=True)
    validate.add_argument("--expected-run-attempt", required=True)
    validate.add_argument("--expected-commit-sha", required=True)
    validate.add_argument("--expected-candidate-commit", required=True)
    validate.add_argument("--cache-file", type=Path, required=True)
    validate.add_argument("--expected-cache-sha256", required=True)
    validate.add_argument("--validation-plan-json")
    validate.add_argument("--output", type=Path, required=True)

    compare = commands.add_parser("compare-three")
    compare.add_argument("--report", type=Path, action="append", required=True)
    compare.add_argument("--expected-repository", required=True)
    compare.add_argument("--expected-workflow", required=True)
    compare.add_argument("--expected-workflow-ref", required=True)
    compare.add_argument("--expected-run-attempt", required=True)
    compare.add_argument("--expected-commit-sha", required=True)
    compare.add_argument("--cache-file", type=Path, required=True)
    compare.add_argument("--expected-cache-sha256", required=True)
    compare.add_argument("--expected-candidate-commit", required=True)
    compare.add_argument("--expected-candidate-artifact-root", required=True)
    compare.add_argument("--expected-build-identity-root", required=True)
    compare.add_argument("--expected-baseline-id", required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = _load_contract(args.contract)
        if args.command == "init":
            report = initialize_report(
                output=args.output,
                contract=contract,
                job=args.job,
                os_name=args.os_name,
                python_version=args.python_version,
                suite=args.suite,
                workflow=args.workflow,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                commit_sha=args.commit_sha,
                cache_file=args.cache_file,
                source_dirty=(
                    _worktree_dirty()
                    if args.source_dirty is None
                    else args.source_dirty == "true"
                ),
            )
            print(json.dumps({"report_id": report["identity"]["report_id"]}, sort_keys=True))
            return 0
        if args.command == "run-step":
            command = list(args.timed_command)
            if command[:1] == ["--"]:
                command = command[1:]
            return run_step(
                report_path=args.report,
                step_name=args.step,
                timeout_seconds=args.timeout_seconds,
                command=command,
                cwd=args.cwd,
            )
        if args.command == "validate":
            lock_sha = _sha256(args.cache_file)
            if lock_sha != args.expected_cache_sha256:
                raise ValueError("trusted cache file identity mismatch")
            expected_report_ids = (
                None
                if args.validation_plan_json is None
                else _validation_plan_report_ids(args.validation_plan_json, contract)
            )
            trusted_candidate_identity(
                contract,
                repository=args.expected_repository,
                workflow=args.expected_workflow,
                workflow_ref=args.expected_workflow_ref,
                baseline_id=args.expected_baseline_id,
                candidate_commit=args.expected_candidate_commit,
                cache_lock_sha256=lock_sha,
                expected_report_ids=expected_report_ids,
            )
            if args.expected_candidate_commit != args.expected_commit_sha:
                raise ValueError("trusted candidate commit identity mismatch")
            timing_reports = _load_reports(args.reports, pattern="timing-*.json")
            aggregate = validate_timing_reports(
                timing_reports,
                contract=contract,
                expected_run_id=args.expected_run_id,
                expected_run_attempt=args.expected_run_attempt,
                expected_commit_sha=args.expected_commit_sha,
                expected_lock_sha256=lock_sha,
                expected_report_ids=expected_report_ids,
            )
            aggregate["pytest"] = validate_pytest_reports(
                _load_reports(args.reports, pattern="pytest-*.json"),
                timing_reports=timing_reports,
                contract=contract,
            )
            _write_json(args.output, aggregate)
            print(json.dumps(aggregate, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "compare-three":
            values = [_read_json(path) for path in args.report]
            if any(not isinstance(value, Mapping) for value in values):
                raise ValueError("comparison reports must be JSON objects")
            cache_sha256 = _sha256(args.cache_file)
            if cache_sha256 != args.expected_cache_sha256:
                raise ValueError("trusted comparison cache file identity mismatch")
            comparison = compare_three_runs(
                values,
                contract=contract,
                expected_repository=args.expected_repository,
                expected_workflow=args.expected_workflow,
                expected_workflow_ref=args.expected_workflow_ref,
                expected_run_attempt=args.expected_run_attempt,
                expected_commit_sha=args.expected_commit_sha,
                expected_cache_sha256=cache_sha256,
                expected_candidate_commit=args.expected_candidate_commit,
                expected_candidate_artifact_root=(
                    args.expected_candidate_artifact_root
                ),
                expected_build_identity_root=args.expected_build_identity_root,
                expected_baseline_id=args.expected_baseline_id,
            )
            _write_json(args.output, comparison)
            print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
            return 0
    except KeyboardInterrupt:
        print("ci_timing: timed command interrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"ci_timing: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
