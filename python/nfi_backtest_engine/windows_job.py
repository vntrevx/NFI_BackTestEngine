"""Windows suspended-process launch inside a kill-on-close Job Object."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO

from .errors import BenchmarkError

_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
_JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_STILL_ACTIVE = 259
_CLEANUP_TIMEOUT_SECONDS = 30.0
WINDOWS_LIFECYCLE_CHECKPOINTS = (
    "job-created",
    "completion-created",
    "job-configured",
    "before-devnull-open",
    "devnull-opened",
    "stdio-handles-ready",
    "attribute-list-ready",
    "inheritance-enabled",
    "before-create",
    "process-created",
    "inheritance-restored",
    "attribute-list-closed",
    "devnull-closed",
    "before-assign",
    "assigned",
    "before-resume",
    "resumed",
    "before-poll",
    "before-direct-wait",
    "before-job-terminate",
    "before-active-zero-wait",
    "before-close-thread",
    "before-close-process",
    "before-close-completion",
    "before-close-job",
)


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong), ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong), ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _AssociateCompletionPort(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", ctypes.c_void_p)]


class _WindowsCleanupError(BenchmarkError):
    def __init__(self, message: str, *, job_closed: bool = False) -> None:
        super().__init__(message)
        self.job_closed = job_closed


def _windows_lifecycle_checkpoint(_name: str) -> None:
    """Exact fault-injection point used by Windows runtime regressions."""
    return


def _close_owned_descriptor(descriptor: int, _label: str) -> None:
    os.close(descriptor)


def _close_attribute_list(kernel32: Any, buffer: Any, _label: str) -> None:
    kernel32.DeleteProcThreadAttributeList(
        ctypes.cast(buffer, ctypes.c_void_p)
    )


class WindowsJobProcess:
    """Popen-like root process assigned before resume to one Job Object."""

    def __init__(
        self, kernel32: Any, *, job: int, completion_port: int,
        process_handle: int, thread_handle: int, pid: int,
    ) -> None:
        self._kernel32 = kernel32
        self._job = job
        self._completion_port = completion_port
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self.pid = pid
        self._closed = False
        self._job_empty = False

    @classmethod
    def create(
        cls, command: list[str], *, cwd: Path, environment: dict[str, str],
        stdout: BinaryIO, stderr: BinaryIO,
    ) -> WindowsJobProcess:
        if os.name != "nt":
            raise BenchmarkError("Windows Job Object launch is unavailable")
        import msvcrt

        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(kernel32)
        job = _checked_handle(kernel32.CreateJobObjectW(None, None), "CreateJobObjectW")
        completion = 0
        process_handle = 0
        thread_handle = 0
        assigned = False
        try:
            _windows_lifecycle_checkpoint("job-created")
            completion = _checked_handle(
                kernel32.CreateIoCompletionPort(ctypes.c_void_p(-1), None, 0, 1),
                "CreateIoCompletionPort",
            )
            _windows_lifecycle_checkpoint("completion-created")
            _configure_job(kernel32, job, completion)
            _windows_lifecycle_checkpoint("job-configured")

            _windows_lifecycle_checkpoint("before-devnull-open")
            devnull_fd = os.open(os.devnull, os.O_RDONLY)
            try:
                _windows_lifecycle_checkpoint("devnull-opened")
                inherited_handles = (
                    msvcrt.get_osfhandle(devnull_fd),
                    msvcrt.get_osfhandle(stdout.fileno()),
                    msvcrt.get_osfhandle(stderr.fileno()),
                )
                _windows_lifecycle_checkpoint("stdio-handles-ready")
            except BaseException:
                _close_owned_descriptor(devnull_fd, "devnull-descriptor")
                raise
            attribute_buffer: Any = None
            attribute_initialized = False
            try:
                startup, attribute_buffer, handle_array = _startup_with_handle_allowlist(
                    kernel32, inherited_handles
                )
                attribute_initialized = True
                _windows_lifecycle_checkpoint("attribute-list-ready")
                startup.StartupInfo.hStdInput = ctypes.c_void_p(inherited_handles[0])
                startup.StartupInfo.hStdOutput = ctypes.c_void_p(inherited_handles[1])
                startup.StartupInfo.hStdError = ctypes.c_void_p(inherited_handles[2])
                _ = handle_array  # retain the attribute-list payload through CreateProcessW
                information = _ProcessInformation()
                command_line = ctypes.create_unicode_buffer(
                    subprocess.list2cmdline(command)
                )
                environment_block = ctypes.create_unicode_buffer(
                    "\0".join(
                        f"{key}={value}" for key, value in sorted(environment.items())
                    ) + "\0\0"
                )
                inheritance_enabled: list[int] = []
                try:
                    for inherited_handle in inherited_handles:
                        os.set_handle_inheritable(inherited_handle, True)
                        inheritance_enabled.append(inherited_handle)
                    _windows_lifecycle_checkpoint("inheritance-enabled")
                    _windows_lifecycle_checkpoint("before-create")
                    _check(
                        kernel32.CreateProcessW(
                            command[0], command_line, None, None, True,
                            _CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP
                            | _CREATE_UNICODE_ENVIRONMENT
                            | _EXTENDED_STARTUPINFO_PRESENT,
                            environment_block, str(cwd), ctypes.byref(startup),
                            ctypes.byref(information),
                        ),
                        "CreateProcessW suspended",
                    )
                    # Ownership transfers immediately while PROCESS_INFORMATION is live.
                    process_handle = int(information.hProcess or 0)
                    thread_handle = int(information.hThread or 0)
                    if not process_handle or not thread_handle:
                        raise BenchmarkError(
                            "CreateProcessW returned incomplete process ownership"
                        )
                    _windows_lifecycle_checkpoint("process-created")
                finally:
                    restoration_failed = False
                    for inherited_handle in inheritance_enabled:
                        try:
                            os.set_handle_inheritable(inherited_handle, False)
                        except OSError:
                            restoration_failed = True
                    _windows_lifecycle_checkpoint("inheritance-restored")
                    if restoration_failed:
                        raise BenchmarkError(
                            "restoring inherited Windows handles failed"
                        )
            finally:
                try:
                    if attribute_initialized:
                        _close_attribute_list(
                            kernel32,
                            attribute_buffer,
                            "attribute-list-closed",
                        )
                        _windows_lifecycle_checkpoint("attribute-list-closed")
                finally:
                    _close_owned_descriptor(devnull_fd, "devnull-descriptor")
                    _windows_lifecycle_checkpoint("devnull-closed")

            _windows_lifecycle_checkpoint("before-assign")
            _check(
                kernel32.AssignProcessToJobObject(job, process_handle),
                "AssignProcessToJobObject",
            )
            assigned = True
            _windows_lifecycle_checkpoint("assigned")
            _windows_lifecycle_checkpoint("before-resume")
            if kernel32.ResumeThread(thread_handle) == 0xFFFFFFFF:
                raise BenchmarkError("ResumeThread failed")
            _windows_lifecycle_checkpoint("resumed")
            return cls(
                kernel32, job=job, completion_port=completion,
                process_handle=process_handle, thread_handle=thread_handle,
                pid=int(information.dwProcessId),
            )
        except BaseException as exc:
            cleanup_error: BaseException | None = None
            if process_handle:
                try:
                    job_closed = _reap_failed_creation(
                        kernel32, job=job, completion_port=completion,
                        process_handle=process_handle, assigned=assigned,
                    )
                    if job_closed:
                        job = 0
                except BaseException as cleanup_exc:
                    if isinstance(cleanup_exc, _WindowsCleanupError) and cleanup_exc.job_closed:
                        job = 0
                    cleanup_error = cleanup_exc
            close_failed = False
            for label, handle in (
                ("thread", thread_handle),
                ("process", process_handle),
                ("completion", completion),
                ("job", job),
            ):
                if handle and not _close_owned_handle(kernel32, handle, label):
                    close_failed = True
            if close_failed and cleanup_error is None:
                cleanup_error = BenchmarkError(
                    "Windows process creation handle close failed"
                )
            if cleanup_error is not None:
                raise BenchmarkError(
                    f"Windows process creation failed and cleanup failed: {cleanup_error}"
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, BenchmarkError):
                raise
            raise BenchmarkError("Windows suspended process creation failed") from exc

    def poll(self) -> int | None:
        _windows_lifecycle_checkpoint("before-poll")
        result = self._kernel32.WaitForSingleObject(self._process_handle, 0)
        if result == _WAIT_TIMEOUT:
            return None
        if result != _WAIT_OBJECT_0:
            raise BenchmarkError(
                f"polling Windows benchmark root failed: {_last_error()}"
            )
        return self._exit_code()

    def wait(self, timeout: float | None = None) -> int:
        _windows_lifecycle_checkpoint("before-direct-wait")
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = self._kernel32.WaitForSingleObject(self._process_handle, milliseconds)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(
                "Windows Job Object process", -1 if timeout is None else timeout
            )
        if result != _WAIT_OBJECT_0:
            raise BenchmarkError(
                f"waiting for Windows benchmark root failed: {_last_error()}"
            )
        return self._exit_code()

    def terminate_tree(self, exit_code: int = 124) -> None:
        _windows_lifecycle_checkpoint("before-job-terminate")
        termination_error: BaseException | None = None
        if not self._kernel32.TerminateJobObject(self._job, exit_code):
            if self._kernel32.CloseHandle(self._job):
                self._job = 0
            else:
                termination_error = BenchmarkError(
                    "TerminateJobObject and kill-on-close both failed"
                )
        empty = False
        try:
            empty = self.wait_job_empty(_CLEANUP_TIMEOUT_SECONDS)
        except BaseException as exc:
            termination_error = exc
        try:
            self.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except BaseException as exc:
            if termination_error is None:
                termination_error = exc
        if not empty and termination_error is None:
            termination_error = BenchmarkError(
                "Windows Job Object did not reach active-process-zero"
            )
        if termination_error is not None:
            raise BenchmarkError(
                "Windows benchmark process tree cleanup failed"
            ) from termination_error

    def wait_job_empty(self, timeout_seconds: float) -> bool:
        _windows_lifecycle_checkpoint("before-active-zero-wait")
        if self._job_empty:
            return True
        self._job_empty = _wait_active_process_zero(
            self._kernel32, self._completion_port, timeout_seconds
        )
        return self._job_empty

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_failed = False
        for label, handle in (
            ("thread", self._thread_handle),
            ("process", self._process_handle),
            ("completion", self._completion_port),
            ("job", self._job),
        ):
            if handle and not _close_owned_handle(self._kernel32, handle, label):
                close_failed = True
        if close_failed:
            raise BenchmarkError("closing Windows benchmark handles failed")

    def _exit_code(self) -> int:
        code = ctypes.c_ulong()
        _check(
            self._kernel32.GetExitCodeProcess(
                self._process_handle, ctypes.byref(code)
            ),
            "GetExitCodeProcess",
        )
        if code.value == _STILL_ACTIVE:
            raise BenchmarkError("Windows process signaled completion but is still active")
        return int(code.value)


def _configure_job(kernel32: Any, job: int, completion: int) -> None:
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _check(
        kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ),
        "SetInformationJobObject kill-on-close",
    )
    association = _AssociateCompletionPort(
        ctypes.c_void_p(job), ctypes.c_void_p(completion)
    )
    _check(
        kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION,
            ctypes.byref(association), ctypes.sizeof(association),
        ),
        "SetInformationJobObject completion-port",
    )


def _startup_with_handle_allowlist(
    kernel32: Any, inherited_handles: tuple[int, int, int]
) -> tuple[_StartupInfoExW, Any, Any]:
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise BenchmarkError("InitializeProcThreadAttributeList sizing failed")
    buffer = ctypes.create_string_buffer(size.value)
    pointer = ctypes.cast(buffer, ctypes.c_void_p)
    _check(
        kernel32.InitializeProcThreadAttributeList(pointer, 1, 0, ctypes.byref(size)),
        "InitializeProcThreadAttributeList",
    )
    handles = (ctypes.c_void_p * len(inherited_handles))(
        *(ctypes.c_void_p(handle) for handle in inherited_handles)
    )
    try:
        _check(
            kernel32.UpdateProcThreadAttribute(
                pointer, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(handles), ctypes.sizeof(handles), None, None,
            ),
            "UpdateProcThreadAttribute handle allowlist",
        )
    except BaseException:
        kernel32.DeleteProcThreadAttributeList(pointer)
        raise
    startup = _StartupInfoExW()
    startup.StartupInfo.cb = ctypes.sizeof(startup)
    startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
    startup.lpAttributeList = pointer
    return startup, buffer, handles


def _reap_failed_creation(
    kernel32: Any, *, job: int, completion_port: int,
    process_handle: int, assigned: bool,
) -> bool:
    cleanup_error: BaseException | None = None
    job_closed = False
    if assigned:
        if not kernel32.TerminateJobObject(job, 130):
            if kernel32.CloseHandle(job):
                job_closed = True
            else:
                cleanup_error = BenchmarkError(
                    "failed Windows Job could not be terminated or kill-on-close closed"
                )
        try:
            if not _wait_active_process_zero(
                kernel32, completion_port, _CLEANUP_TIMEOUT_SECONDS
            ):
                raise BenchmarkError(
                    "failed Windows Job did not reach active-process-zero"
                )
        except BaseException as exc:
            cleanup_error = exc
    elif not kernel32.TerminateProcess(process_handle, 130):
        cleanup_error = BenchmarkError("TerminateProcess after create failure failed")
    result = kernel32.WaitForSingleObject(
        process_handle, int(_CLEANUP_TIMEOUT_SECONDS * 1000)
    )
    if result != _WAIT_OBJECT_0:
        cleanup_error = BenchmarkError("failed Windows root process was not reaped")
    if cleanup_error is not None:
        raise _WindowsCleanupError(
            "failed Windows process cleanup was incomplete",
            job_closed=job_closed,
        ) from cleanup_error
    return job_closed


def _wait_active_process_zero(
    kernel32: Any, completion_port: int, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        message = ctypes.c_ulong()
        key = ctypes.c_size_t()
        overlapped = ctypes.c_void_p()
        succeeded = kernel32.GetQueuedCompletionStatus(
            completion_port, ctypes.byref(message), ctypes.byref(key),
            ctypes.byref(overlapped), max(1, int(remaining * 1000)),
        )
        if succeeded:
            if message.value == _JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO:
                return True
            continue
        error = _last_error()
        if error == _WAIT_TIMEOUT:
            return False
        raise BenchmarkError(f"waiting for Windows Job completion failed: {error}")


def _configure(kernel32: Any) -> None:
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateIoCompletionPort.restype = ctypes.c_void_p
    kernel32.CreateProcessW.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.ResumeThread.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.GetQueuedCompletionStatus.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_int
    kernel32.UpdateProcThreadAttribute.restype = ctypes.c_int


def _check(value: Any, operation: str) -> None:
    if not value:
        raise BenchmarkError(f"{operation} failed: {_last_error()}")


def _close_owned_handle(kernel32: Any, handle: int, label: str) -> bool:
    injected = False
    try:
        _windows_lifecycle_checkpoint(f"before-close-{label}")
    except BaseException:
        injected = True
    closed = bool(kernel32.CloseHandle(handle))
    return closed and not injected


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _checked_handle(value: Any, operation: str) -> int:
    _check(value, operation)
    return int(value)
