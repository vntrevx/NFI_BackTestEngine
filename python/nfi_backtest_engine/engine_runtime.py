"""Build and execute the Linux Rust core from Windows/WSL or native Linux."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .hardware import load_execution_profile


def build_engine(*, force: bool = False) -> dict[str, Any]:
    """Build only when the Rust source fingerprint differs from the marker."""
    root = _project_root()
    rust_root = root / "rust"
    binary = _engine_binary()
    marker = binary.with_suffix(".build.json")
    fingerprint = _rust_source_fingerprint(rust_root)
    if not force and binary.is_file() and marker.is_file():
        existing = read_json(marker)
        if existing.get("source_fingerprint") == fingerprint:
            return existing

    started_ns = time.perf_counter_ns()
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            raise BenchmarkError("WSL is required to build the Linux engine on Windows")
        command = [
            wsl,
            "-e",
            "bash",
            "-lc",
            (
                f"cd {shlex.quote(_wsl_path(rust_root))} && "
                "cargo build --release --locked -p nfi-sim-cli"
            ),
        ]
    else:
        cargo = shutil.which("cargo")
        if cargo is None:
            raise BenchmarkError("Cargo is not installed or not on PATH")
        command = [cargo, "build", "--release", "--locked", "-p", "nfi-sim-cli"]
    completed = subprocess.run(
        command,
        cwd=rust_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not binary.is_file():
        raise BenchmarkError(
            "Rust engine build failed: "
            f"{completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()}"
        )
    record = {
        "schema_version": "1.0.0",
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_fingerprint": fingerprint,
        "binary_path": str(binary),
        "binary_sha256": sha256_file(binary),
        "binary_bytes": binary.stat().st_size,
        "target": "x86_64-unknown-linux-gnu",
        "build_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
    }
    write_json(marker, record)
    return record


def run_engine(
    input_path: str | Path,
    output_path: str | Path,
    *,
    profile_path: str | Path | None = None,
    timeout_seconds: int | None = None,
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run one JSON simulation without a shell or per-candle Python calls."""
    source = Path(input_path).resolve()
    destination = Path(output_path).resolve()
    if not source.is_file():
        raise BenchmarkError(f"simulation input does not exist: {source}")
    if destination.exists():
        raise BenchmarkError(f"simulation output already exists: {destination}")
    event_destination = Path(events_path).resolve() if events_path is not None else None
    if event_destination is not None and event_destination.exists():
        raise BenchmarkError(f"simulation events output already exists: {event_destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if event_destination is not None:
        event_destination.parent.mkdir(parents=True, exist_ok=True)
    build = build_engine()
    binary = Path(build["binary_path"])
    resource_path = destination.parent / f".{destination.name}.resources"
    if resource_path.exists():
        raise BenchmarkError(f"engine resource output already exists: {resource_path}")
    environment = os.environ.copy()
    profile = None
    if profile_path is not None:
        profile = load_execution_profile(profile_path)
        environment.update(profile["environment"])

    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            raise BenchmarkError("WSL is required to run the Linux engine on Windows")
        command = [
            wsl,
            "-e",
            "/usr/bin/time",
            "-f",
            "max_rss_kib=%M\nuser_seconds=%U\nsystem_seconds=%S",
            "-o",
            _wsl_path(resource_path),
            _wsl_path(binary),
            _wsl_path(source),
            _wsl_path(destination),
        ]
    else:
        time_executable = Path("/usr/bin/time")
        command = (
            [
                str(time_executable),
                "-f",
                "max_rss_kib=%M\nuser_seconds=%U\nsystem_seconds=%S",
                "-o",
                str(resource_path),
                str(binary),
                str(source),
                str(destination),
            ]
            if time_executable.is_file()
            else [str(binary), str(source), str(destination)]
        )
    if event_destination is not None:
        command.append(
            _wsl_path(event_destination) if os.name == "nt" else str(event_destination)
        )

    started_ns = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            command,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError("Rust engine execution timed out") from exc
    wall_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    if completed.returncode != 0 or not destination.is_file():
        raise BenchmarkError(
            "Rust engine execution failed: "
            f"{completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()}"
        )
    resources = _read_resource_record(resource_path)
    resource_path.unlink(missing_ok=True)
    result = read_json(destination)
    return {
        "schema_version": "1.0.0",
        "input_path": str(source),
        "input_sha256": sha256_file(source),
        "output_path": str(destination),
        "output_sha256": sha256_file(destination),
        "wall_time_seconds": wall_seconds,
        "peak_rss_bytes": resources.get("peak_rss_bytes"),
        "cpu_time_seconds": resources.get("cpu_time_seconds"),
        "build": build,
        "execution_profile_fingerprint": (
            profile["hardware_fingerprint"] if profile is not None else None
        ),
        "trade_count": len(result.get("trades", [])),
        "events": (
            {
                "path": str(event_destination),
                "bytes": event_destination.stat().st_size,
                "sha256": sha256_file(event_destination),
            }
            if event_destination is not None and event_destination.is_file()
            else None
        ),
    }


def _read_resource_record(path: Path) -> dict[str, float | int]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    try:
        peak_rss_bytes = int(values["max_rss_kib"]) * 1024
        cpu_time_seconds = float(values["user_seconds"]) + float(values["system_seconds"])
    except (KeyError, ValueError):
        return {}
    return {
        "peak_rss_bytes": peak_rss_bytes,
        "cpu_time_seconds": cpu_time_seconds,
    }


def _rust_source_fingerprint(rust_root: Path) -> str:
    hasher = hashlib.sha256()
    files = sorted(
        (
            path
            for path in rust_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and (
                path.name in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}
                or path.suffix == ".rs"
            )
        ),
        key=lambda path: path.relative_to(rust_root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(rust_root).as_posix().encode()
        hasher.update(len(relative).to_bytes(4, "big"))
        hasher.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _engine_binary() -> Path:
    return _project_root() / "rust" / "target" / "release" / "nfi-sim"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive
    if not drive or len(drive) < 2:
        raise BenchmarkError(f"cannot map Windows path into WSL: {resolved}")
    tail = resolved.as_posix()[len(drive) :].lstrip("/")
    return f"/mnt/{drive[0].lower()}/{tail}"
