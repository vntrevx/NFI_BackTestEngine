"""Reference container execution and dependency preparation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..canonical import read_json
from ..docker_runtime import managed_docker_run, run_managed_container
from ..errors import BenchmarkError
from ..fixture import fixture_input_sha256, validate_fixture
from ..reference_assets import reference_package_root, reference_tracer_root
from .contracts import (
    _BINANCE_TIER_EXPORT,
    _CGROUP_CAPTURE_SCRIPT,
    REFERENCE_BLAKE3_VERSION,
    REFERENCE_DOCKER_IMAGE_IDS,
    REFERENCE_IMAGE,
    REFERENCE_IMAGE_REF,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
    SUPPORTED_REFERENCE_TRACER_VERSIONS,
)
from .storage import _file_record, _one_input


def build_reference_docker_command(
    manifest: dict[str, Any],
    *,
    fixture_root: Path,
    output_directory: Path,
    dependency_directory: Path | None,
    trace_mode: str,
    profile: bool,
    docker_config: Path,
    market_snapshot: dict[str, Any],
    run_prefix: list[str] | None = None,
) -> list[str]:
    """Build argv without shell interpolation so fixture values cannot become commands."""
    freqtrade_args = _reference_freqtrade_args(manifest["freqtrade"]["command"])
    tracer_root = reference_tracer_root()
    package_root = reference_package_root()
    command = (
        list(run_prefix)
        if run_prefix is not None
        else [
            _docker_executable(),
            "--config",
            str(docker_config),
            "run",
            "--rm",
        ]
    )
    command.extend(
        [
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
            f"{tracer_root}:/nfi-reference-tracer:ro",
            "--volume",
            f"{package_root}:/nfi-python/nfi_backtest_engine:ro",
            "--env",
            "PYTHONPATH=/nfi-reference-tracer:/nfi-python"
            + (":/reference-deps" if dependency_directory is not None else ""),
            "--env",
            f"NFI_MARKET_SNAPSHOT_PATH=/fixture/{market_snapshot['path']}",
        ]
    )
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
    tracer_root = reference_tracer_root()
    package_root = reference_package_root()
    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)

    with tempfile.TemporaryDirectory(prefix="nfi-market-", dir=target.parent) as temporary:
        output = Path(temporary)
        (output / "user_data").mkdir()
        try:
            with managed_docker_run(
                docker_config=docker_config,
                role="market-capture",
            ) as lease:
                command = [
                    *lease["command_prefix"],
                    "--platform",
                    REFERENCE_PLATFORM,
                    "--workdir",
                    "/fixture",
                    "--volume",
                    f"{manifest_file.parent}:/fixture:ro",
                    "--volume",
                    f"{output}:/output",
                    "--volume",
                    f"{tracer_root}:/nfi-reference-tracer:ro",
                    "--volume",
                    f"{package_root}:/nfi-python/nfi_backtest_engine:ro",
                    "--env",
                    "PYTHONPATH=/nfi-reference-tracer:/nfi-python",
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
        except OSError as exc:
            raise BenchmarkError(f"cannot execute Docker: {exc}") from exc
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


def load_reference_leverage_tiers(
    pairs: list[str],
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Load exact dry-run leverage tiers from the pinned Freqtrade image.

    This is deliberately a Docker oracle operation instead of a CCXT network call.
    Binance's leverage-bracket API requires credentials, while official Freqtrade
    backtesting reads its bundled snapshot. The returned source identity is stored
    beside the normalized tiers so a later run can prove which table it used.
    """
    normalized_pairs = list(dict.fromkeys(pairs))
    if not normalized_pairs:
        raise BenchmarkError("at least one futures pair is required for leverage tiers")
    if any(not isinstance(pair, str) or not pair or ":" not in pair for pair in normalized_pairs):
        raise BenchmarkError(
            "reference leverage tiers require canonical futures pairs such as BTC/USDT:USDT"
        )

    docker_config = ensure_docker_config()
    ensure_reference_image(docker_config=docker_config)
    completed, resources = run_managed_container(
        [
            "--platform",
            REFERENCE_PLATFORM,
            "--network",
            "none",
            "--entrypoint",
            "python",
            REFERENCE_IMAGE_REF,
            "-c",
            _BINANCE_TIER_EXPORT,
            *normalized_pairs,
        ],
        docker_config=docker_config,
        role="leverage-tier-capture",
        capture_output=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = completed.stderr[-2000:].strip() or completed.stdout[-2000:].strip()
        raise BenchmarkError(f"failed to load pinned Freqtrade leverage tiers: {detail}")
    try:
        tiers = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("pinned Freqtrade returned invalid leverage-tier JSON") from exc
    if not isinstance(tiers, dict) or set(tiers) != set(normalized_pairs):
        raise BenchmarkError("pinned Freqtrade returned an incomplete leverage-tier table")
    return {
        "tiers": tiers,
        "source": {
            "kind": "freqtrade-bundled-binance-leverage-tiers",
            "freqtrade_version": REFERENCE_VERSION,
            "image": REFERENCE_IMAGE,
            "image_platform_digest": REFERENCE_PLATFORM_DIGEST,
            "platform": REFERENCE_PLATFORM,
            "docker_policy": resources["policy"],
        },
    }


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
    if inspect.returncode != 0 or image_id not in REFERENCE_DOCKER_IMAGE_IDS:
        expected = ", ".join(sorted(REFERENCE_DOCKER_IMAGE_IDS))
        raise BenchmarkError(
            "pinned Freqtrade image identity mismatch: "
            f"expected one of [{expected}], found {image_id or '<missing>'}"
        )


def ensure_reference_dependencies(*, project_root: Path, docker_config: Path) -> Path:
    """Build an ignored Linux wheel target used only by the reference tracer."""
    dependency_directory = project_root / "artifacts" / "docker" / "reference-deps"
    marker = dependency_directory / "blake3" / "blake3.cpython-314-x86_64-linux-gnu.so"
    if marker.is_file():
        return dependency_directory
    dependency_directory.mkdir(parents=True, exist_ok=True)
    mount_owner = dependency_directory.stat()
    arguments = [
        "--platform",
        REFERENCE_PLATFORM,
        "--user",
        f"{mount_owner.st_uid}:{mount_owner.st_gid}",
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
    completed, _resources = run_managed_container(
        arguments,
        docker_config=docker_config,
        role="reference-dependencies",
        capture_output=True,
    )
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
    }
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            raise BenchmarkError(
                f"fixture reference pin {key} differs: "
                f"expected {expected_value!r}, actual {actual.get(key)!r}"
            )
    tracer_version = actual.get("tracer_version")
    if tracer_version not in SUPPORTED_REFERENCE_TRACER_VERSIONS:
        raise BenchmarkError(
            "fixture reference pin tracer_version differs: "
            f"supported {sorted(SUPPORTED_REFERENCE_TRACER_VERSIONS)!r}, "
            f"actual {tracer_version!r}"
        )


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
    # Command construction is intentionally testable on machines without
    # Docker (for example, macOS CI). Real execution still reports a clear
    # OSError through the guarded subprocess boundary.
    return shutil.which("docker") or "docker"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]
