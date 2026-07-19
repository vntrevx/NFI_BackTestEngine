"""Pinned, offline Freqtrade reference execution for sealed fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import read_json, write_json
from .errors import BenchmarkError
from .fixture import fixture_input_sha256, validate_fixture
from .normalize import normalize_file
from .parity import first_difference
from .profiling import aggregate_profile_events
from .specs import validate_trade_surface
from .state_trace import first_trace_difference, trace_summary

REFERENCE_VERSION = "2026.5.1"
REFERENCE_IMAGE = "freqtradeorg/freqtrade"
REFERENCE_INDEX_DIGEST = "sha256:d47d7053dc07eca2ace20385575143090ba88621007e5e8b76052dca6038799a"
REFERENCE_PLATFORM_DIGEST = (
    "sha256:bc5b7276118a8539d09ea797cb32c198d029a805815a29c6d27d5f610a3e0b6b"
)
REFERENCE_PLATFORM = "linux/amd64"
REFERENCE_IMAGE_REF = f"{REFERENCE_IMAGE}@{REFERENCE_PLATFORM_DIGEST}"
REFERENCE_BLAKE3_VERSION = "1.0.9"
REFERENCE_TRACER_VERSION = "1.0.0"

_CGROUP_CAPTURE_SCRIPT = """\
freqtrade "$@"
status=$?
if [ -r /sys/fs/cgroup/memory.peak ]; then
  cat /sys/fs/cgroup/memory.peak > /output/container-memory-peak.txt
elif [ -r /sys/fs/cgroup/memory/memory.max_usage_in_bytes ]; then
  cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes > /output/container-memory-peak.txt
fi
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  cat /sys/fs/cgroup/cpu.stat > /output/container-cpu.stat
fi
exit "$status"
"""


def run_reference_fixture(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    trace_mode: str = "off",
    profile: bool = True,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one sealed fixture in the pinned container and compare official evidence."""
    if trace_mode not in {"off", "hash", "full"}:
        raise BenchmarkError("trace_mode must be one of: off, hash, full")

    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    _validate_reference_pin(manifest)
    market_snapshot = _one_input(manifest["inputs"], "market_metadata")
    fixture_root = manifest_file.parent
    output = Path(output_directory).resolve()
    _initialize_output_directory(output)
    project_root = _project_root()
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)

    dependency_directory: Path | None = None
    if trace_mode != "off":
        dependency_directory = ensure_reference_dependencies(
            project_root=project_root,
            docker_config=docker_config,
        )

    stdout_path = output / "stdout.log"
    stderr_path = output / "stderr.log"
    profile_path = output / "profile.jsonl"
    trace_path = output / "state-trace.nfitrace"
    docker_argv = build_reference_docker_command(
        manifest,
        fixture_root=fixture_root,
        output_directory=output,
        project_root=project_root,
        dependency_directory=dependency_directory,
        trace_mode=trace_mode,
        profile=profile,
        docker_config=docker_config,
        market_snapshot=market_snapshot,
    )

    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            completed = subprocess.run(
                docker_argv,
                cwd=project_root,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
    ended_at = datetime.now(UTC)

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "fixture_id": manifest["fixture_id"],
        "manifest_path": str(manifest_file),
        "reference": {
            "version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_index_digest": REFERENCE_INDEX_DIGEST,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "network": "none",
            "tracer_version": REFERENCE_TRACER_VERSION if trace_mode != "off" else None,
        },
        "trace_mode": trace_mode,
        "profile_enabled": profile,
        "started_at": _utc_string(started_at),
        "ended_at": _utc_string(ended_at),
        "wall_time_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "container_peak_memory_bytes": _read_nonnegative_integer(
            output / "container-memory-peak.txt"
        ),
        "container_cpu": _read_cpu_stat(output / "container-cpu.stat"),
        "stdout": _file_record(stdout_path),
        "stderr": _file_record(stderr_path),
        "result": None,
        "trade_surface": None,
        "profile": None,
        "state_trace": None,
        "parity": {
            "trade_surface": {"equal": False, "difference": None},
            "state_trace": None,
        },
        "complete": False,
    }

    if exit_code == 0:
        result_zip = _find_result_zip(output)
        surface_path = output / "trade-surface.json"
        surface = normalize_file(
            result_zip,
            surface_path,
            strategy=manifest["freqtrade"]["strategy"],
            surface_version="2",
        )
        expected_surface_path = (
            fixture_root / manifest["artifacts"]["trade_surface"]["path"]
        ).resolve()
        expected_surface = read_json(expected_surface_path)
        validate_trade_surface(expected_surface)
        difference = first_difference(expected_surface, surface)
        report["result"] = _file_record(result_zip)
        report["trade_surface"] = _file_record(surface_path)
        report["parity"]["trade_surface"] = {
            "equal": difference is None,
            "difference": _parity_difference_record(difference),
        }

        if profile:
            report["profile"] = (
                aggregate_profile_events(profile_path)
                if profile_path.is_file()
                else {
                    "schema_version": "1.0.0",
                    "phases": {},
                    "missing_phases": list(manifest["measurement"]["required_profile_phases"]),
                }
            )

        if trace_mode != "off":
            actual_trace_summary = trace_summary(trace_path)
            expected_trace_path = (
                fixture_root / manifest["artifacts"]["state_trace"]["path"]
            ).resolve()
            trace_difference = first_trace_difference(expected_trace_path, trace_path)
            report["state_trace"] = {
                **_file_record(trace_path),
                "summary": actual_trace_summary,
            }
            report["parity"]["state_trace"] = {
                "equal": trace_difference is None,
                "difference": _trace_difference_record(trace_difference),
            }

        profile_complete = not profile or not report["profile"]["missing_phases"]
        trace_complete = trace_mode == "off" or bool(report["parity"]["state_trace"]["equal"])
        report["complete"] = (
            bool(report["parity"]["trade_surface"]["equal"]) and profile_complete and trace_complete
        )

    write_json(output / "run.json", report)
    return report


def build_reference_docker_command(
    manifest: dict[str, Any],
    *,
    fixture_root: Path,
    output_directory: Path,
    project_root: Path,
    dependency_directory: Path | None,
    trace_mode: str,
    profile: bool,
    docker_config: Path,
    market_snapshot: dict[str, Any],
) -> list[str]:
    """Build argv without shell interpolation so fixture values cannot become commands."""
    freqtrade_args = _reference_freqtrade_args(manifest["freqtrade"]["command"])
    command = [
        _docker_executable(),
        "--config",
        str(docker_config),
        "run",
        "--rm",
        "--platform",
        REFERENCE_PLATFORM,
        "--network",
        "none",
        "--workdir",
        "/fixture",
        "--volume",
        f"{fixture_root}:/fixture:ro",
        "--volume",
        f"{output_directory}:/output",
        "--volume",
        f"{project_root}:/project:ro",
        "--env",
        "PYTHONPATH=/project/benchmarks/reference/tracer:/project/python"
        + (":/reference-deps" if dependency_directory is not None else ""),
        "--env",
        f"NFI_MARKET_SNAPSHOT_PATH=/fixture/{market_snapshot['path']}",
    ]
    if dependency_directory is not None:
        command.extend(["--volume", f"{dependency_directory}:/reference-deps:ro"])
    if profile:
        command.extend(["--env", "NFI_BTE_PROFILE_EVENTS=/output/profile.jsonl"])
    if trace_mode != "off":
        strategy = _one_input(manifest["inputs"], "strategy")
        config = _one_input(manifest["inputs"], "config")
        command.extend(
            [
                "--env",
                "NFI_TRACE_PATH=/output/state-trace.nfitrace",
                "--env",
                f"NFI_TRACE_RUN_ID={manifest['fixture_id']}",
                "--env",
                f"NFI_TRACE_INPUT_SHA256={fixture_input_sha256(manifest['inputs'])}",
                "--env",
                f"NFI_TRACE_STRATEGY_SHA256={strategy['sha256']}",
                "--env",
                f"NFI_TRACE_PROFILE_SHA256={config['sha256']}",
                "--env",
                f"NFI_TRACE_INCLUDE_STATE={'1' if trace_mode == 'full' else '0'}",
            ]
        )
    command.extend(
        [
            "--entrypoint",
            "/bin/sh",
            REFERENCE_IMAGE_REF,
            "-c",
            _CGROUP_CAPTURE_SCRIPT,
            "nfi-reference",
            *freqtrade_args,
        ]
    )
    return command


def capture_reference_markets(
    manifest_path: str | Path,
    destination: str | Path,
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Capture the exact CCXT market state used by the pinned online reference."""
    manifest_file = Path(manifest_path).resolve()
    manifest = validate_fixture(manifest_file)
    _validate_reference_pin(manifest)
    target = Path(destination).resolve()
    if target.exists():
        raise BenchmarkError(f"market snapshot destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    project_root = _project_root()
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)

    with tempfile.TemporaryDirectory(prefix="nfi-market-", dir=target.parent) as temporary:
        output = Path(temporary)
        (output / "user_data").mkdir()
        command = [
            _docker_executable(),
            "--config",
            str(docker_config),
            "run",
            "--rm",
            "--platform",
            REFERENCE_PLATFORM,
            "--workdir",
            "/fixture",
            "--volume",
            f"{manifest_file.parent}:/fixture:ro",
            "--volume",
            f"{output}:/output",
            "--volume",
            f"{project_root}:/project:ro",
            "--env",
            "PYTHONPATH=/project/benchmarks/reference/tracer:/project/python",
            "--env",
            "NFI_MARKET_CAPTURE_PATH=/output/market-snapshot.json",
            "--entrypoint",
            "/bin/sh",
            REFERENCE_IMAGE_REF,
            "-c",
            _CGROUP_CAPTURE_SCRIPT,
            "nfi-market-capture",
            *_reference_freqtrade_args(manifest["freqtrade"]["command"]),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise BenchmarkError("timed out while capturing reference markets") from exc
        captured = output / "market-snapshot.json"
        if completed.returncode != 0 or not captured.is_file():
            raise BenchmarkError(
                "failed to capture reference markets: "
                f"{completed.stderr[-2000:].strip() or completed.stdout[-2000:].strip()}"
            )
        document = read_json(captured)
        if (
            document.get("schema_version") != "1.0.0"
            or document.get("freqtrade_version") != REFERENCE_VERSION
        ):
            raise BenchmarkError("captured market snapshot has an invalid identity")
        captured.replace(target)
    return _file_record(target)


def ensure_docker_config() -> Path:
    """Use an isolated credential-free Docker config for public reference images."""
    configured = os.environ.get("NFI_BTE_DOCKER_CONFIG")
    directory = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(tempfile.gettempdir()) / "nfi-bte-docker-anonymous"
    )
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    expected = {"auths": {"https://index.docker.io/v1/": {}}, "credsStore": ""}
    if not config_path.is_file() or read_json(config_path) != expected:
        config_path.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return directory


def ensure_reference_image(*, docker_config: Path) -> None:
    """Verify or pull the exact platform manifest, then check its content ID."""
    inspect = _run_docker(
        docker_config,
        ["image", "inspect", REFERENCE_IMAGE_REF, "--format", "{{.Id}}"],
    )
    if inspect.returncode != 0:
        pull = _run_docker(
            docker_config,
            ["pull", "--platform", REFERENCE_PLATFORM, REFERENCE_IMAGE_REF],
        )
        if pull.returncode != 0:
            raise BenchmarkError(
                "failed to pull pinned Freqtrade image: "
                f"{pull.stderr.strip() or pull.stdout.strip()}"
            )
        inspect = _run_docker(
            docker_config,
            ["image", "inspect", REFERENCE_IMAGE_REF, "--format", "{{.Id}}"],
        )
    image_id = inspect.stdout.strip()
    if inspect.returncode != 0 or image_id != REFERENCE_PLATFORM_DIGEST:
        raise BenchmarkError(
            "pinned Freqtrade image identity mismatch: "
            f"expected {REFERENCE_PLATFORM_DIGEST}, found {image_id or '<missing>'}"
        )


def ensure_reference_dependencies(*, project_root: Path, docker_config: Path) -> Path:
    """Build an ignored Linux wheel target used only by the reference tracer."""
    dependency_directory = project_root / "artifacts" / "docker" / "reference-deps"
    marker = dependency_directory / "blake3" / "blake3.cpython-314-x86_64-linux-gnu.so"
    if marker.is_file():
        return dependency_directory
    dependency_directory.mkdir(parents=True, exist_ok=True)
    command = [
        "run",
        "--rm",
        "--platform",
        REFERENCE_PLATFORM,
        "--volume",
        f"{dependency_directory}:/reference-deps",
        "--entrypoint",
        "python",
        REFERENCE_IMAGE_REF,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        "--target",
        "/reference-deps",
        f"blake3=={REFERENCE_BLAKE3_VERSION}",
    ]
    completed = _run_docker(docker_config, command)
    if completed.returncode != 0 or not marker.is_file():
        raise BenchmarkError(
            "failed to prepare pinned reference tracer dependency: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return dependency_directory


def _reference_freqtrade_args(command: list[str]) -> list[str]:
    args = list(command)
    if args[:1] == ["freqtrade"]:
        args = args[1:]
    if not args:
        raise BenchmarkError("fixture Freqtrade command is empty")
    args = _remove_option(args, "--export-filename")
    args = _remove_option(args, "--backtest-directory")
    args = _remove_option(args, "--userdir")
    args.extend(
        [
            "--userdir",
            "/output/user_data",
            "--backtest-directory",
            "/output",
        ]
    )
    return args


def _remove_option(args: list[str], option: str) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == option:
            if index + 1 >= len(args):
                raise BenchmarkError(f"fixture command option {option} has no value")
            index += 2
            continue
        if item.startswith(f"{option}="):
            index += 1
            continue
        result.append(item)
        index += 1
    return result


def _validate_reference_pin(manifest: dict[str, Any]) -> None:
    actual = manifest["freqtrade"]
    expected = {
        "version": REFERENCE_VERSION,
        "image": REFERENCE_IMAGE,
        "image_index_digest": REFERENCE_INDEX_DIGEST,
        "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
        "platform": REFERENCE_PLATFORM,
        "tracer_version": REFERENCE_TRACER_VERSION,
    }
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            raise BenchmarkError(
                f"fixture reference pin {key} differs: "
                f"expected {expected_value!r}, actual {actual.get(key)!r}"
            )


def _initialize_output_directory(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"reference output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "user_data").mkdir()


def _find_result_zip(output: Path) -> Path:
    candidates = sorted(output.glob("backtest-result-*.zip"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise BenchmarkError(f"expected exactly one official result ZIP in {output}; found {names}")
    return candidates[0]


def _one_input(inputs: list[dict[str, Any]], role: str) -> dict[str, Any]:
    matches = [item for item in inputs if item["role"] == role]
    if len(matches) != 1:
        raise BenchmarkError(f"fixture requires exactly one {role!r} input")
    return matches[0]


def _file_record(path: Path) -> dict[str, Any]:
    from .fixture import sha256_file

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _read_nonnegative_integer(path: Path) -> int | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _read_cpu_stat(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    result: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result or None


def _parity_difference_record(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
    }


def _trace_difference_record(difference: Any) -> dict[str, Any] | None:
    if difference is None:
        return None
    return {
        "sequence": difference.sequence,
        "path": difference.path,
        "expected": difference.expected,
        "actual": difference.actual,
        "reason": difference.reason,
        "event_key": difference.event_key,
    }


def _run_docker(docker_config: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [_docker_executable(), "--config", str(docker_config), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise BenchmarkError(f"cannot execute Docker: {exc}") from exc


def _docker_executable() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise BenchmarkError("Docker CLI is not installed or not on PATH")
    return executable


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_string(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
