from __future__ import annotations

from copy import deepcopy
from pathlib import Path

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
        project_root=ROOT,
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
    assert "NFI_TRACE_INCLUDE_STATE=0" in command
    assert "NFI_BTE_PROFILE_EVENTS=/output/profile.jsonl" in command
    assert (
        "NFI_MARKET_SNAPSHOT_PATH=/fixture/inputs/market_metadata/markets.json"
        in command
    )
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
        project_root=ROOT,
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
