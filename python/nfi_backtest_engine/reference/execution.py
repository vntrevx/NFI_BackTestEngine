"""Reference container execution and dependency preparation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from ..canonical import read_json
from ..docker_environment import docker_subprocess_environment
from ..docker_runtime import (
    docker_bind_owner_arguments,
    docker_root_with_bind_owner_arguments,
    managed_docker_run,
    run_managed_container,
    validate_final_managed_command,
    validate_managed_run_prefix,
)
from ..errors import BenchmarkError
from ..fixture import fixture_input_sha256, validate_fixture
from ..reference_assets import reference_package_root, reference_tracer_root
from .contracts import (
    _BINANCE_TIER_EXPORT,
    _CGROUP_CAPTURE_SCRIPT,
    REFERENCE_DEPENDENCY_WHEELS,
    REFERENCE_DOCKER_IMAGE_IDS,
    REFERENCE_IMAGE,
    REFERENCE_IMAGE_REF,
    REFERENCE_INDEX_DIGEST,
    REFERENCE_PLATFORM,
    REFERENCE_PLATFORM_DIGEST,
    REFERENCE_VERSION,
    SUPPORTED_REFERENCE_TRACER_VERSIONS,
)
from .dependency_seal import safe_member, validate_archive_bounds, validate_inventory
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
    runtime_volume: str | None = None,
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
            "--cidfile",
            str(docker_config / "reference-command.cid"),
            "--label",
            "io.nfi-backtest-engine.managed=true",
            "--label",
            "io.nfi-backtest-engine.role=reference-command",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev",
            "--tmpfs",
            "/nfi-deps:rw,exec,nosuid,nodev",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "512",
            "--ulimit",
            "nofile=4096:4096",
            "--memory",
            str(1024**3),
            "--memory-swap",
            str(1024**3),
        ]
    )
    validate_managed_run_prefix(command)
    command.extend(
        [
            "--platform",
            REFERENCE_PLATFORM,
            "--network",
            "none",
            *docker_root_with_bind_owner_arguments(output_directory),
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
            + (":/nfi-deps/site" if dependency_directory is not None else "")
            + (
                ":/nfi-runtime/lib/python3.14/site-packages:/freqtrade"
                if runtime_volume is not None
                else ":/home/ftuser/.local/lib/python3.14/site-packages:/freqtrade"
            ),
            "--env",
            f"NFI_MARKET_SNAPSHOT_PATH=/fixture/{market_snapshot['path']}",
        ]
    )
    if dependency_directory is not None:
        command.extend(["--volume", f"{dependency_directory}:/reference-deps:ro"])
    if runtime_volume is not None:
        command.extend(
            [
                "--mount",
                f"type=volume,source={runtime_volume},target=/nfi-runtime,readonly",
            ]
        )
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
    owner = output_directory.stat()
    expected_volumes = [
        f"{fixture_root}:/fixture:ro",
        f"{output_directory}:/output",
        f"{tracer_root}:/nfi-reference-tracer:ro",
        f"{package_root}:/nfi-python/nfi_backtest_engine:ro",
    ]
    if dependency_directory is not None:
        expected_volumes.append(f"{dependency_directory}:/reference-deps:ro")
    expected_mounts = (
        [f"type=volume,source={runtime_volume},target=/nfi-runtime,readonly"]
        if runtime_volume is not None
        else []
    )
    expected_environment = [
        f"NFI_BIND_UID={owner.st_uid}",
        f"NFI_BIND_GID={owner.st_gid}",
        "PYTHONPATH=/nfi-reference-tracer:/nfi-python"
        + (":/nfi-deps/site" if dependency_directory is not None else "")
        + (
            ":/nfi-runtime/lib/python3.14/site-packages:/freqtrade"
            if runtime_volume is not None
            else ":/home/ftuser/.local/lib/python3.14/site-packages:/freqtrade"
        ),
        f"NFI_MARKET_SNAPSHOT_PATH=/fixture/{market_snapshot['path']}",
    ]
    if profile:
        expected_environment.append("NFI_BTE_PROFILE_EVENTS=/output/profile.jsonl")
    if trace_mode != "off":
        strategy = _one_input(manifest["inputs"], "strategy")
        config = _one_input(manifest["inputs"], "config")
        expected_environment.extend(
            [
                "NFI_TRACE_PATH=/output/state-trace.nfitrace",
                f"NFI_TRACE_RUN_ID={manifest['fixture_id']}",
                f"NFI_TRACE_INPUT_SHA256={fixture_input_sha256(manifest['inputs'])}",
                f"NFI_TRACE_STRATEGY_SHA256={strategy['sha256']}",
                f"NFI_TRACE_PROFILE_SHA256={config['sha256']}",
                f"NFI_TRACE_INCLUDE_STATE={'1' if trace_mode == 'full' else '0'}",
            ]
        )
    validate_final_managed_command(
        command,
        image=REFERENCE_IMAGE_REF,
        platform=REFERENCE_PLATFORM,
        user=f"{owner.st_uid}:{owner.st_gid}",
        workdir="/fixture",
        entrypoint="/bin/sh",
        volumes=expected_volumes,
        mounts=expected_mounts,
        environment=expected_environment,
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
            with reference_runtime_volume(docker_config) as runtime_volume, managed_docker_run(
                docker_config=docker_config,
                role="market-capture",
            ) as lease:
                command = [
                    *lease["command_prefix"],
                    "--platform",
                    REFERENCE_PLATFORM,
                    *docker_root_with_bind_owner_arguments(output),
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
                    "PYTHONPATH=/nfi-reference-tracer:/nfi-python:"
                    "/nfi-runtime/lib/python3.14/site-packages:/freqtrade",
                    "--mount",
                    f"type=volume,source={runtime_volume},target=/nfi-runtime,readonly",
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
                    env=docker_subprocess_environment(),
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


@contextmanager
def reference_dependency_lock(project_root: Path) -> Iterator[None]:
    """Serialize dependency builds and every use across processes."""
    build_root = project_root / "artifacts" / "docker"
    build_root.mkdir(parents=True, exist_ok=True)
    with (build_root / "reference-dependencies.lock").open("a+b") as handle:
        _lock_dependency_file(handle)
        try:
            yield
        finally:
            _unlock_dependency_file(handle)


@contextmanager
def reference_runtime_volume(docker_config: Path) -> Iterator[str]:
    """Expose the pinned image user runtime at a universally traversable mount."""
    token = uuid.uuid4().hex
    volume = f"nfi-reference-runtime-{token}"
    helper = f"nfi-reference-runtime-init-{token}"
    created = _run_docker(
        docker_config,
        [
            "volume",
            "create",
            "--label",
            "io.nfi-backtest-engine.managed=true",
            "--label",
            "io.nfi-backtest-engine.role=reference-runtime",
            volume,
        ],
    )
    if created.returncode != 0:
        raise BenchmarkError("cannot create the private reference runtime volume")
    try:
        initialized = _run_docker(
            docker_config,
            [
                "create",
                "--name",
                helper,
                "--platform",
                REFERENCE_PLATFORM,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--label",
                "io.nfi-backtest-engine.managed=true",
                "--label",
                "io.nfi-backtest-engine.role=reference-runtime-init",
                "--mount",
                f"type=volume,source={volume},target=/home/ftuser/.local",
                REFERENCE_IMAGE_REF,
            ],
        )
        if initialized.returncode != 0:
            raise BenchmarkError("cannot initialize the private reference runtime volume")
        _run_docker(docker_config, ["container", "rm", helper])
        yield volume
    finally:
        _run_docker(docker_config, ["container", "rm", "--force", helper])
        _run_docker(docker_config, ["volume", "rm", "--force", volume])


def ensure_reference_dependencies(*, project_root: Path, docker_config: Path) -> Path:
    """Materialize and verify the complete hash-pinned tracer dependency inventory."""
    with reference_dependency_lock(project_root):
        return _ensure_reference_dependencies_unlocked(
            project_root=project_root,
            docker_config=docker_config,
        )


def _ensure_reference_dependencies_unlocked(*, project_root: Path, docker_config: Path) -> Path:
    build_root = project_root / "artifacts" / "docker"
    dependency_directory = build_root / "reference-deps"
    if _reference_dependency_inventory_is_valid(dependency_directory):
        return dependency_directory

    build_root.mkdir(parents=True, exist_ok=True)
    for interrupted_pattern in (
        ".reference-deps.build-*",
        ".reference-deps.replaced-*",
    ):
        for interrupted in build_root.glob(interrupted_pattern):
            if interrupted.is_dir():
                shutil.rmtree(interrupted)
    staging = Path(tempfile.mkdtemp(prefix=".reference-deps.build-", dir=build_root))
    requirements = staging.parent / f".{staging.name}.requirements.txt"
    requirements.write_text(
        "".join(
            f"{url} --hash=sha256:{digest}\n"
            for _name, url, digest in REFERENCE_DEPENDENCY_WHEELS
        ),
        encoding="utf-8",
    )
    try:
        (staging / ".wheels").mkdir()
        arguments = [
            "--platform",
            REFERENCE_PLATFORM,
            *docker_bind_owner_arguments(staging),
            "--volume",
            f"{staging}:/reference-deps",
            "--volume",
            f"{requirements}:/nfi-requirements.txt:ro",
            "--entrypoint",
            "python",
            REFERENCE_IMAGE_REF,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--dest",
            "/reference-deps/.wheels",
            "--requirement",
            "/nfi-requirements.txt",
        ]
        completed, _resources = run_managed_container(
            arguments,
            docker_config=docker_config,
            role="reference-dependencies",
            capture_output=True,
        )
        if completed.returncode != 0:
            raise BenchmarkError(
                "failed to download hash-pinned reference tracer dependencies: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        _extract_reference_dependency_wheels(staging)
        validate_reference_dependencies(staging)

        displaced = build_root / f".reference-deps.replaced-{uuid.uuid4().hex}"
        if dependency_directory.exists():
            dependency_directory.replace(displaced)
        try:
            staging.replace(dependency_directory)
        except OSError:
            if displaced.exists() and not dependency_directory.exists():
                displaced.replace(dependency_directory)
            raise
        if displaced.exists():
            shutil.rmtree(displaced)
        return dependency_directory
    finally:
        requirements.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)


def _lock_dependency_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_dependency_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_reference_dependencies(dependency_directory: Path) -> None:
    """Fail unless every cached byte exactly matches every pinned wheel member."""
    if not _reference_dependency_inventory_is_valid(dependency_directory):
        raise BenchmarkError("reference dependency inventory is incomplete or untrusted")


def _reference_dependency_inventory_is_valid(dependency_directory: Path) -> bool:
    if not dependency_directory.is_dir():
        return False
    wheels = tuple(
        (wheel_name, wheel_sha256)
        for wheel_name, _url, wheel_sha256 in REFERENCE_DEPENDENCY_WHEELS
    )
    try:
        validate_inventory(dependency_directory, wheels)
    except (OSError, ValueError):
        return False
    return True


def _extract_reference_dependency_wheels(dependency_directory: Path) -> None:
    for wheel_name, _url, wheel_sha256 in REFERENCE_DEPENDENCY_WHEELS:
        wheel = dependency_directory / ".wheels" / wheel_name
        if not wheel.is_file() or _sha256_file(wheel) != wheel_sha256:
            raise BenchmarkError(f"hash-pinned reference wheel is missing or changed: {wheel_name}")
        try:
            with zipfile.ZipFile(wheel) as archive:
                members = archive.infolist()
                validate_archive_bounds(members)
                for member in members:
                    relative = safe_member(member)
                    if relative is None:
                        continue
                    target = dependency_directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise BenchmarkError(f"cannot extract pinned reference wheel: {wheel_name}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            env=docker_subprocess_environment(),
        )
    except OSError as exc:
        raise BenchmarkError(f"cannot execute Docker: {exc}") from exc


def _docker_executable() -> str:
    # Command construction is intentionally testable on machines without
    # Docker (for example, macOS CI). Real execution still reports a clear
    # OSError through the guarded subprocess boundary.
    return shutil.which("docker") or "docker"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]
