"""Build or execute the packaged native Rust simulation core."""

from __future__ import annotations

import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import sha256_file
from .hardware import load_execution_profile


def build_engine(*, force: bool = False) -> dict[str, Any]:
    """Return the packaged engine, or build the source-checkout CLI fallback."""
    native = _native_module()
    root = _project_root_or_none()
    rust_root = root / "rust" if root is not None else None
    current_fingerprint = (
        _rust_source_fingerprint(rust_root) if rust_root is not None else None
    )
    native_fingerprint = (
        _native_source_fingerprint(native) if native is not None else None
    )
    native_is_fresh = (
        native is not None
        and (
            root is None
            or (
                not force
                and native_fingerprint is not None
                and native_fingerprint == current_fingerprint
            )
        )
    )
    if native_is_fresh and native is not None:
        binary = Path(native.__file__).resolve()
        return {
            "schema_version": "1.0.0",
            "built_at": None,
            "source_fingerprint": native_fingerprint,
            "binary_path": str(binary),
            "binary_sha256": sha256_file(binary),
            "binary_bytes": binary.stat().st_size,
            "target": f"{platform.system().lower()}-{platform.machine().lower()}",
            "build_seconds": 0.0,
            "kind": "pyo3-extension",
        }

    # A source checkout may have an importable extension from an older
    # `maturin develop`. Loading it would silently execute stale Rust. Build
    # and use the standalone CLI instead; replacing and re-importing a loaded
    # extension in the current Python process is not reliable on every OS.
    if root is None or rust_root is None or current_fingerprint is None:
        raise BenchmarkError("native Rust extension is unavailable outside a source checkout")
    binary = _engine_binary()
    marker = binary.with_suffix(".build.json")
    if not force and binary.is_file() and marker.is_file():
        existing = read_json(marker)
        if existing.get("source_fingerprint") == current_fingerprint:
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
        "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_fingerprint": current_fingerprint,
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
    vector_manifest: bool = False,
) -> dict[str, Any]:
    """Run one simulation without per-candle Python calls.

    ``vector_manifest=True`` selects the compact, SHA-bound Feather transport.
    It is explicit instead of inferred from a filename so malformed inputs
    cannot accidentally fall through to a different parser.
    """
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
    if build.get("kind") == "pyo3-extension":
        return _run_native_engine(
            source,
            destination,
            event_destination=event_destination,
            profile_path=profile_path,
            timeout_seconds=timeout_seconds,
            build=build,
            vector_manifest=vector_manifest,
        )
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
    if vector_manifest:
        if os.name == "nt":
            command.insert(-2, "--vector-manifest")
        else:
            time_prefix = 6 if Path("/usr/bin/time").is_file() else 1
            command.insert(time_prefix, "--vector-manifest")
    if event_destination is not None:
        command.append(_wsl_path(event_destination) if os.name == "nt" else str(event_destination))

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


def _run_native_engine(
    source: Path,
    destination: Path,
    *,
    event_destination: Path | None,
    profile_path: str | Path | None,
    timeout_seconds: int | None,
    build: dict[str, Any],
    vector_manifest: bool,
) -> dict[str, Any]:
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise BenchmarkError("engine timeout must be positive")
    native = _native_module()
    if native is None:
        raise BenchmarkError("packaged Rust extension disappeared before execution")
    profile = load_execution_profile(profile_path) if profile_path is not None else None
    environment = profile["environment"] if profile is not None else {}
    previous_environment = {key: os.environ.get(key) for key in environment}
    rss_before = _current_rss_bytes()
    cpu_before = time.process_time()
    started_ns = time.perf_counter_ns()
    try:
        os.environ.update(environment)
        if vector_manifest:
            native.simulate_vector_file(source, destination, event_destination)
        else:
            native.simulate_file(source, destination, event_destination)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if event_destination is not None:
            event_destination.unlink(missing_ok=True)
        raise BenchmarkError(f"Rust engine execution failed: {exc}") from exc
    finally:
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    wall_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    cpu_seconds = time.process_time() - cpu_before
    rss_after = _current_rss_bytes()
    result = read_json(destination)
    return {
        "schema_version": "1.0.0",
        "input_path": str(source),
        "input_sha256": sha256_file(source),
        "output_path": str(destination),
        "output_sha256": sha256_file(destination),
        "wall_time_seconds": wall_seconds,
        "peak_rss_bytes": max(value for value in (rss_before, rss_after) if value is not None)
        if rss_before is not None or rss_after is not None
        else None,
        "cpu_time_seconds": cpu_seconds,
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


def _native_module() -> Any | None:
    try:
        from . import _rust
    except ImportError:
        return None
    return _rust if _rust.simulator_available() else None


def _native_source_fingerprint(native: Any) -> str | None:
    getter = getattr(native, "source_fingerprint", None)
    if not callable(getter):
        return None
    value = getter()
    return value if isinstance(value, str) and len(value) == 64 else None


def _current_rss_bytes() -> int | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss
    except (ImportError, OSError):
        return None


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
    files: list[Path] = []
    # Prune `target` before traversal. Filtering the paths yielded by rglob()
    # still walks every Cargo artifact and made a no-op startup take tens of
    # seconds in a developed checkout.
    for directory, child_directories, names in os.walk(rust_root):
        child_directories[:] = [
            name for name in child_directories if name != "target"
        ]
        parent = Path(directory)
        for name in names:
            path = parent / name
            if name in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"} or (
                path.suffix == ".rs"
            ):
                files.append(path)
    files.sort(key=lambda path: path.relative_to(rust_root).as_posix())
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
    root = _project_root_or_none()
    if root is not None:
        return root
    raise BenchmarkError("native Rust extension is not installed and this is not a source checkout")


def _project_root_or_none() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "rust" / "Cargo.toml").is_file():
            return parent
    return None


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive
    if not drive or len(drive) < 2:
        raise BenchmarkError(f"cannot map Windows path into WSL: {resolved}")
    tail = resolved.as_posix()[len(drive) :].lstrip("/")
    return f"/mnt/{drive[0].lower()}/{tail}"
