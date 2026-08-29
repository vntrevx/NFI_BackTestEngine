from __future__ import annotations

import importlib.metadata
import json
import mmap
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import psutil
import pytest
from nfi_backtest_engine import benchmark, canonical, windows_job
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.config_loader import load_effective_config
from nfi_backtest_engine.errors import BenchmarkError, NormalizationError, SpecValidationError
from nfi_backtest_engine.evidence_bundle import write_evidence_bundle
from nfi_backtest_engine.normalize import normalize_file, read_freqtrade_export
from nfi_backtest_engine.release_gate import _validate_certificate_archive

ROOT = Path(__file__).parents[1]
SEALED_MEMFD_AVAILABLE = sys.platform.startswith("linux") and hasattr(os, "memfd_create")
OFFICIAL_FIXTURE = (
    ROOT
    / "benchmarks/fixtures/captured/stops-only-spot-2025-01-01_04"
)
HOSTILE_PORTABLE_PATHS = [
    "CON", "con", "Con.txt", "PRN", "prn.json", "AUX", "aux.data",
    "NUL", "nul.txt", "CLOCK$", "clock$.json", "CONIN$", "conin$.x",
    "CONOUT$", "conout$.x", "COM1", "com9.log", "LPT1.bin", "lpt9.data",
    "COM¹", "com².bin", "COM³.x", "LPT¹", "lpt².cfg", "LPT³",
    "file:ads", "file.", "file ", "dir./file", "dir /file", "C:/file",
    "C:file", ".", "../file", "/file", "//server/share/file",
    "\\\\server\\share\\file", "\\\\?\\C:\\file", "\\\\.\\NUL",
    "dir\\file", "dir/../file", "./file", "dir/./file", "dir//file", "..", "",
    "bad<name", "bad>name", 'bad"name', "bad|name", "bad?name", "bad*name",
    "CON .txt", "NUL .json",
]


def test_public_initial_and_destination_paths_reject_portable_aliases(
    tmp_path: Path,
) -> None:
    config = tmp_path / "NUL.json"
    _config(config)
    with pytest.raises(SpecValidationError, match="portable|path"):
        load_effective_config(config)

    reserved_source = tmp_path / "NUL.zip"
    _zip(reserved_source, [("result.json", b'{"trades": []}')])
    with pytest.raises((NormalizationError, SpecValidationError), match="portable|path"):
        normalize_file(reserved_source, tmp_path / "unused.json")
    assert not (tmp_path / "unused.json").exists()

    source = tmp_path / "valid.zip"
    _zip(source, [("result.json", b'{"trades": []}')])
    with pytest.raises((NormalizationError, SpecValidationError), match="portable|path"):
        normalize_file(source, tmp_path / "NUL.txt")
    assert not (tmp_path / "NUL.txt").exists()

    destination = tmp_path / "existing.json"
    destination.write_bytes(b"SENTINEL")
    before = destination.read_bytes()
    with pytest.raises((NormalizationError, SpecValidationError), match="exists"):
        normalize_file(source, destination)
    assert destination.read_bytes() == before


def test_official_sealed_config_and_export_remain_accepted() -> None:
    loaded = load_effective_config(OFFICIAL_FIXTURE / "inputs/config.json")
    export = read_freqtrade_export(OFFICIAL_FIXTURE / "artifacts/freqtrade-result.zip")

    assert loaded["config"]["exchange"]["name"] == "binance"
    assert isinstance(export, dict) and "strategy" in export


def _config(path: Path, *, includes: list[str] | None = None) -> None:
    document: dict[str, object] = {"exchange": {"name": "binance"}}
    if includes is not None:
        document["add_config_files"] = includes
    write_json(path, document)


@pytest.mark.parametrize("include", ["../outside.json", "/tmp/outside.json"])
def test_config_includes_must_be_relative_children(tmp_path: Path, include: str) -> None:
    _config(tmp_path / "config.json", includes=[include])
    _config(tmp_path.parent / "outside.json")

    with pytest.raises(SpecValidationError, match="include path"):
        load_effective_config(tmp_path / "config.json")


@pytest.mark.parametrize(
    "include",
    [
        "C:/outside.json",
        "C:\\outside.json",
        "\\\\server\\share\\outside.json",
        "//server/share/outside.json",
        "\\rooted\\outside.json",
    ],
)
def test_config_include_rejects_cross_platform_path_aliases(
    tmp_path: Path, include: str
) -> None:
    _config(tmp_path / "config.json", includes=[include])

    with pytest.raises(SpecValidationError, match="include path"):
        load_effective_config(tmp_path / "config.json")


@pytest.mark.parametrize(
    "include",
    [
        "NUL",
        "CON",
        "AUX",
        "COM1",
        "child.json.",
        "child.json ",
        "child.json:stream",
        "dir./child.json",
        "dir /child.json",
        "nul.txt",
        "PRN.json",
        "COM9.log",
        "LPT1",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        "COM¹.txt",
        "LPT³.cfg",
    ],
)
def test_config_include_rejects_windows_component_aliases_on_every_host(
    tmp_path: Path, include: str
) -> None:
    target = tmp_path.joinpath(*include.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    _config(target)
    _config(tmp_path / "config.json", includes=[include])

    with pytest.raises(SpecValidationError, match="include path|portable component"):
        load_effective_config(tmp_path / "config.json")


@pytest.mark.parametrize("swap_parent", [False, True])
def test_config_include_rejects_descriptor_open_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    swap_parent: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _config(outside / "child.json")
    (outside / "child.json").write_text(
        '{"exchange":{"name":"OUTSIDE-READ"}}', encoding="utf-8"
    )
    if swap_parent:
        selected = root / "selected"
        selected.mkdir()
        child = selected / "child.json"
        _config(child)
        include = "selected/child.json"
    else:
        selected = None
        child = root / "child.json"
        _config(child)
        include = "child.json"
    _config(root / "config.json", includes=[include])
    swapped = False

    def checkpoint(name: str, component: str) -> None:
        nonlocal swapped
        if swapped:
            return
        if swap_parent and component == "selected":
            assert selected is not None
            selected.rename(root / "selected-original")
            selected.symlink_to(outside, target_is_directory=True)
            swapped = True
        elif not swap_parent and name == "child.json" and component == "child.json":
            child.unlink()
            child.symlink_to(outside / "child.json")
            swapped = True

    monkeypatch.setattr("nfi_backtest_engine.config_loader._config_open_checkpoint", checkpoint)
    with pytest.raises(SpecValidationError, match="symlink|changed|containment"):
        load_effective_config(root / "config.json")

    assert swapped is True
    assert (outside / "child.json").read_text(encoding="utf-8").endswith("}")


def test_config_loading_fails_closed_without_nofollow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "config.json"
    _config(source)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0)

    with pytest.raises(SpecValidationError, match="no-follow containment"):
        load_effective_config(source)


def test_config_rejects_deep_json_before_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "config.json"
    source.write_text(
        '{"exchange":{"name":"binance"},"deep":' + "[" * 120 + "0" + "]" * 120 + "}",
        encoding="utf-8",
    )
    def must_not_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("json.loads was reached")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(SpecValidationError, match="nesting limit"):
        load_effective_config(source)


def test_config_initial_parent_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    deep = outside / "deep"
    deep.mkdir(parents=True)
    write_json(deep / "config.json", {"exchange": {"name": "OUTSIDE"}})
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SpecValidationError, match="symlink|containment|root"):
        load_effective_config(holder / "linked" / "deep" / "config.json")


def test_config_include_rejects_symlink(tmp_path: Path) -> None:
    _config(tmp_path / "included.json")
    (tmp_path / "linked.json").symlink_to("included.json")
    _config(tmp_path / "config.json", includes=["linked.json"])

    with pytest.raises(SpecValidationError, match="symlink"):
        load_effective_config(tmp_path / "config.json")


def test_trusted_benchmark_rejects_manifest_executable_before_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "executed"
    manifest = {
        "fixture_id": "hostile",
        "evidence_status": "captured",
        "freqtrade": {"command": ["sh", "-c", f"touch {sentinel}"]},
        "measurement": {
            "warmup_runs": 0,
            "measured_runs": 1,
            "poll_interval_ms": 1,
        },
    }
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)

    with pytest.raises(BenchmarkError, match="trusted benchmark command"):
        benchmark.run_benchmark(tmp_path / "manifest.json", tmp_path / "report.json")

    assert not sentinel.exists()
    assert not (tmp_path / "report.json").exists()


def test_trusted_benchmark_rejects_ambient_path_executable_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    sentinel = tmp_path / "ambient-ran"
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    executable = ambient / ("freqtrade.exe" if os.name == "nt" else "freqtrade")
    executable.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(sentinel)!r}).touch()\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    manifest = {
        "fixture_id": "hostile-path",
        "evidence_status": "captured",
        "freqtrade": {"version": "2026.5.1", "command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    monkeypatch.setenv("PATH", f"{ambient}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)

    with pytest.raises(BenchmarkError, match="sealed.*freqtrade|Freqtrade.*environment"):
        benchmark.run_benchmark(manifest_path, tmp_path / "report.json")

    assert not sentinel.exists()
    assert not (tmp_path / "report.files").exists()
    assert not (tmp_path / "report.json").exists()


@pytest.mark.skipif(
    not SEALED_MEMFD_AVAILABLE,
    reason="requires sealed memfd descriptor execution",
)
def test_trusted_executable_replacement_after_hash_cannot_execute_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interpreter = sys.executable
    environment = tmp_path / "sealed"
    environment.mkdir()
    python_marker = environment / "python"
    python_marker.write_text("marker", encoding="utf-8")
    executable = environment / "freqtrade"
    executable.write_text(f"#!{interpreter}\n", encoding="utf-8")
    executable.chmod(0o755)
    original_sha256 = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
    sentinel = tmp_path / "replacement-ran"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = {
        "fixture_id": "trusted-replacement",
        "evidence_status": "captured",
        "freqtrade": {"version": "2026.5.1", "command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    replaced = False

    def replace_after_hash(_path: Path) -> None:
        nonlocal replaced
        hostile = environment / "hostile"
        hostile.write_text(
            f"#!{interpreter}\nfrom pathlib import Path\nPath({str(sentinel)!r}).touch()\n",
            encoding="utf-8",
        )
        hostile.chmod(0o755)
        hostile.replace(executable)
        replaced = True

    monkeypatch.setattr(sys, "executable", str(python_marker))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "2026.5.1")
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)
    monkeypatch.setattr(benchmark, "_trusted_executable_checkpoint", replace_after_hash)

    with pytest.raises(BenchmarkError, match="changed|identity"):
        benchmark.run_benchmark(manifest_path, tmp_path / "report.json")

    assert replaced is True
    assert not sentinel.exists()
    assert original_sha256
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.files").exists()


@pytest.mark.skipif(
    not SEALED_MEMFD_AVAILABLE,
    reason="requires sealed memfd immutable snapshot",
)
@pytest.mark.parametrize("mutation", ["truncate-rewrite", "mmap-write", "interpreter"])
def test_trusted_executable_mutation_after_snapshot_fails_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    interpreter = Path(sys.executable).resolve()
    environment = tmp_path / "sealed"
    environment.mkdir()
    python_marker = environment / "python"
    shutil.copy2(interpreter, python_marker)
    executable = environment / "freqtrade"
    sentinel = tmp_path / "changed-ran"
    hostile = (
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    ).encode()
    original = (
        f"#!{python_marker}\n# immutable trusted script\n".encode()
        + b"#" * (len(hostile) + 128)
        + b"\n"
    )
    executable.write_bytes(original)
    executable.chmod(0o755)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = {
        "fixture_id": "trusted-mutation",
        "evidence_status": "captured",
        "freqtrade": {"version": "2026.5.1", "command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }

    def mutate_after_snapshot(_path: Path) -> None:
        if mutation == "truncate-rewrite":
            executable.write_bytes(hostile)
        elif mutation == "mmap-write":
            with executable.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapped:
                mapped[: len(hostile)] = hostile
                mapped.flush()
        else:
            with python_marker.open("r+b") as handle:
                handle.write(b"HOSTILE!")
                handle.flush()
                os.fsync(handle.fileno())

    monkeypatch.setattr(sys, "executable", str(python_marker))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "2026.5.1")
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)
    monkeypatch.setattr(benchmark, "_trusted_executable_checkpoint", mutate_after_snapshot)

    with pytest.raises(BenchmarkError, match="changed|identity"):
        benchmark.run_benchmark(manifest_path, tmp_path / "report.json")

    assert not sentinel.exists()
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.files").exists()


def test_trusted_benchmark_always_passes_bounded_isolated_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = {
        "fixture_id": "trusted-timeout",
        "evidence_status": "captured",
        "freqtrade": {"version": "2026.5.1", "command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    invocation = {
        "exit_code": 0,
        "wall_time_seconds": 0.01,
        "peak_rss_bytes": 1,
        "profile": {"missing_phases": []},
    }
    observed: dict[str, object] = {}

    def capture(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return invocation

    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)
    monkeypatch.setattr(
        benchmark,
        "_resolve_sealed_freqtrade",
        lambda _manifest: benchmark._SealedExecutable(
            Path(sys.executable), "a" * 64, None
        ),
        raising=False,
    )
    monkeypatch.setattr(benchmark, "_run_once", capture)

    benchmark.run_benchmark(manifest_path, tmp_path / "report.json")

    assert isinstance(observed["timeout_seconds"], float)
    assert observed["timeout_seconds"] > 0
    assert observed["isolate_process_group"] is True


def test_trusted_benchmark_deadline_reaps_descendants_on_exact_ready_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    script_source = (
        "import os,socket,subprocess,sys,threading\n"
        "child=subprocess.Popen([\n"
        "    sys.executable,'-c','import threading; threading.Event().wait()'\n"
        "])\n"
        "port=int(os.environ['NFI_TEST_READY_PORT'])\n"
        "with socket.create_connection(('127.0.0.1',port)) as ready:\n"
        "    ready.sendall(str(child.pid).encode())\n"
        "threading.Event().wait()\n"
    )
    manifest = {
        "fixture_id": "trusted-deadline",
        "evidence_status": "captured",
        "freqtrade": {"version": "2026.5.1", "command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)
    monkeypatch.setattr(
        benchmark,
        "_resolve_sealed_freqtrade",
        lambda _manifest: benchmark._SealedExecutable(
            Path(sys.executable), "a" * 64, None
        ),
    )
    monkeypatch.setattr(
        benchmark._SealedExecutable,
        "launch_command",
        lambda _self, _command: [sys.executable, "-c", script_source],
    )
    deadline_seconds = 10.0 if os.name == "nt" else 2.0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        monkeypatch.setenv("NFI_TEST_READY_PORT", str(listener.getsockname()[1]))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                benchmark.run_benchmark,
                manifest_path,
                tmp_path / "report.json",
                trusted_timeout_seconds=deadline_seconds,
            )
            ready_deadline = time.monotonic() + deadline_seconds + 5
            while True:
                try:
                    connection, _address = listener.accept()
                    break
                except TimeoutError:
                    if future.done():
                        future.result()
                    if time.monotonic() >= ready_deadline:
                        raise
            with connection:
                connection.settimeout(5)
                child_pid = int(connection.recv(32).decode())
            with pytest.raises(BenchmarkError, match="timed out"):
                future.result(timeout=deadline_seconds + 5)

    if os.name == "nt":
        assert not psutil.pid_exists(child_pid)
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.files").exists()


def test_benchmark_report_marks_unsafe_override_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = {
        "fixture_id": "research",
        "evidence_status": "captured",
        "freqtrade": {"command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    invocation = {
        "exit_code": 0,
        "wall_time_seconds": 0.01,
        "peak_rss_bytes": 1,
        "profile": {"missing_phases": []},
    }
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)
    monkeypatch.setattr(benchmark, "_run_once", lambda **_kwargs: invocation)

    report = benchmark.run_benchmark(
        manifest_path,
        tmp_path / "report.json",
        command_override=[sys.executable, "-c", "pass"],
    )

    assert report["certification_eligible"] is False
    assert report["execution_mode"] == "unsafe_research_override"
    assert report["certification_ineligibility_reason"]


def test_unsafe_override_timeout_kills_descendants_and_removes_partials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    child_pid_path = tmp_path / "child.pid"
    manifest = {
        "fixture_id": "research",
        "evidence_status": "captured",
        "freqtrade": {"command": ["freqtrade", "backtesting"]},
        "measurement": {"warmup_runs": 0, "measured_runs": 1, "poll_interval_ms": 1},
    }
    script = (
        "import pathlib,subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid)); "
        "p.wait()"
    )
    monkeypatch.setattr(benchmark, "validate_fixture", lambda _path: manifest)
    monkeypatch.setattr(benchmark, "_fixture_trade_count", lambda *_args: 0)

    with pytest.raises(BenchmarkError, match="timed out"):
        benchmark.run_benchmark(
            manifest_path,
            tmp_path / "report.json",
            command_override=[sys.executable, "-c", script],
            unsafe_timeout_seconds=1.0,
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.files").exists()


def _zip(path: Path, members: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)


@pytest.mark.parametrize("member", HOSTILE_PORTABLE_PATHS)
def test_freqtrade_zip_rejects_complete_portable_alias_matrix(
    tmp_path: Path, member: str
) -> None:
    archive = tmp_path / "hostile.zip"
    info = zipfile.ZipInfo(member)
    info.external_attr = 0o100644 << 16
    _zip(archive, [(info, b'{}')])

    with pytest.raises((NormalizationError, ValueError), match="archive|member|path|portable"):
        read_freqtrade_export(archive)


def test_freqtrade_zip_rejects_traversal_member(tmp_path: Path) -> None:
    archive = tmp_path / "hostile.zip"
    _zip(archive, [("../result.json", b'{"trades": []}')])

    with pytest.raises(NormalizationError, match="unsafe archive member path"):
        read_freqtrade_export(archive)


def test_freqtrade_zip_rejects_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "hostile.zip"
    link = zipfile.ZipInfo("result.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _zip(archive, [(link, b"target.json")])

    with pytest.raises(NormalizationError, match="non-regular archive member"):
        read_freqtrade_export(archive)


def test_release_certificate_archive_rejects_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "certificate.zip"
    link = zipfile.ZipInfo("evidence/full-x7-certification.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _zip(archive, [(link, b"certificate.json")])

    with pytest.raises(SpecValidationError, match="not a ZIP archive"):
        _validate_certificate_archive(archive, {})


def test_freqtrade_zip_rejects_excessive_member_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nfi_backtest_engine import archive_security

    monkeypatch.setattr(archive_security, "MAX_ARCHIVE_MEMBERS", 2)
    archive = tmp_path / "hostile.zip"
    _zip(archive, [(f"item-{index}.txt", b"") for index in range(3)])

    with pytest.raises(NormalizationError, match="too many archive members"):
        read_freqtrade_export(archive)


def test_freqtrade_zip_rejects_ratio_and_aggregate_overflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nfi_backtest_engine import archive_security

    archive = tmp_path / "hostile.zip"
    _zip(archive, [("result.json", b" " * 20_000)])
    monkeypatch.setattr(archive_security, "MAX_DECOMPRESSION_RATIO", 2)
    with pytest.raises(NormalizationError, match="decompression ratio"):
        read_freqtrade_export(archive)

    monkeypatch.setattr(archive_security, "MAX_DECOMPRESSION_RATIO", 10_000)
    monkeypatch.setattr(archive_security, "MAX_ARCHIVE_TOTAL_BYTES", 10_000)
    with pytest.raises(NormalizationError, match="aggregate uncompressed size"):
        read_freqtrade_export(archive)


def test_json_reader_rejects_oversized_and_deep_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_text(json.dumps({"payload": "x" * 100}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON byte limit"):
        read_json(oversized, max_bytes=32)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 12 + "0" + "]" * 12, encoding="utf-8")
    monkeypatch.setattr(canonical, "MAX_JSON_BYTES", 1024)
    monkeypatch.setattr(canonical, "MAX_JSON_DEPTH", 10)
    with pytest.raises(ValueError, match="JSON nesting limit"):
        read_json(deep)


def test_json_depth_is_rejected_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("json.loads was reached")

    monkeypatch.setattr(canonical, "MAX_JSON_DEPTH", 4)
    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(ValueError, match="JSON nesting limit"):
        canonical.loads_json_bytes(b'{"quoted":"[[[", "deep":[[[[0]]]]}')


def test_hostile_bundle_preflight_preserves_all_preexisting_destinations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    sentinels = {
        root / "certification-bundle.zip": b"OLD-ARCHIVE",
        root / "bundle-manifest.json": b"OLD-MANIFEST",
        root / "bundle.json": b"OLD-BUNDLE",
        tmp_path / ".nfi-bundle-sentinel": b"OLD-STAGE",
    }
    for path, content in sentinels.items():
        path.write_bytes(content)
    before_entries = sorted(path.name for path in tmp_path.iterdir())
    before_metadata = {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in sentinels
    }

    with pytest.raises(ValueError, match="outside|exists|destination"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[outside],
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == before_entries
    assert {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in sentinels
    } == before_metadata


def test_bundle_root_rejects_fifo_before_publication(tmp_path: Path) -> None:
    write_json(tmp_path / "report.json", {"release_certified": True})
    os.mkfifo(tmp_path / "hostile.fifo")

    with pytest.raises(ValueError, match="non-regular"):
        write_evidence_bundle(
            tmp_path,
            evidence_id="a" * 64,
            release_certified=True,
        )

    assert not (tmp_path / "bundle-manifest.json").exists()
    assert not (tmp_path / "certification-bundle.zip").exists()
    assert not (tmp_path / "bundle.json").exists()


def test_interrupted_bundle_write_removes_partial_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    write_json(report, {"release_certified": True})

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(zipfile.ZipFile, "open", interrupt)
    for _attempt in range(2):
        with pytest.raises(KeyboardInterrupt):
            write_evidence_bundle(
                tmp_path,
                evidence_id="a" * 64,
                release_certified=True,
                include_paths=[report],
            )

        assert not (tmp_path / "bundle-manifest.json").exists()
        assert not (tmp_path / "certification-bundle.zip").exists()
        assert not (tmp_path / ".certification-bundle.zip.tmp").exists()
        assert not (tmp_path / "bundle.json").exists()


@pytest.mark.parametrize(
    "include_factory",
    [
        lambda root, outside: root / ".." / outside.name,
        lambda _root, outside: Path("..") / outside.name,
        lambda _root, outside: outside.absolute(),
        lambda root, _outside: root / "C:/outside.json",
        lambda root, _outside: root / "//server/share/outside.json",
        lambda root, _outside: root / "folder\\outside.json",
    ],
)
def test_explicit_bundle_include_rejects_ambiguous_or_outside_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_factory: object,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel", encoding="utf-8")
    monkeypatch.chdir(root)
    include = include_factory(root, outside)  # type: ignore[operator]

    with pytest.raises(ValueError, match="path|outside|relative|drive|backslash|UNC"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[include],
        )

    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    _assert_no_bundle_publication(root)


@pytest.mark.parametrize("link_parent", [False, True])
def test_explicit_bundle_include_rejects_symlinked_file_or_parent(
    tmp_path: Path,
    link_parent: bool,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("outside sentinel", encoding="utf-8")
    if link_parent:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        include = root / "linked/report.json"
    else:
        (root / "report.json").symlink_to(outside / "report.json")
        include = root / "report.json"

    with pytest.raises(ValueError, match="symlink|regular|path"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[include],
        )

    _assert_no_bundle_publication(root)


def test_explicit_bundle_include_rejects_case_colliding_names(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    upper = root / "Report.json"
    lower = root / "report.json"
    upper.write_text("upper", encoding="utf-8")
    lower.write_text("lower", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate|collision"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[upper, lower],
        )

    _assert_no_bundle_publication(root)


def test_explicit_bundle_include_rejects_validate_then_swap_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    target = root / "race.json"
    target.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swap_after_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if path == "race.json" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_open)
    with pytest.raises(ValueError, match="changed|symlink|race"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[target],
        )

    assert swapped is True
    _assert_no_bundle_publication(root)


@pytest.mark.parametrize("swap_parent", [False, True])
def test_explicit_bundle_include_fails_closed_without_nofollow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    swap_parent: bool,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    selected_parent: Path | None = None
    if swap_parent:
        selected_parent = root / "selected"
        selected_parent.mkdir()
        target = selected_parent / "report.json"
        target.write_text("SAFE", encoding="utf-8")
    else:
        target = root / "report.json"
        target.write_text("SAFE", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swap_before_unsafe_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        if kwargs.get("dir_fd") is not None and not swapped:
            if swap_parent and path == "selected":
                assert selected_parent is not None
                selected_parent.rename(root / "selected-original")
                selected_parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            elif not swap_parent and path == "report.json":
                target.unlink()
                target.symlink_to(outside / "report.json")
                swapped = True
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(os, "open", swap_before_unsafe_open)
    with pytest.raises(ValueError, match="no-follow|unsupported|containment"):
        write_evidence_bundle(
            root,
            evidence_id="a" * 64,
            release_certified=False,
            include_paths=[target],
        )

    assert (outside / "report.json").read_text(encoding="utf-8") == "OUTSIDE-SENTINEL"
    _assert_no_bundle_publication(root)


def test_windows_final_path_containment_contract() -> None:
    from nfi_backtest_engine.windows_path_security import windows_path_is_contained

    assert windows_path_is_contained(
        r"\\?\C:\trusted\bundle",
        r"\\?\C:\trusted\bundle\nested\report.json",
    )
    assert not windows_path_is_contained(
        r"\\?\C:\trusted\bundle",
        r"\\?\C:\outside\report.json",
    )
    assert not windows_path_is_contained(
        r"\\?\C:\trusted\bundle",
        r"\\?\D:\trusted\bundle\report.json",
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse-point APIs")
def test_windows_runtime_rejects_intermediate_reparse(tmp_path: Path) -> None:
    from nfi_backtest_engine.windows_path_security import (
        open_windows_contained_descriptor,
    )

    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    (target / "report.json").write_text("safe", encoding="utf-8")
    link = root / "linked"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(ValueError, match="reparse"):
        open_windows_contained_descriptor(root, "linked/report.json")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse-point APIs")
def test_windows_runtime_parent_swap_resolves_outside_and_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nfi_backtest_engine import windows_path_security

    root = tmp_path / "root"
    root.mkdir()
    selected = root / "selected"
    selected.mkdir()
    (selected / "report.json").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    real_open_relative = windows_path_security._nt_open_relative
    swapped = False

    def swap_before_final(
        ntdll: object,
        parent_handle: int,
        component: str,
        *,
        directory: bool,
    ) -> int:
        nonlocal swapped
        if not swapped and component == "report.json":
            selected.rename(root / "selected-original")
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(selected), str(outside)],
                check=True,
                capture_output=True,
            )
            swapped = True
        return real_open_relative(
            ntdll, parent_handle, component, directory=directory
        )

    monkeypatch.setattr(
        windows_path_security, "_nt_open_relative", swap_before_final
    )
    with pytest.raises(ValueError, match="outside|reparse|changed"):
        windows_path_security.open_windows_contained_descriptor(
            root, "selected/report.json"
        )

    assert swapped is True
    assert (outside / "report.json").read_text(encoding="utf-8") == "OUTSIDE-SENTINEL"


def test_windows_failed_creation_cleanup_reaps_assigned_and_unassigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nfi_backtest_engine import windows_job

    class FakeKernel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def TerminateJobObject(self, _job: int, _code: int) -> int:
            self.calls.append("terminate-job")
            return 1

        def TerminateProcess(self, _process: int, _code: int) -> int:
            self.calls.append("terminate-process")
            return 1

        def WaitForSingleObject(self, _process: int, _timeout: int) -> int:
            self.calls.append("wait-root")
            return windows_job._WAIT_OBJECT_0

    for assigned, expected in (
        (False, ["terminate-process", "wait-root"]),
        (True, ["terminate-job", "active-zero", "wait-root"]),
    ):
        kernel = FakeKernel()

        def active_zero(
            *_args: object, _kernel: FakeKernel = kernel, **_kwargs: object
        ) -> bool:
            _kernel.calls.append("active-zero")
            return True

        monkeypatch.setattr(windows_job, "_wait_active_process_zero", active_zero)
        windows_job._reap_failed_creation(
            kernel,
            job=1,
            completion_port=2,
            process_handle=3,
            assigned=assigned,
        )
        assert kernel.calls == expected


def test_windows_failed_job_termination_uses_kill_on_close_and_still_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nfi_backtest_engine import windows_job

    class FakeKernel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def TerminateJobObject(self, _job: int, _code: int) -> int:
            self.calls.append("terminate-job-failed")
            return 0

        def CloseHandle(self, _job: int) -> int:
            self.calls.append("kill-on-close")
            return 1

        def WaitForSingleObject(self, _process: int, _timeout: int) -> int:
            self.calls.append("wait-root")
            return windows_job._WAIT_OBJECT_0

    kernel = FakeKernel()

    def active_zero(*_args: object, **_kwargs: object) -> bool:
        kernel.calls.append("active-zero")
        return True

    monkeypatch.setattr(windows_job, "_wait_active_process_zero", active_zero)
    assert windows_job._reap_failed_creation(
        kernel,
        job=1,
        completion_port=2,
        process_handle=3,
        assigned=True,
    ) is True
    assert kernel.calls == [
        "terminate-job-failed",
        "kill-on-close",
        "active-zero",
        "wait-root",
    ]


def test_windows_poll_rejects_wait_failure() -> None:
    from nfi_backtest_engine import windows_job

    class FakeKernel:
        def WaitForSingleObject(self, _process: int, _timeout: int) -> int:
            return windows_job._WAIT_FAILED

        def CloseHandle(self, _handle: int) -> int:
            return 1

    process = windows_job.WindowsJobProcess(
        FakeKernel(),
        job=1,
        completion_port=2,
        process_handle=3,
        thread_handle=4,
        pid=5,
    )
    with pytest.raises(BenchmarkError, match="polling"):
        process.poll()
    process.close()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows process Job APIs")
@pytest.mark.parametrize("checkpoint", windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS)
def test_windows_runtime_creation_fault_reaps_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checkpoint: str
) -> None:
    import psutil

    triggered = False
    close_labels: list[str] = []
    descriptor_labels: list[str] = []
    attribute_labels: list[str] = []
    real_close = windows_job._close_owned_handle
    real_descriptor_close = windows_job._close_owned_descriptor
    real_attribute_close = windows_job._close_attribute_list

    def inject(name: str) -> None:
        nonlocal triggered
        if name == checkpoint and not triggered:
            triggered = True
            raise BenchmarkError(f"injected {checkpoint}")

    def track_close(kernel32, handle: int, label: str) -> bool:
        close_labels.append(label)
        return real_close(kernel32, handle, label)

    def track_descriptor(descriptor: int, label: str) -> None:
        descriptor_labels.append(label)
        real_descriptor_close(descriptor, label)

    def track_attribute(kernel32, buffer, label: str) -> None:
        attribute_labels.append(label)
        real_attribute_close(kernel32, buffer, label)

    monkeypatch.setattr(windows_job, "_windows_lifecycle_checkpoint", inject)
    monkeypatch.setattr(windows_job, "_close_owned_handle", track_close)
    monkeypatch.setattr(windows_job, "_close_owned_descriptor", track_descriptor)
    monkeypatch.setattr(windows_job, "_close_attribute_list", track_attribute)
    process = None
    root_pid: int | None = None
    descendant_pid: int | None = None
    resumed_index = windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index("resumed")
    create_checkpoints = set(
        windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS[: resumed_index + 1]
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    script = (
        "import socket,subprocess,sys,threading;"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import threading;threading.Event().wait()']);"
        f"connection=socket.create_connection(('127.0.0.1',{port}));"
        "connection.sendall(str(child.pid).encode());connection.close();"
        "threading.Event().wait()"
    )
    with listener, (tmp_path / "stdout.log").open("wb") as stdout, (
        tmp_path / "stderr.log"
    ).open("wb") as stderr:
        if checkpoint in create_checkpoints:
            with pytest.raises(BenchmarkError, match="injected|cleanup|close"):
                windows_job.WindowsJobProcess.create(
                    [sys.executable, "-c", script],
                    cwd=tmp_path,
                    environment=os.environ.copy(),
                    stdout=stdout,
                    stderr=stderr,
                )
        else:
            process = windows_job.WindowsJobProcess.create(
                [sys.executable, "-c", script],
                cwd=tmp_path,
                environment=os.environ.copy(),
                stdout=stdout,
                stderr=stderr,
            )
            root_pid = process.pid
            connection, _address = listener.accept()
            with connection:
                descendant_pid = int(connection.recv(32).decode())
            with pytest.raises(BenchmarkError, match="injected|failed|cleanup|close"):
                if checkpoint == "before-poll":
                    process.poll()
                elif checkpoint == "before-direct-wait":
                    process.wait(timeout=0)
                elif checkpoint in {"before-job-terminate", "before-active-zero-wait"}:
                    process.terminate_tree()
                else:
                    process.terminate_tree()
                    process.close()
            monkeypatch.setattr(
                windows_job, "_windows_lifecycle_checkpoint", lambda _name: None
            )
            if not checkpoint.startswith("before-close-"):
                if checkpoint != "before-active-zero-wait":
                    process.terminate_tree()
                process.close()
    assert triggered
    assert len(close_labels) == len(set(close_labels))
    assert len(descriptor_labels) == len(set(descriptor_labels))
    assert len(attribute_labels) == len(set(attribute_labels))
    checkpoint_index = windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index(checkpoint)
    required_handle_labels = {"job"}
    if checkpoint_index >= windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index(
        "completion-created"
    ):
        required_handle_labels.add("completion")
    if checkpoint_index >= windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index(
        "process-created"
    ):
        required_handle_labels.update(("process", "thread"))
    assert required_handle_labels <= set(close_labels)
    if checkpoint_index >= windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index(
        "devnull-opened"
    ):
        assert descriptor_labels == ["devnull-descriptor"]
    if checkpoint_index >= windows_job.WINDOWS_LIFECYCLE_CHECKPOINTS.index(
        "attribute-list-ready"
    ):
        assert attribute_labels == ["attribute-list-closed"]
    for pid in (root_pid, descendant_pid):
        if pid is None:
            continue
        with suppress(psutil.NoSuchProcess):
            psutil.Process(pid).wait(timeout=5)
        assert not psutil.pid_exists(pid)


def test_windows_reparse_attribute_contract() -> None:
    from nfi_backtest_engine.windows_path_security import validate_windows_file_attributes

    validate_windows_file_attributes(0x00000080)
    with pytest.raises(ValueError, match="reparse"):
        validate_windows_file_attributes(0x00000400)
    with pytest.raises(ValueError, match="regular"):
        validate_windows_file_attributes(0x00000010)


def _assert_no_bundle_publication(root: Path) -> None:
    assert not (root / "bundle-manifest.json").exists()
    assert not (root / "certification-bundle.zip").exists()
    assert not (root / "bundle.json").exists()
    assert not list(root.glob(".*.tmp"))
    assert not list(root.parent.glob(".nfi-bundle-*"))


def test_bundle_packaging_rejects_symlink_entries(tmp_path: Path) -> None:
    write_json(tmp_path / "report.json", {"release_certified": True})
    (tmp_path / "report-link.json").symlink_to("report.json")

    with pytest.raises(ValueError, match="symlink"):
        write_evidence_bundle(
            tmp_path,
            evidence_id="a" * 64,
            release_certified=True,
        )

    assert not (tmp_path / "certification-bundle.zip").exists()
