"""Reproducible external-command benchmark runner for sealed fixtures."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import BenchmarkError
from .fixture import _validate_fixture_retained, sha256_file, validate_fixture
from .portable_paths import parse_portable_relative_path, validate_new_output_path
from .profiling import PROFILE_ENV, aggregate_profile_events
from .windows_job import WindowsJobProcess
from .windows_path_security import open_windows_locked_executable_descriptor

MAX_TRUSTED_TIMEOUT_SECONDS = 3600.0
DEFAULT_TRUSTED_TIMEOUT_SECONDS = 1800.0
MAX_EXECUTABLE_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_INTERPRETER_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = MAX_EXECUTABLE_SNAPSHOT_BYTES + MAX_INTERPRETER_SNAPSHOT_BYTES
MAX_SHEBANG_BYTES = 255
_ORIGINAL_VALIDATE_FIXTURE = validate_fixture


@dataclass(slots=True)
class _SealedExecutable:
    path: Path
    sha256: str
    descriptor: int | None
    source_descriptor: int | None = None
    source_identity: tuple[int, int] | None = None
    interpreter_path: Path | None = None
    interpreter_sha256: str | None = None
    interpreter_descriptor: int | None = None
    interpreter_source_descriptor: int | None = None
    interpreter_source_identity: tuple[int, int] | None = None
    interpreter_arguments: tuple[str, ...] = ()

    def launch_command(self, command: list[str]) -> list[str]:
        if self.descriptor is None or os.name == "nt":
            return [str(self.path), *command[1:]]
        if self.interpreter_descriptor is None:
            return [self._fd_path(self.descriptor), *command[1:]]
        return [
            self._fd_path(self.interpreter_descriptor),
            *self.interpreter_arguments,
            self._fd_path(self.descriptor),
            *command[1:],
        ]

    def pass_fds(self) -> tuple[int, ...]:
        if os.name != "posix":
            return ()
        return tuple(
            descriptor
            for descriptor in (self.descriptor, self.interpreter_descriptor)
            if descriptor is not None
        )

    def verify_sources_unchanged(self) -> None:
        if os.name != "posix" or self.source_descriptor is None:
            return
        _verify_source(
            self.path,
            self.source_descriptor,
            self.source_identity,
            self.sha256,
            "Freqtrade executable",
        )
        if (
            self.interpreter_path is not None
            and self.interpreter_source_descriptor is not None
            and self.interpreter_sha256 is not None
        ):
            _verify_source(
                self.interpreter_path,
                self.interpreter_source_descriptor,
                self.interpreter_source_identity,
                self.interpreter_sha256,
                "trusted interpreter",
            )
        self._close_sources()

    def close(self) -> None:
        descriptors = {
            descriptor
            for descriptor in (
                self.descriptor,
                self.source_descriptor,
                self.interpreter_descriptor,
                self.interpreter_source_descriptor,
            )
            if descriptor is not None
        }
        for descriptor in descriptors:
            os.close(descriptor)
        self.descriptor = None
        self.source_descriptor = None
        self.interpreter_descriptor = None
        self.interpreter_source_descriptor = None

    def _close_sources(self) -> None:
        for attribute in ("source_descriptor", "interpreter_source_descriptor"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)

    @staticmethod
    def _fd_path(descriptor: int) -> str:
        path = Path(f"/proc/self/fd/{descriptor}")
        if not path.exists():
            raise BenchmarkError("immutable descriptor execution requires /proc/self/fd")
        return str(path)


def run_benchmark(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    command_override: Sequence[str] | None = None,
    unsafe_timeout_seconds: float = 300.0,
    trusted_timeout_seconds: float = DEFAULT_TRUSTED_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Measure a trusted sealed command or an explicit unsafe research override."""
    try:
        import psutil
    except ImportError as exc:
        raise BenchmarkError(
            "benchmark measurement requires psutil; install with `uv sync --extra benchmark`"
        ) from exc

    manifest_file = Path(manifest_path).resolve()
    if validate_fixture is _ORIGINAL_VALIDATE_FIXTURE:
        manifest, manifest_payload, fixture_payloads = _validate_fixture_retained(
            manifest_file
        )
    else:
        # Preserve the established test/integration seam while production always
        # consumes the retained descriptor payloads above.
        manifest = validate_fixture(manifest_file)
        manifest_payload = b""
        fixture_payloads = {}
    command = list(command_override or manifest["freqtrade"]["command"])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise BenchmarkError("benchmark command is empty")
    unsafe_override = command_override is not None
    executable_sha256: str | None = None
    sealed_executable: _SealedExecutable | None = None
    if not unsafe_override:
        _validate_trusted_command(command, manifest_file.parent)
        if not 0 < trusted_timeout_seconds <= MAX_TRUSTED_TIMEOUT_SECONDS:
            raise BenchmarkError(
                f"trusted benchmark timeout must be in (0, {MAX_TRUSTED_TIMEOUT_SECONDS:g}]"
            )
        sealed_executable = _resolve_sealed_freqtrade(manifest)
        executable_sha256 = sealed_executable.sha256
        command[0] = str(sealed_executable.path)
        try:
            _trusted_executable_checkpoint(sealed_executable.path)
            sealed_executable.verify_sources_unchanged()
        except BaseException:
            sealed_executable.close()
            raise
    elif unsafe_timeout_seconds <= 0:
        raise BenchmarkError("unsafe benchmark timeout must be positive")

    try:
        output_file = validate_new_output_path(output_path)
    except ValueError as exc:
        if sealed_executable is not None:
            sealed_executable.close()
        raise BenchmarkError(f"benchmark output path is not publishable: {exc}") from exc
    run_directory = output_file.parent / f"{output_file.stem}.files"
    if run_directory.exists() or run_directory.is_symlink():
        if sealed_executable is not None:
            sealed_executable.close()
        raise BenchmarkError(f"benchmark run destination already exists: {run_directory}")
    run_directory.mkdir(parents=True, exist_ok=False)
    measurement = manifest["measurement"]
    warmup_count = measurement["warmup_runs"]
    measured_count = measurement["measured_runs"]
    poll_seconds = measurement["poll_interval_ms"] / 1000

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "fixture_id": manifest["fixture_id"],
        "fixture_evidence_status": manifest["evidence_status"],
        "manifest_path": str(manifest_file),
        "manifest_sha256": __import__("hashlib").sha256(manifest_payload).hexdigest(),
        "trade_count": _fixture_trade_count(manifest, fixture_payloads),
        "command": command,
        "trusted_executable_sha256": executable_sha256,
        "trusted_interpreter_sha256": (
            sealed_executable.interpreter_sha256
            if sealed_executable is not None
            else None
        ),
        "working_directory": str(manifest_file.parent),
        "execution_mode": (
            "unsafe_research_override" if unsafe_override else "trusted_sealed_fixture"
        ),
        "certification_eligible": not unsafe_override,
        "certification_ineligibility_reason": (
            "external command override is research-only and not a sealed certification route"
            if unsafe_override
            else None
        ),
        "hardware": _hardware_record(psutil),
        "warmups": [],
        "runs": [],
    }

    total_invocations = warmup_count + measured_count
    try:
        for invocation_index in range(total_invocations):
            is_warmup = invocation_index < warmup_count
            category = "warmup" if is_warmup else "measured"
            category_index = invocation_index if is_warmup else invocation_index - warmup_count
            label = f"{category}-{category_index + 1:02d}"
            launch_command = list(command)
            if sealed_executable is not None:
                launch_command = sealed_executable.launch_command(command)
            invocation = _run_once(
                psutil=psutil,
                command=launch_command,
                cwd=manifest_file.parent,
                run_directory=run_directory,
                label=label,
                poll_seconds=poll_seconds,
                timeout_seconds=(
                    unsafe_timeout_seconds if unsafe_override else trusted_timeout_seconds
                ),
                isolate_process_group=True,
                pass_fds=(
                    sealed_executable.pass_fds()
                    if sealed_executable is not None
                    else ()
                ),
            )
            report["warmups" if is_warmup else "runs"].append(invocation)
            if invocation["exit_code"] != 0:
                break
    except BaseException:
        shutil.rmtree(run_directory, ignore_errors=True)
        raise
    finally:
        if sealed_executable is not None:
            sealed_executable.close()

    all_runs = [*report["warmups"], *report["runs"]]
    report["complete"] = (
        len(report["warmups"]) == warmup_count
        and len(report["runs"]) == measured_count
        and all(item["exit_code"] == 0 for item in all_runs)
        and all(not item["profile"]["missing_phases"] for item in report["runs"])
    )
    report["measurement_summary"] = _summarize_measured_runs(report["runs"])
    _write_benchmark_report(output_file, report)
    return report


_TRUSTED_VALUE_OPTIONS = frozenset(
    {
        "--backtest-directory",
        "--cache",
        "--config",
        "--datadir",
        "--export",
        "--export-filename",
        "--fee",
        "--margin-mode",
        "--pairs",
        "--strategy",
        "--strategy-path",
        "--timeframe",
        "--timeframe-detail",
        "--timerange",
        "--trading-mode",
    }
)
_TRUSTED_PATH_OPTIONS = frozenset(
    {"--backtest-directory", "--config", "--datadir", "--export-filename", "--strategy-path"}
)


def _resolve_sealed_freqtrade(manifest: dict[str, Any]) -> _SealedExecutable:
    """Bind trusted execution to Freqtrade installed beside this interpreter."""
    environment = Path(sys.executable).resolve().parent
    executable = environment / ("freqtrade.exe" if os.name == "nt" else "freqtrade")
    if executable.is_symlink() or not executable.is_file():
        raise BenchmarkError("sealed Freqtrade environment has no regular executable")
    if executable.resolve().parent != environment:
        raise BenchmarkError("sealed Freqtrade executable escapes its environment")
    expected_version = manifest.get("freqtrade", {}).get("version")
    try:
        installed_version = importlib.metadata.version("freqtrade")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BenchmarkError("sealed Freqtrade environment has no package identity") from exc
    if not isinstance(expected_version, str) or installed_version != expected_version:
        raise BenchmarkError(
            "sealed Freqtrade environment version differs from the fixture identity"
        )
    if os.name == "nt":
        descriptor = open_windows_locked_executable_descriptor(executable)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise BenchmarkError("sealed Freqtrade executable is not regular")
            digest = _sha256_descriptor(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            return _SealedExecutable(executable, digest, descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    if os.name != "posix":
        raise BenchmarkError("sealed executable launch is unsupported on this platform")

    source_descriptor = _open_trusted_source(executable, "Freqtrade executable")
    interpreter_source = -1
    executable_snapshot = -1
    interpreter_snapshot = -1
    try:
        executable_snapshot, digest, executable_size = _immutable_snapshot(
            source_descriptor,
            "nfi-freqtrade-executable",
            max_bytes=MAX_EXECUTABLE_SNAPSHOT_BYTES,
            executable=True,
        )
        kind, interpreter_path, interpreter_arguments = _executable_format(
            executable_snapshot
        )
        if kind == "native":
            return _SealedExecutable(
                executable,
                digest,
                executable_snapshot,
                source_descriptor=source_descriptor,
                source_identity=_descriptor_identity(source_descriptor),
            )
        assert interpreter_path is not None
        interpreter_source = _open_trusted_source(
            interpreter_path, "trusted shebang interpreter"
        )
        interpreter_snapshot, interpreter_digest, interpreter_size = _immutable_snapshot(
            interpreter_source,
            "nfi-shebang-interpreter",
            max_bytes=MAX_INTERPRETER_SNAPSHOT_BYTES,
            executable=True,
        )
        if executable_size + interpreter_size > MAX_SNAPSHOT_TOTAL_BYTES:
            raise BenchmarkError("trusted executable snapshots exceed aggregate size limit")
        interpreter_kind, _nested, _nested_arguments = _executable_format(
            interpreter_snapshot
        )
        if interpreter_kind != "native":
            raise BenchmarkError("trusted shebang interpreter must be a native executable")
        os.fchmod(executable_snapshot, 0o400)
        return _SealedExecutable(
            executable,
            digest,
            executable_snapshot,
            source_descriptor=source_descriptor,
            source_identity=_descriptor_identity(source_descriptor),
            interpreter_path=interpreter_path,
            interpreter_sha256=interpreter_digest,
            interpreter_descriptor=interpreter_snapshot,
            interpreter_source_descriptor=interpreter_source,
            interpreter_source_identity=_descriptor_identity(interpreter_source),
            interpreter_arguments=interpreter_arguments,
        )
    except BaseException:
        for descriptor in (
            source_descriptor,
            interpreter_source,
            executable_snapshot,
            interpreter_snapshot,
        ):
            if descriptor >= 0:
                os.close(descriptor)
        raise


def _open_trusted_source(path: Path, label: str) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise BenchmarkError("sealed executable requires no-follow descriptor support")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
        )
    except OSError as exc:
        raise BenchmarkError(f"cannot retain sealed {label}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
        os.close(descriptor)
        raise BenchmarkError(f"sealed {label} is not a regular executable")
    return descriptor


def _snapshot_copy_checkpoint(_name: str, _checkpoint: str) -> None:
    return


def _immutable_snapshot(
    source_descriptor: int,
    name: str,
    *,
    max_bytes: int,
    executable: bool,
) -> tuple[int, str, int]:
    create = getattr(os, "memfd_create", None)
    allow_sealing = getattr(os, "MFD_ALLOW_SEALING", 0)
    if create is None or not allow_sealing:
        raise BenchmarkError("immutable executable snapshots require sealable memfd support")
    import fcntl

    metadata = os.fstat(source_descriptor)
    initial_state = _descriptor_state(metadata)
    if metadata.st_size < 1 or metadata.st_size > max_bytes:
        raise BenchmarkError(f"immutable executable snapshot size exceeds limit: {name}")
    expected_size = metadata.st_size
    snapshot = create(
        name,
        allow_sealing | getattr(os, "MFD_CLOEXEC", 0),
    )
    digest = __import__("hashlib").sha256()
    copied = 0
    try:
        _snapshot_copy_checkpoint(name, "after-stat")
        if _descriptor_state(os.fstat(source_descriptor)) != initial_state:
            raise BenchmarkError("immutable executable changed after initial stat")
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while copied <= expected_size:
            chunk = os.read(
                source_descriptor,
                min(1024 * 1024, expected_size + 1 - copied),
            )
            if not chunk:
                break
            copied += len(chunk)
            if copied > expected_size or copied > max_bytes:
                raise BenchmarkError("immutable executable grew during snapshot")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot, view)
                if written <= 0:
                    raise BenchmarkError("immutable executable snapshot write failed")
                view = view[written:]
            _snapshot_copy_checkpoint(name, "after-chunk")
            if _descriptor_state(os.fstat(source_descriptor)) != initial_state:
                raise BenchmarkError("immutable executable changed during snapshot copy")
        if (
            copied != expected_size
            or _descriptor_state(os.fstat(source_descriptor)) != initial_state
        ):
            raise BenchmarkError("immutable executable changed during snapshot")
        os.fchmod(snapshot, 0o500 if executable else 0o400)
        seals = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(snapshot, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(snapshot, fcntl.F_GET_SEALS) & seals != seals:
            raise BenchmarkError("immutable executable snapshot sealing failed")
        os.lseek(snapshot, 0, os.SEEK_SET)
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        return snapshot, digest.hexdigest(), copied
    except BaseException:
        os.close(snapshot)
        raise


def _descriptor_state(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _executable_format(
    descriptor: int,
) -> tuple[str, Path | None, tuple[str, ...]]:
    header = os.pread(descriptor, MAX_SHEBANG_BYTES + 2, 0)
    if header.startswith(b"\x7fELF"):
        _validate_elf_snapshot(descriptor)
        return "native", None, ()
    if not header.startswith(b"#!"):
        raise BenchmarkError("trusted executable has an unsupported format")
    newline = header.find(b"\n")
    if newline < 0 or newline > MAX_SHEBANG_BYTES:
        raise BenchmarkError("trusted executable has a malformed or long shebang")
    try:
        shebang = header[2:newline].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkError("trusted executable shebang is malformed") from exc
    # Match Linux binfmt_script semantics: trim horizontal whitespace, split the
    # interpreter at the first whitespace, and pass the entire remainder as one
    # optional argument. Shell tokenization here would silently change argv.
    shebang = shebang.rstrip(" \t").lstrip(" \t")
    split_at = next(
        (index for index, character in enumerate(shebang) if character in " \t"),
        len(shebang),
    )
    interpreter_text = shebang[:split_at]
    optional_argument = shebang[split_at:].lstrip(" \t")
    if (
        not interpreter_text
        or not Path(interpreter_text).is_absolute()
        or "\x00" in shebang
        or "\r" in shebang
    ):
        raise BenchmarkError("trusted executable shebang interpreter is invalid")
    interpreter = Path(interpreter_text).resolve()
    if interpreter.name == "env":
        raise BenchmarkError("environment-resolved shebang interpreters are unsupported")
    arguments = (optional_argument,) if optional_argument else ()
    return "script", interpreter, arguments


def _validate_elf_snapshot(descriptor: int) -> None:
    file_size = os.fstat(descriptor).st_size
    identity = os.pread(descriptor, 64, 0)
    if len(identity) < 16 or identity[4] not in {1, 2} or identity[5] not in {1, 2}:
        raise BenchmarkError("trusted executable has a malformed ELF header")
    elf_class = identity[4]
    byteorder = "little" if identity[5] == 1 else "big"
    header_size = 52 if elf_class == 1 else 64
    program_header_size = 32 if elf_class == 1 else 56
    if len(identity) < header_size or identity[6] != 1:
        raise BenchmarkError("trusted executable has a truncated ELF header")
    expected_class = 2 if struct.calcsize("P") == 8 else 1
    expected_machines = {
        "x86_64": 62,
        "amd64": 62,
        "i386": 3,
        "i686": 3,
        "aarch64": 183,
        "arm64": 183,
    }
    machine = int.from_bytes(identity[18:20], byteorder)
    if (
        elf_class != expected_class
        or identity[5] != (1 if sys.byteorder == "little" else 2)
        or int.from_bytes(identity[16:18], byteorder) not in {2, 3}
        or expected_machines.get(platform.machine().lower()) != machine
    ):
        raise BenchmarkError("trusted executable ELF identity is unsupported")
    if elf_class == 1:
        program_offset = int.from_bytes(identity[28:32], byteorder)
        declared_header_size = int.from_bytes(identity[40:42], byteorder)
        declared_program_size = int.from_bytes(identity[42:44], byteorder)
        program_count = int.from_bytes(identity[44:46], byteorder)
    else:
        program_offset = int.from_bytes(identity[32:40], byteorder)
        declared_header_size = int.from_bytes(identity[52:54], byteorder)
        declared_program_size = int.from_bytes(identity[54:56], byteorder)
        program_count = int.from_bytes(identity[56:58], byteorder)
    table_size = declared_program_size * program_count
    if (
        declared_header_size != header_size
        or declared_program_size != program_header_size
        or program_count < 1
        or program_count == 0xFFFF
        or program_offset < header_size
        or program_offset + table_size > file_size
    ):
        raise BenchmarkError("trusted executable ELF program-header structure is malformed")
    table = os.pread(descriptor, table_size, program_offset)
    if len(table) != table_size:
        raise BenchmarkError("trusted executable ELF program-header table is truncated")
    has_load = False
    has_executable_load = False
    for index in range(program_count):
        entry = table[index * program_header_size : (index + 1) * program_header_size]
        segment_type = int.from_bytes(entry[0:4], byteorder)
        if elf_class == 1:
            segment_offset = int.from_bytes(entry[4:8], byteorder)
            file_bytes = int.from_bytes(entry[16:20], byteorder)
            memory_bytes = int.from_bytes(entry[20:24], byteorder)
            flags = int.from_bytes(entry[24:28], byteorder)
        else:
            flags = int.from_bytes(entry[4:8], byteorder)
            segment_offset = int.from_bytes(entry[8:16], byteorder)
            file_bytes = int.from_bytes(entry[32:40], byteorder)
            memory_bytes = int.from_bytes(entry[40:48], byteorder)
        if file_bytes > memory_bytes or segment_offset + file_bytes > file_size:
            raise BenchmarkError("trusted executable ELF segment bounds are malformed")
        if segment_type == 1:
            has_load = True
            has_executable_load = has_executable_load or bool(flags & 1)
    if not has_load or not has_executable_load:
        raise BenchmarkError("trusted executable ELF has no executable load segment")


def _verify_source(
    path: Path,
    source_descriptor: int,
    expected_identity: tuple[int, int] | None,
    expected_sha256: str,
    label: str,
) -> None:
    current = _open_trusted_source(path, label)
    try:
        if _descriptor_identity(current) != expected_identity:
            raise BenchmarkError(f"sealed {label} path identity changed before staging")
    finally:
        os.close(current)
    if _sha256_descriptor(source_descriptor) != expected_sha256:
        raise BenchmarkError(f"sealed {label} bytes changed before staging")
    os.lseek(source_descriptor, 0, os.SEEK_SET)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _sha256_descriptor(descriptor: int) -> str:
    digest = __import__("hashlib").sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_executable_checkpoint(_path: Path) -> None:
    return


def _validate_trusted_command(command: list[str], root: Path) -> None:
    """Constrain sealed fixture argv; explicit CLI overrides remain unsafe research use."""
    if command[:2] != ["freqtrade", "backtesting"]:
        raise BenchmarkError(
            "trusted benchmark command must use the fixed 'freqtrade backtesting' executable"
        )
    option: str | None = None
    for value in command[2:]:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise BenchmarkError("trusted benchmark command contains an invalid argument")
        if value.startswith("--"):
            option = value.split("=", 1)[0]
            if option not in _TRUSTED_VALUE_OPTIONS:
                raise BenchmarkError(f"trusted benchmark command option is not allowed: {option}")
            if "=" in value:
                _validate_trusted_option_value(option, value.split("=", 1)[1], root)
                option = None
            continue
        if option is None:
            raise BenchmarkError(f"trusted benchmark command has an unbound value: {value}")
        _validate_trusted_option_value(option, value, root)
        if option != "--pairs":
            option = None
    if option is not None and option != "--pairs":
        raise BenchmarkError(f"trusted benchmark command option has no value: {option}")


def _validate_trusted_option_value(option: str, value: str, root: Path) -> None:
    if not value or value.startswith("--"):
        raise BenchmarkError(f"trusted benchmark command has an invalid value for {option}")
    if option not in _TRUSTED_PATH_OPTIONS:
        return
    try:
        relative = Path(*parse_portable_relative_path(value).parts)
    except ValueError as exc:
        raise BenchmarkError(f"trusted benchmark path is not portable: {value}") from exc
    candidate = root / relative
    current = candidate
    while current != root:
        if current.is_symlink():
            raise BenchmarkError(f"trusted benchmark path traverses a symlink: {value}")
        current = current.parent


def _run_once(
    *,
    psutil: Any,
    command: list[str],
    cwd: Path,
    run_directory: Path,
    label: str,
    poll_seconds: float,
    timeout_seconds: float | None = None,
    isolate_process_group: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> dict[str, Any]:
    stdout_path = run_directory / f"{label}.stdout.log"
    stderr_path = run_directory / f"{label}.stderr.log"
    events_path = run_directory / f"{label}.profile.jsonl"
    if events_path.exists():
        events_path.unlink()

    environment = os.environ.copy()
    environment[PROFILE_ENV] = str(events_path)
    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    peak_rss = 0
    latest_cpu_seconds = 0.0

    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            if os.name == "nt" and isolate_process_group:
                process: Any = WindowsJobProcess.create(
                    command,
                    cwd=cwd,
                    environment=environment,
                    stdout=stdout,
                    stderr=stderr,
                )
            else:
                popen_options: dict[str, Any] = {}
                if pass_fds:
                    popen_options["pass_fds"] = pass_fds
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    start_new_session=isolate_process_group and os.name == "posix",
                    **popen_options,
                )
        except OSError as exc:
            raise BenchmarkError(f"failed to start benchmark command: {exc}") from exc

        root_process = psutil.Process(process.pid)
        deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        )
        tree_terminated = False
        try:
            while process.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    _terminate_process_tree(
                        psutil,
                        process,
                        root_process,
                        isolated_group=isolate_process_group,
                    )
                    tree_terminated = True
                    raise BenchmarkError(
                        f"benchmark command timed out after {timeout_seconds:g} seconds"
                    )
                rss, cpu_seconds = _process_tree_snapshot(psutil, root_process)
                peak_rss = max(peak_rss, rss)
                latest_cpu_seconds = max(latest_cpu_seconds, cpu_seconds)
                remaining = (
                    deadline - time.monotonic() if deadline is not None else poll_seconds
                )
                time.sleep(max(0.0, min(poll_seconds, remaining)))
            rss, cpu_seconds = _process_tree_snapshot(psutil, root_process)
            peak_rss = max(peak_rss, rss)
            latest_cpu_seconds = max(latest_cpu_seconds, cpu_seconds)
            exit_code = process.wait()
            if isinstance(process, WindowsJobProcess) and not process.wait_job_empty(5.0):
                process.terminate_tree(exit_code)
        except BaseException:
            if not tree_terminated and (
                isinstance(process, WindowsJobProcess) or process.poll() is None
            ):
                _terminate_process_tree(
                    psutil,
                    process,
                    root_process,
                    isolated_group=isolate_process_group,
                )
            raise
        finally:
            if isinstance(process, WindowsJobProcess):
                process.close()

    ended_at = datetime.now(UTC)
    profile = (
        aggregate_profile_events(events_path)
        if events_path.is_file()
        else {
            "schema_version": "1.0.0",
            "phases": {},
            "missing_phases": [
                "indicators",
                "callbacks",
                "trade_scans",
                "event_simulation",
            ],
        }
    )
    return {
        "label": label,
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(ended_at),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "peak_rss_bytes": peak_rss,
        "cpu_time_seconds": latest_cpu_seconds,
        "exit_code": exit_code,
        "stdout": {
            "path": str(stdout_path),
            "bytes": stdout_path.stat().st_size,
            "sha256": sha256_file(stdout_path),
        },
        "stderr": {
            "path": str(stderr_path),
            "bytes": stderr_path.stat().st_size,
            "sha256": sha256_file(stderr_path),
        },
        "profile": profile,
    }


def _terminate_process_tree(
    psutil: Any,
    process: Any,
    root_process: Any,
    *,
    isolated_group: bool,
) -> None:
    if isinstance(process, WindowsJobProcess):
        process.terminate_tree()
        return
    try:
        descendants = root_process.children(recursive=True)
    except psutil.Error:
        descendants = []
    if isolated_group and os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        for child in descendants:
            with suppress(psutil.Error):
                child.terminate()
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if isolated_group and os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkError("benchmark process could not be reaped after kill") from exc
    _gone, alive = psutil.wait_procs(descendants, timeout=2)
    for child in alive:
        with suppress(psutil.Error):
            child.kill()
    psutil.wait_procs(alive, timeout=2)


def _process_tree_snapshot(psutil: Any, root_process: Any) -> tuple[int, float]:
    try:
        processes = [root_process, *root_process.children(recursive=True)]
    except psutil.Error:
        processes = [root_process]
    rss = 0
    cpu_seconds = 0.0
    for process in processes:
        try:
            rss += process.memory_info().rss
            cpu = process.cpu_times()
            cpu_seconds += cpu.user + cpu.system
        except psutil.Error:
            continue
    return rss, cpu_seconds


def _summarize_measured_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if run["exit_code"] == 0]
    if not successful:
        return {"successful_runs": 0}
    wall_times = [run["wall_time_seconds"] for run in successful]
    peaks = [run["peak_rss_bytes"] for run in successful]
    return {
        "successful_runs": len(successful),
        "wall_time_seconds": {
            "minimum": min(wall_times),
            "maximum": max(wall_times),
            "mean": sum(wall_times) / len(wall_times),
        },
        "peak_rss_bytes": {
            "minimum": min(peaks),
            "maximum": max(peaks),
            "mean": sum(peaks) / len(peaks),
        },
    }


def _hardware_record(psutil: Any) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "total_memory_bytes": memory.total,
    }


def _fixture_trade_count(
    manifest: dict[str, Any], fixture_payloads: dict[str, bytes]
) -> int:
    surface_name = manifest["artifacts"]["trade_surface"]["path"]
    surface = json.loads(fixture_payloads[surface_name])
    return len(surface["trades"])


def _write_benchmark_report(destination: Path, report: dict[str, Any]) -> None:
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise BenchmarkError(f"benchmark output already exists: {destination}") from exc
    metadata = os.fstat(descriptor)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current = destination.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == identity:
                destination.unlink()
        except FileNotFoundError:
            pass
        raise


def _utc_string(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
