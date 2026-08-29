from __future__ import annotations

import hashlib
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from nfi_backtest_engine import reference_runtime
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import BenchmarkError, TraceError
from nfi_backtest_engine.reference import dependency_seal
from nfi_backtest_engine.reference import execution as reference_execution
from nfi_backtest_engine.reference_runtime import (
    REFERENCE_CONFIG_DIGEST,
    REFERENCE_DOCKER_IMAGE_IDS,
    REFERENCE_IMAGE_REF,
    REFERENCE_PLATFORM_DIGEST,
    build_reference_docker_command,
)
from nfi_backtest_engine.reference_tracer.nfi_reference_trace import (
    REFERENCE_STATE_SCHEMA_VERSION,
)
from nfi_backtest_engine.state_trace import StateTraceWriter

ROOT = Path(__file__).parents[1]
MANIFEST = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
    / "manifest.json"
)


def test_reference_runtime_resolves_the_repository_root() -> None:
    assert reference_execution._project_root() == ROOT  # pyright: ignore[reportPrivateUsage]


def test_reference_image_identity_accepts_both_docker_store_projections() -> None:
    assert {
        REFERENCE_PLATFORM_DIGEST,
        REFERENCE_CONFIG_DIGEST,
    } == REFERENCE_DOCKER_IMAGE_IDS
    assert "sha256:" + "0" * 64 not in REFERENCE_DOCKER_IMAGE_IDS


@pytest.mark.parametrize("image_id", sorted(REFERENCE_DOCKER_IMAGE_IDS))
def test_reference_image_check_accepts_each_docker_store_projection(
    image_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        reference_execution,
        "_run_docker",
        lambda *_args, **_kwargs: CompletedProcess([], 0, image_id + "\n", ""),
    )

    reference_execution.ensure_reference_image(docker_config=tmp_path)


def test_reference_image_check_rejects_an_unbound_config_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        reference_execution,
        "_run_docker",
        lambda *_args, **_kwargs: CompletedProcess(
            [],
            0,
            "sha256:" + "0" * 64 + "\n",
            "",
        ),
    )

    with pytest.raises(BenchmarkError, match="identity mismatch"):
        reference_execution.ensure_reference_image(docker_config=tmp_path)


def _sealed_dependency_cache(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> tuple[Path, Path]:
    wheel_name = "blake3-1.0.9-cp314-cp314-manylinux_x86_64.whl"
    wheel_payloads = {
        "blake3/__init__.py": b"from .blake3 import blake3\n",
        "blake3/blake3.cpython-314-x86_64-linux-gnu.so": b"native-extension",
        "blake3-1.0.9.dist-info/METADATA": b"Name: blake3\nVersion: 1.0.9\n",
    }
    wheel = root / ".wheels" / wheel_name
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in wheel_payloads.items():
            archive.writestr(name, payload)
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(
        reference_execution,
        "REFERENCE_DEPENDENCY_WHEELS",
        ((wheel_name, "https://example.invalid/" + wheel_name, wheel_sha256),),
    )
    for name, payload in wheel_payloads.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root, root / "blake3/blake3.cpython-314-x86_64-linux-gnu.so"


def test_reference_dependency_cache_reuses_the_complete_wheel_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "artifacts/docker/reference-deps"
    _sealed_dependency_cache(monkeypatch, dependency_root)
    monkeypatch.setattr(
        reference_execution,
        "run_managed_container",
        lambda *_args, **_kwargs: pytest.fail("valid cache must be reused"),
    )

    assert reference_execution.ensure_reference_dependencies(
        project_root=tmp_path,
        docker_config=tmp_path / "docker-config",
    ) == dependency_root


def test_replaced_native_dependency_forces_a_fresh_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "artifacts/docker/reference-deps"
    _, native = _sealed_dependency_cache(monkeypatch, dependency_root)
    native.write_bytes(b"substituted-native-extension")
    builds: list[list[str]] = []

    def rebuild(arguments: list[str], **_kwargs):
        builds.append(arguments)
        staging = Path(arguments[arguments.index("--volume") + 1].split(":", 1)[0])
        _sealed_dependency_cache(monkeypatch, staging)
        return CompletedProcess(arguments, 0, "", ""), {}

    monkeypatch.setattr(reference_execution, "run_managed_container", rebuild)

    result = reference_execution.ensure_reference_dependencies(
        project_root=tmp_path,
        docker_config=tmp_path / "docker-config",
    )

    assert len(builds) == 1
    assert (result / native.relative_to(dependency_root)).read_bytes() == b"native-extension"


def test_incomplete_dependency_inventory_forces_a_fresh_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "artifacts/docker/reference-deps"
    _sealed_dependency_cache(monkeypatch, dependency_root)
    (dependency_root / "blake3/__init__.py").unlink()
    builds = 0

    def rebuild(arguments: list[str], **_kwargs):
        nonlocal builds
        builds += 1
        staging = Path(arguments[arguments.index("--volume") + 1].split(":", 1)[0])
        _sealed_dependency_cache(monkeypatch, staging)
        return CompletedProcess(arguments, 0, "", ""), {}

    monkeypatch.setattr(reference_execution, "run_managed_container", rebuild)
    reference_execution.ensure_reference_dependencies(
        project_root=tmp_path,
        docker_config=tmp_path / "docker-config",
    )

    assert builds == 1


def test_interrupted_dependency_build_leaves_no_reusable_partial_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        reference_execution,
        "REFERENCE_DEPENDENCY_WHEELS",
        (("dependency.whl", "https://example.invalid/dependency.whl", "0" * 64),),
    )
    stale_replacement = tmp_path / "artifacts/docker/.reference-deps.replaced-stale"
    stale_replacement.mkdir(parents=True)
    (stale_replacement / "partial.so").write_bytes(b"stale")

    def interrupt(arguments: list[str], **_kwargs):
        staging = Path(arguments[arguments.index("--volume") + 1].split(":", 1)[0])
        (staging / "partial.so").write_bytes(b"partial")
        raise BenchmarkError("interrupted")

    monkeypatch.setattr(reference_execution, "run_managed_container", interrupt)

    with pytest.raises(BenchmarkError, match="interrupted"):
        reference_execution.ensure_reference_dependencies(
            project_root=tmp_path,
            docker_config=tmp_path / "docker-config",
        )

    build_root = tmp_path / "artifacts/docker"
    assert not (build_root / "reference-deps").exists()
    assert list(build_root.glob(".reference-deps.build-*")) == []
    assert list(build_root.glob(".reference-deps.replaced-*")) == []


def test_malformed_wheel_inventory_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "deps"
    wheel = root / ".wheels/dependency.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escape.py", b"escape")
    monkeypatch.setattr(
        reference_execution,
        "REFERENCE_DEPENDENCY_WHEELS",
        (
            (
                wheel.name,
                "https://example.invalid/dependency.whl",
                hashlib.sha256(wheel.read_bytes()).hexdigest(),
            ),
        ),
    )

    with pytest.raises(BenchmarkError, match="inventory"):
        reference_execution.validate_reference_dependencies(root)


def test_concurrent_dependency_builders_publish_one_complete_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    builds = 0

    def build(arguments: list[str], **_kwargs):
        nonlocal builds
        builds += 1
        staging = Path(arguments[arguments.index("--volume") + 1].split(":", 1)[0])
        _sealed_dependency_cache(monkeypatch, staging)
        entered.set()
        assert release.wait(timeout=5)
        return CompletedProcess(arguments, 0, "", ""), {}

    monkeypatch.setattr(reference_execution, "run_managed_container", build)
    kwargs = {
        "project_root": tmp_path,
        "docker_config": tmp_path / "docker-config",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reference_execution.ensure_reference_dependencies, **kwargs)
        assert entered.wait(timeout=5)
        second = pool.submit(reference_execution.ensure_reference_dependencies, **kwargs)
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5)

    assert builds == 1


def test_dependency_staging_is_immune_to_source_tamper_after_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, native = _sealed_dependency_cache(monkeypatch, tmp_path / "source")
    wheel_name, _url, wheel_sha256 = reference_execution.REFERENCE_DEPENDENCY_WHEELS[0]
    target = tmp_path / "private-tmpfs"
    dependency_seal.copy_and_validate(
        source,
        target,
        ((wheel_name, wheel_sha256),),
        after_copy=lambda: native.write_bytes(b"tampered-after-private-copy"),
    )

    assert (target / native.relative_to(source)).read_bytes() == b"native-extension"


def test_wheel_decompression_bomb_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "deps"
    wheel = root / ".wheels/dependency.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package/bomb.bin", b"0" * (2 * 1024**2))
    monkeypatch.setattr(
        reference_execution,
        "REFERENCE_DEPENDENCY_WHEELS",
        (
            (
                wheel.name,
                "https://example.invalid/dependency.whl",
                hashlib.sha256(wheel.read_bytes()).hexdigest(),
            ),
        ),
    )
    (root / "package").mkdir()
    (root / "package/bomb.bin").write_bytes(b"0" * (2 * 1024**2))

    with pytest.raises(BenchmarkError, match="inventory"):
        reference_execution.validate_reference_dependencies(root)


def test_reference_dependency_builder_uses_bind_mount_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def run_container(arguments: list[str], **_kwargs):
        captured.extend(arguments)
        staging = Path(arguments[arguments.index("--volume") + 1].split(":", 1)[0])
        _sealed_dependency_cache(monkeypatch, staging)
        return CompletedProcess(arguments, 0, "", ""), {}

    monkeypatch.setattr(reference_execution, "run_managed_container", run_container)

    dependency_directory = reference_execution.ensure_reference_dependencies(
        project_root=tmp_path,
        docker_config=tmp_path / "docker-config",
    )

    owner = dependency_directory.stat()
    assert captured[captured.index("--user") + 1] == f"{owner.st_uid}:{owner.st_gid}"


def test_reference_command_is_digest_pinned_offline_and_read_only(tmp_path: Path) -> None:
    manifest = read_json(MANIFEST)
    fixture = MANIFEST.parent
    output = tmp_path / "output"
    output.mkdir()
    dependencies = tmp_path / "deps"
    dependencies.mkdir()

    command = build_reference_docker_command(
        manifest,
        fixture_root=fixture,
        output_directory=output,
        dependency_directory=dependencies,
        trace_mode="hash",
        profile=True,
        docker_config=tmp_path / "docker-config",
        market_snapshot={
            "role": "market_metadata",
            "path": "inputs/market_metadata/markets.json",
        },
    )

    assert REFERENCE_IMAGE_REF in command
    assert command[command.index("--network") + 1] == "none"
    assert f"{fixture}:/fixture:ro" in command
    assert any(value.endswith(":/nfi-reference-tracer:ro") for value in command)
    assert any(
        value.endswith(":/nfi-python/nfi_backtest_engine:ro") for value in command
    )
    assert "NFI_TRACE_INCLUDE_STATE=0" in command
    assert "NFI_BTE_PROFILE_EVENTS=/output/profile.jsonl" in command
    assert "NFI_MARKET_SNAPSHOT_PATH=/fixture/inputs/market_metadata/markets.json" in command
    owner = output.stat()
    assert command[command.index("--user") + 1] == f"{owner.st_uid}:{owner.st_gid}"
    assert f"NFI_BIND_UID={owner.st_uid}" in command
    assert f"NFI_BIND_GID={owner.st_gid}" in command
    writable_binds = [
        value
        for index, value in enumerate(command)
        if index and command[index - 1] == "--volume" and not value.endswith(":ro")
    ]
    assert writable_binds == [f"{output}:/output"]
    assert command[-4:] == [
        "--userdir",
        "/output/user_data",
        "--backtest-directory",
        "/output",
    ]


def test_docker_invocation_does_not_leak_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("NFI_UNTRUSTED_AMBIENT", "leaked")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setattr(reference_execution.subprocess, "run", fake_run)
    reference_execution._run_docker(tmp_path, ["version"])

    child_environment = captured["env"]
    assert isinstance(child_environment, dict)
    assert "NFI_UNTRUSTED_AMBIENT" not in child_environment
    assert "PYTHONPATH" not in child_environment


def test_reference_command_removes_mutable_output_options(tmp_path: Path) -> None:
    manifest = deepcopy(read_json(MANIFEST))
    manifest["freqtrade"]["command"].extend(
        ["--export-filename", "old.json", "--backtest-directory=/old"]
    )
    output = tmp_path / "output"
    output.mkdir()

    command = build_reference_docker_command(
        manifest,
        fixture_root=MANIFEST.parent,
        output_directory=output,
        dependency_directory=None,
        trace_mode="off",
        profile=False,
        docker_config=tmp_path / "docker-config",
        market_snapshot={
            "role": "market_metadata",
            "path": "inputs/market_metadata/markets.json",
        },
    )

    assert "old.json" not in command
    assert "--backtest-directory=/old" not in command
    assert command[-4:] == [
        "--userdir",
        "/output/user_data",
        "--backtest-directory",
        "/output",
    ]


def test_reference_command_can_be_built_without_local_docker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Pure argv validation must remain portable to Docker-free CI hosts."""
    monkeypatch.setattr(reference_runtime.shutil, "which", lambda _name: None)
    manifest = read_json(MANIFEST)
    output = tmp_path / "output"
    output.mkdir()

    command = build_reference_docker_command(
        manifest,
        fixture_root=MANIFEST.parent,
        output_directory=output,
        dependency_directory=None,
        trace_mode="off",
        profile=False,
        docker_config=tmp_path / "docker-config",
        market_snapshot={
            "role": "market_metadata",
            "path": "inputs/market_metadata/markets.json",
        },
    )

    assert command[0] == "docker"


def _reference_managed_prefix(tmp_path: Path) -> list[str]:
    return [
        "docker",
        "--config",
        str(tmp_path / "docker-config"),
        "run",
        "--rm",
        "--cidfile",
        str(tmp_path / "container.cid"),
        "--label",
        "io.nfi-backtest-engine.managed=true",
        "--label",
        "io.nfi-backtest-engine.role=reference",
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
        str(8 * 1024**3),
        "--memory-swap",
        str(8 * 1024**3),
    ]


def test_reference_command_rejects_a_run_prefix_without_mandatory_sandbox(
    tmp_path: Path,
) -> None:
    manifest = read_json(MANIFEST)
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(BenchmarkError, match="sandbox"):
        build_reference_docker_command(
            manifest,
            fixture_root=MANIFEST.parent,
            output_directory=output,
            dependency_directory=None,
            trace_mode="off",
            profile=False,
            docker_config=tmp_path / "docker-config",
            market_snapshot={
                "role": "market_metadata",
                "path": "inputs/market_metadata/markets.json",
            },
            run_prefix=["docker", "run", "--rm", "--read-only=false"],
        )


@pytest.mark.parametrize(
    "injected",
    [
        ["--volume", "/:/host-root"],
        ["--privileged=true"],
        ["--memory=0"],
        ["--network", "none", "--network", "host"],
        ["--user", "1000:1000", "--user", "0:0"],
        ["--mount", "type=bind,source=/,target=/host-root"],
    ],
)
def test_reference_command_rejects_prefix_option_injection(
    injected: list[str],
    tmp_path: Path,
) -> None:
    manifest = read_json(MANIFEST)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(BenchmarkError, match="sandbox"):
        build_reference_docker_command(
            manifest,
            fixture_root=MANIFEST.parent,
            output_directory=output,
            dependency_directory=None,
            trace_mode="off",
            profile=False,
            docker_config=tmp_path / "docker-config",
            market_snapshot={
                "role": "market_metadata",
                "path": "inputs/market_metadata/markets.json",
            },
            run_prefix=[*_reference_managed_prefix(tmp_path), *injected],
        )


def test_reference_command_accepts_a_resource_managed_run_prefix(tmp_path: Path) -> None:
    manifest = read_json(MANIFEST)
    output = tmp_path / "output"
    output.mkdir()
    prefix = _reference_managed_prefix(tmp_path)

    command = build_reference_docker_command(
        manifest,
        fixture_root=MANIFEST.parent,
        output_directory=output,
        dependency_directory=None,
        trace_mode="off",
        profile=False,
        docker_config=tmp_path / "docker-config",
        market_snapshot={
            "role": "market_metadata",
            "path": "inputs/market_metadata/markets.json",
        },
        run_prefix=prefix,
    )

    assert command[: len(prefix)] == prefix
    assert command[len(prefix) : len(prefix) + 2] == ["--platform", "linux/amd64"]


def test_container_memory_assessment_distinguishes_headroom_and_oom() -> None:
    resources = {
        "policy": {
            "container_memory_limit_bytes": 10 * 1024**3,
        }
    }

    healthy = reference_runtime._container_memory_assessment(
        exit_code=0,
        peak_bytes=4 * 1024**3,
        events={"oom": 0, "oom_kill": 0},
        resources=resources,
    )
    exhausted = reference_runtime._container_memory_assessment(
        exit_code=137,
        peak_bytes=10 * 1024**3,
        events={"oom": 1, "oom_kill": 1},
        resources=resources,
    )

    assert healthy["verdict"] == "within_limit"
    assert healthy["peak_ratio"] == 0.4
    assert exhausted["verdict"] == "oom_killed"
    assert exhausted["oom_kill_count"] == 1


def test_cgroup_io_stat_parser_preserves_device_counters(tmp_path: Path) -> None:
    source = tmp_path / "io.stat"
    source.write_text(
        "8:0 rbytes=1024 wbytes=2048 rios=3 wios=4\n",
        encoding="utf-8",
    )

    assert reference_runtime._read_io_stat(source) == [
        {
            "device": "8:0",
            "counters": {
                "rbytes": 1024,
                "wbytes": 2048,
                "rios": 3,
                "wios": 4,
            },
        }
    ]
def test_reference_leverage_tiers_are_loaded_from_pinned_offline_image(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        reference_runtime,
        "ensure_docker_config",
        lambda: tmp_path / "docker-config",
    )
    monkeypatch.setattr(
        reference_runtime,
        "ensure_reference_image",
        lambda **_kwargs: None,
    )
    captured: dict[str, object] = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return (
            CompletedProcess(
                arguments,
                0,
                stdout='{"BTC/USDT:USDT":[{"minNotional":0}]}',
                stderr="",
            ),
            {"policy": {"container_memory_limit_bytes": 1024}},
        )

    monkeypatch.setattr(reference_runtime, "run_managed_container", fake_run)

    result = reference_runtime.load_reference_leverage_tiers(
        ["BTC/USDT:USDT", "BTC/USDT:USDT"]
    )

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert arguments[arguments.index("--network") + 1] == "none"
    assert arguments[arguments.index("--entrypoint") + 1] == "python"
    assert arguments.count("BTC/USDT:USDT") == 1
    assert result["source"]["image_platform_digest"] == (
        reference_runtime.REFERENCE_PLATFORM_DIGEST
    )
    assert list(result["tiers"]) == ["BTC/USDT:USDT"]


def _write_reference_trace(path: Path, state: dict[str, object]) -> None:
    with StateTraceWriter(
        path,
        source="freqtrade-reference",
        run_id="reference-test",
        input_sha256="1" * 64,
        strategy_sha256="2" * 64,
        profile_sha256="3" * 64,
        trading_mode="spot",
    ) as writer:
        writer.append(
            timestamp_ms=1_700_000_000_000,
            phase="candle.after",
            pair="BTC/USDT",
            state=state,
        )


def test_reference_trace_comparison_migrates_only_legacy_expected_state(
    tmp_path: Path,
) -> None:
    closed_trade = {"id": 1, "is_open": False}
    expected_state: dict[str, object] = {
        "open_trade_count": 1,
        "trades": [closed_trade],
    }
    actual_state: dict[str, object] = {
        "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
        "open_trade_count": 1,
        "trades": [
            closed_trade,
            {"id": 2, "is_open": True},
        ],
    }
    expected = tmp_path / "legacy.trace"
    actual = tmp_path / "v2.trace"
    _write_reference_trace(expected, expected_state)
    _write_reference_trace(actual, actual_state)

    assert (
        reference_runtime._first_reference_trace_difference(expected, actual)
        is None
    )


def test_reference_trace_comparison_keeps_v2_state_exact(tmp_path: Path) -> None:
    expected = tmp_path / "expected.trace"
    actual = tmp_path / "actual.trace"
    expected_state: dict[str, object] = {
        "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
        "open_trade_count": 1,
        "trades": [{"id": 1, "is_open": True}],
        "marker": "expected",
    }
    actual_state = deepcopy(expected_state)
    actual_state["marker"] = "actual"
    _write_reference_trace(expected, expected_state)
    _write_reference_trace(actual, actual_state)

    difference = reference_runtime._first_reference_trace_difference(expected, actual)

    assert difference is not None
    assert difference.path == "$.state.marker"


def test_reference_trace_comparison_rejects_reversed_schema_migration(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "v2.trace"
    actual = tmp_path / "legacy.trace"
    _write_reference_trace(
        expected,
        {
            "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
            "open_trade_count": 0,
            "trades": [],
        },
    )
    _write_reference_trace(actual, {"open_trade_count": 0, "trades": []})

    with pytest.raises(TraceError, match="legacy expected to v2 actual"):
        reference_runtime._first_reference_trace_difference(expected, actual)


@pytest.mark.parametrize(
    "state",
    [
        {
            "schema_version": "reference-state-v3",
            "open_trade_count": 0,
            "trades": [],
        },
        {
            "schema_version": REFERENCE_STATE_SCHEMA_VERSION,
            "open_trade_count": 1,
            "trades": [{"id": 1}],
        },
    ],
)
def test_reference_trace_comparison_rejects_unsupported_or_malformed_v2_state(
    state: dict[str, object],
    tmp_path: Path,
) -> None:
    expected = tmp_path / "legacy.trace"
    actual = tmp_path / "actual.trace"
    _write_reference_trace(expected, {"open_trade_count": 0, "trades": []})
    _write_reference_trace(actual, state)

    with pytest.raises(TraceError):
        reference_runtime._first_reference_trace_difference(expected, actual)
