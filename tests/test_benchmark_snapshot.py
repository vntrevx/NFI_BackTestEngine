from __future__ import annotations

import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from nfi_backtest_engine import benchmark
from nfi_backtest_engine.errors import BenchmarkError


def _sealed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    environment = tmp_path / "sealed"
    environment.mkdir()
    python_marker = environment / "python"
    shutil.copy2(Path(sys.executable).resolve(), python_marker)
    monkeypatch.setattr(sys, "executable", str(python_marker))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "2026.5.1")
    return environment, environment / "freqtrade"


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
@pytest.mark.parametrize("kind", ["shell", "python", "elf"])
def test_snapshot_preserves_actual_executable_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    environment, executable = _sealed_environment(monkeypatch, tmp_path)
    if kind == "shell":
        executable.write_text("#!/bin/sh\nprintf 'shell-ok\\n'\n", encoding="utf-8")
    elif kind == "python":
        executable.write_text(
            f"#!{environment / 'python'}\nprint('python-ok')\n", encoding="utf-8"
        )
    else:
        shutil.copy2("/bin/echo", executable)
    executable.chmod(0o755)
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()

    sealed = benchmark._resolve_sealed_freqtrade(
        {"freqtrade": {"version": "2026.5.1"}}
    )
    try:
        sealed.verify_sources_unchanged()
        completed = subprocess.run(
            sealed.launch_command(["freqtrade", "backtesting"]),
            check=True,
            capture_output=True,
            pass_fds=sealed.pass_fds(),
            text=True,
        )
    finally:
        sealed.close()

    assert sealed.sha256 == expected
    assert f"{kind}-ok" in completed.stdout or (
        kind == "elf" and "backtesting" in completed.stdout
    )


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
def test_snapshot_preserves_kernel_shebang_optional_argument_as_one_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment, executable = _sealed_environment(monkeypatch, tmp_path)
    executable.write_text(
        f"#!{environment / 'python'} -I -S\nprint('must-not-run')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    sealed = benchmark._resolve_sealed_freqtrade(
        {"freqtrade": {"version": "2026.5.1"}}
    )
    try:
        assert sealed.interpreter_arguments == ("-I -S",)
    finally:
        sealed.close()


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
def test_snapshot_rejects_oversized_sparse_executable_before_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    with executable.open("wb") as handle:
        handle.truncate(benchmark.MAX_EXECUTABLE_SNAPSHOT_BYTES + 1)
    executable.chmod(0o755)
    before = len(list(Path("/proc/self/fd").iterdir()))

    with pytest.raises(BenchmarkError, match="size|limit"):
        benchmark._resolve_sealed_freqtrade(
            {"freqtrade": {"version": "2026.5.1"}}
        )

    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
@pytest.mark.parametrize("mutation", ["grow", "truncate"])
def test_snapshot_rejects_size_change_during_copy_and_closes_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    executable.write_text("#!/bin/sh\nprintf safe\n", encoding="utf-8")
    executable.chmod(0o755)
    before = len(list(Path("/proc/self/fd").iterdir()))
    triggered = False

    def mutate(name: str, checkpoint: str) -> None:
        nonlocal triggered
        if name == "nfi-freqtrade-executable" and checkpoint == "after-stat" and not triggered:
            with executable.open("r+b") as handle:
                handle.truncate(executable.stat().st_size + (1 if mutation == "grow" else -1))
            triggered = True

    monkeypatch.setattr(benchmark, "_snapshot_copy_checkpoint", mutate)
    with pytest.raises(BenchmarkError, match="changed|grew|size"):
        benchmark._resolve_sealed_freqtrade(
            {"freqtrade": {"version": "2026.5.1"}}
        )

    assert triggered
    assert len(list(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
@pytest.mark.parametrize("mutation", ["in-place", "hardlink"])
@pytest.mark.parametrize("checkpoint", ["after-stat", "after-chunk"])
def test_snapshot_rejects_same_size_executable_mutation_at_copy_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    checkpoint: str,
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    safe = b"#!/bin/sh\nprintf safe\n"
    hostile = b"#!/bin/sh\nprintf evil\n"
    assert len(safe) == len(hostile)
    executable.write_bytes(safe)
    executable.chmod(0o755)
    triggered = False

    def mutate(name: str, reached: str) -> None:
        nonlocal triggered
        if name != "nfi-freqtrade-executable" or reached != checkpoint or triggered:
            return
        target = executable
        if mutation == "hardlink":
            target = tmp_path / "executable-alias"
            os.link(executable, target)
        with target.open("r+b") as handle:
            handle.write(hostile)
        triggered = True

    monkeypatch.setattr(benchmark, "_snapshot_copy_checkpoint", mutate)

    with pytest.raises(BenchmarkError, match="changed|mutation"):
        benchmark._resolve_sealed_freqtrade({"freqtrade": {"version": "2026.5.1"}})

    assert triggered


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
def test_snapshot_rejects_truncated_elf_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    executable.write_bytes(
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"\x02\x00\x3e\x00"
    )
    executable.chmod(0o755)

    with pytest.raises(BenchmarkError, match="ELF|header|program"):
        benchmark._resolve_sealed_freqtrade({"freqtrade": {"version": "2026.5.1"}})


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
@pytest.mark.parametrize("malformation", ["no-program-headers", "segment-out-of-bounds", "machine"])
def test_snapshot_rejects_malformed_complete_elf_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, malformation: str
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    payload = bytearray(Path("/bin/echo").read_bytes())
    assert payload[4:6] == b"\x02\x01"
    if malformation == "no-program-headers":
        payload[56:58] = b"\x00\x00"
    elif malformation == "segment-out-of-bounds":
        program_offset = int.from_bytes(payload[32:40], "little")
        payload[program_offset + 8 : program_offset + 16] = len(payload).to_bytes(8, "little")
    else:
        payload[18:20] = (183).to_bytes(2, "little")
    executable.write_bytes(payload)
    executable.chmod(0o755)

    with pytest.raises(BenchmarkError, match="ELF|program|segment|unsupported"):
        benchmark._resolve_sealed_freqtrade({"freqtrade": {"version": "2026.5.1"}})


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
def test_snapshot_rejects_oversized_shebang_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment, executable = _sealed_environment(monkeypatch, tmp_path)
    interpreter = environment / "oversized-interpreter"
    with interpreter.open("wb") as handle:
        handle.truncate(benchmark.MAX_INTERPRETER_SNAPSHOT_BYTES + 1)
    interpreter.chmod(0o755)
    executable.write_text(f"#!{interpreter}\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(BenchmarkError, match="size|limit"):
        benchmark._resolve_sealed_freqtrade(
            {"freqtrade": {"version": "2026.5.1"}}
        )


@pytest.mark.skipif(os.name != "posix", reason="requires sealed memfd execution")
@pytest.mark.parametrize("first_line", [b"#!\n", b"#!" + b"x" * 300 + b"\n", b"plain text\n"])
def test_snapshot_rejects_malformed_or_unsupported_format_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_line: bytes
) -> None:
    _environment, executable = _sealed_environment(monkeypatch, tmp_path)
    executable.write_bytes(first_line)
    executable.chmod(0o755)

    with pytest.raises(BenchmarkError, match="format|shebang|interpreter"):
        benchmark._resolve_sealed_freqtrade(
            {"freqtrade": {"version": "2026.5.1"}}
        )
