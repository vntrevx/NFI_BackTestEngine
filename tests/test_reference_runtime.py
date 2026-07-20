from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess

from nfi_backtest_engine import reference_runtime
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.reference_runtime import (
    REFERENCE_IMAGE_REF,
    build_reference_docker_command,
)

ROOT = Path(__file__).parents[1]
MANIFEST = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
    / "manifest.json"
)


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
    assert command[-4:] == [
        "--userdir",
        "/output/user_data",
        "--backtest-directory",
        "/output",
    ]


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


def test_reference_command_accepts_a_resource_managed_run_prefix(tmp_path: Path) -> None:
    manifest = read_json(MANIFEST)
    output = tmp_path / "output"
    output.mkdir()
    prefix = [
        "docker",
        "--config",
        str(tmp_path / "docker-config"),
        "run",
        "--rm",
        "--memory",
        str(8 * 1024**3),
        "--label",
        "io.nfi-backtest-engine.managed=true",
    ]

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
