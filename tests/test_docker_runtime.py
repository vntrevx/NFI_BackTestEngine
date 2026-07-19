from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from nfi_backtest_engine import docker_resources, docker_runtime
from nfi_backtest_engine.docker_resources import (
    GIB,
    derive_docker_policy,
    inspect_docker_daemon,
)
from nfi_backtest_engine.docker_runtime import (
    cleanup_stopped_managed_containers,
    managed_docker_run,
)
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError


def _daemon(*, total_gib: int = 24) -> dict:
    return {
        "schema_version": "1.0.0",
        "server_version": "29.5.2",
        "operating_system": "Docker Desktop",
        "os_type": "linux",
        "architecture": "aarch64",
        "cpu_count": 10,
        "total_memory_bytes": total_gib * GIB,
        "memory_limit_supported": True,
        "swap_limit_supported": True,
    }


def test_daemon_inspection_reads_resources_visible_inside_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = {
        "ServerVersion": "29.5.2",
        "OperatingSystem": "Docker Desktop",
        "OSType": "linux",
        "Architecture": "aarch64",
        "NCPU": 10,
        "MemTotal": 23 * GIB,
        "MemoryLimit": True,
        "SwapLimit": True,
    }
    captured: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        if "stats" in command:
            usage = "\n".join(
                [
                    json.dumps({"MemUsage": "512MiB / 23GiB"}),
                    json.dumps({"MemUsage": "1.5GiB / 23GiB"}),
                ]
            )
            return subprocess.CompletedProcess(command, 0, usage, "")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(docker_resources, "docker_executable", lambda: "docker")
    monkeypatch.setattr(docker_resources.subprocess, "run", fake_run)

    daemon = inspect_docker_daemon(docker_config=tmp_path)

    assert daemon["total_memory_bytes"] == 23 * GIB
    assert daemon["cpu_count"] == 10
    assert daemon["architecture"] == "aarch64"
    assert daemon["active_container_count"] == 2
    assert daemon["active_container_memory_bytes"] == 2 * GIB
    assert "info" in captured
    assert "stats" in captured


def test_daemon_policy_reserves_vm_headroom_without_mac_specific_constants() -> None:
    policy = derive_docker_policy(_daemon(total_gib=24))

    assert policy["execution_mode"] == "sequential"
    assert policy["maximum_parallel_containers"] == 1
    assert policy["daemon_reserve_bytes"] == 24 * GIB // 5
    assert policy["container_memory_limit_bytes"] == 24 * GIB - (24 * GIB // 5)
    assert policy["memory_limit_enforced"]
    assert policy["swap_limit_enforced"]


def test_explicit_docker_cap_can_only_reduce_the_automatic_budget() -> None:
    policy = derive_docker_policy(_daemon(total_gib=64), memory_cap_bytes=16 * GIB)

    assert policy["container_memory_limit_bytes"] == 16 * GIB

    with pytest.raises(SpecValidationError, match="at least 1 GiB"):
        derive_docker_policy(_daemon(), memory_cap_bytes=GIB // 2)


def test_active_container_usage_is_subtracted_without_stopping_it() -> None:
    daemon = {
        **_daemon(total_gib=24),
        "active_container_count": 2,
        "active_container_memory_bytes": 3 * GIB,
    }

    policy = derive_docker_policy(daemon)

    assert policy["active_container_memory_bytes"] == 3 * GIB
    assert policy["container_memory_limit_bytes"] == 24 * GIB - (24 * GIB // 5) - 3 * GIB

    daemon["active_container_memory_bytes"] = 23 * GIB
    with pytest.raises(SpecValidationError, match="less than 1 GiB remains"):
        derive_docker_policy(daemon)


def test_managed_prefix_labels_limits_and_reclaims_the_exact_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    removed: list[Path] = []
    monkeypatch.setattr(docker_runtime, "_LOCK_PATH", tmp_path / "runtime.lock")
    monkeypatch.setattr(docker_runtime, "docker_executable", lambda: "docker")
    monkeypatch.setattr(
        docker_runtime,
        "inspect_docker_daemon",
        lambda **_kwargs: _daemon(),
    )
    monkeypatch.setattr(
        docker_runtime,
        "cleanup_stopped_managed_containers",
        lambda **_kwargs: ["old-container"],
    )
    monkeypatch.setattr(
        docker_runtime,
        "list_managed_containers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        docker_runtime,
        "_force_remove_cid",
        lambda *, docker_config, cidfile: removed.append(cidfile),
    )

    with managed_docker_run(
        docker_config=tmp_path / "docker-config",
        role="reference",
    ) as lease:
        prefix = lease["command_prefix"]
        assert "--cidfile" in prefix
        assert "io.nfi-backtest-engine.managed=true" in prefix
        assert "io.nfi-backtest-engine.role=reference" in prefix
        assert prefix[prefix.index("--memory") + 1] == str(
            lease["policy"]["container_memory_limit_bytes"]
        )
        assert prefix[prefix.index("--memory-swap") + 1] == str(
            lease["policy"]["container_memory_limit_bytes"]
        )
        assert lease["cleaned_stopped_containers"] == ["old-container"]

    assert len(removed) == 1


def test_managed_run_refuses_an_existing_owned_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(docker_runtime, "_LOCK_PATH", tmp_path / "runtime.lock")
    monkeypatch.setattr(
        docker_runtime,
        "inspect_docker_daemon",
        lambda **_kwargs: _daemon(),
    )
    monkeypatch.setattr(
        docker_runtime,
        "cleanup_stopped_managed_containers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        docker_runtime,
        "list_managed_containers",
        lambda **_kwargs: [
            {"id": "running-1", "name": "reference", "status": "Up", "state": "running"}
        ],
    )

    with (
        pytest.raises(BenchmarkError, match="still running"),
        managed_docker_run(
            docker_config=tmp_path / "docker-config",
            role="reference",
        ),
    ):
        pytest.fail("the lease must not be granted")


def test_cleanup_removes_only_stopped_owned_container_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(docker_runtime, "docker_executable", lambda: "docker")
    monkeypatch.setattr(
        docker_runtime,
        "list_managed_containers",
        lambda **_kwargs: [
            {"id": "stopped-1", "name": "old", "status": "Exited", "state": "exited"},
            {"id": "running-1", "name": "live", "status": "Up", "state": "running"},
        ],
    )

    def fake_short(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "stopped-1\n", "")

    monkeypatch.setattr(docker_runtime, "_run_short", fake_short)

    removed = cleanup_stopped_managed_containers(docker_config=tmp_path)

    assert removed == ["stopped-1"]
    assert commands == [
        [
            "docker",
            "--config",
            str(tmp_path),
            "container",
            "rm",
            "stopped-1",
        ]
    ]
