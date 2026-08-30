from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from nfi_backtest_engine import docker_resources, docker_runtime, doctor
from nfi_backtest_engine.docker_resources import (
    GIB,
    derive_docker_policy,
    inspect_docker_daemon,
    inspect_docker_swap_capacity,
)
from nfi_backtest_engine.docker_runtime import (
    BIND_OWNER_EXECUTABLE_FUNCTION,
    cleanup_stopped_managed_containers,
    docker_root_with_bind_owner_arguments,
    managed_docker_run,
    validate_managed_run_prefix,
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


def test_private_image_executable_drops_to_dynamic_bind_owner(
    tmp_path: Path,
) -> None:
    arguments = docker_root_with_bind_owner_arguments(tmp_path)
    owner = tmp_path.stat()

    assert arguments == [
        "--user",
        f"{owner.st_uid}:{owner.st_gid}",
        "--env",
        f"NFI_BIND_UID={owner.st_uid}",
        "--env",
        f"NFI_BIND_GID={owner.st_gid}",
    ]
    assert "/usr/local/bin/python -m freqtrade" in BIND_OWNER_EXECUTABLE_FUNCTION
    assert "/usr/local/bin/python -m site --user-base" in BIND_OWNER_EXECUTABLE_FUNCTION
    assert 'export PYTHONUSERBASE="${nfi_python_user_base}"' in (
        BIND_OWNER_EXECUTABLE_FUNCTION
    )
    assert 'id -u' in BIND_OWNER_EXECUTABLE_FUNCTION
    assert "setpriv" not in BIND_OWNER_EXECUTABLE_FUNCTION
    assert "ftuser" not in BIND_OWNER_EXECUTABLE_FUNCTION


def test_doctor_drops_ambient_docker_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environments: list[dict[str, str]] = []

    def fake_run(command: list[str], **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "attacker")
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(doctor, "ensure_docker_config", lambda: tmp_path)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    doctor._docker_checks()

    assert environments
    assert all("DOCKER_HOST" not in item for item in environments)
    assert all("DOCKER_CONTEXT" not in item for item in environments)


def test_daemon_inspection_drops_ambient_docker_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environments: list[dict[str, str]] = []
    payload = {
        "ServerVersion": "29.7.1",
        "OperatingSystem": "Linux",
        "OSType": "linux",
        "Architecture": "x86_64",
        "NCPU": 4,
        "MemTotal": 8 * GIB,
        "MemoryLimit": True,
        "SwapLimit": True,
    }

    def fake_run(command: list[str], **kwargs):
        environments.append(kwargs["env"])
        stdout = "" if "stats" in command else json.dumps(payload)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "attacker")
    monkeypatch.setattr(docker_resources.subprocess, "run", fake_run)
    inspect_docker_daemon(docker_config=tmp_path)

    assert environments
    assert all("DOCKER_HOST" not in item for item in environments)
    assert all("DOCKER_CONTEXT" not in item for item in environments)


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
    assert policy["swap_mode"] == "disabled"
    assert policy["container_swap_limit_bytes"] == 0
    assert (
        policy["container_memory_swap_limit_bytes"]
        == policy["container_memory_limit_bytes"]
    )


def test_certification_policy_uses_measured_daemon_swap_without_changing_ram_cap() -> None:
    policy = derive_docker_policy(
        _daemon(total_gib=24),
        memory_cap_bytes=18 * GIB,
        swap_mode="daemon",
        daemon_swap_bytes=32 * GIB,
        swap_cap_bytes=20 * GIB,
    )

    assert policy["container_memory_limit_bytes"] == 18 * GIB
    assert policy["container_swap_limit_bytes"] == 20 * GIB
    assert policy["container_memory_swap_limit_bytes"] == 38 * GIB


def test_certification_swap_requires_measured_supported_capacity() -> None:
    with pytest.raises(SpecValidationError, match="requires measured"):
        derive_docker_policy(_daemon(), swap_mode="daemon")
    with pytest.raises(SpecValidationError, match="exposes no swap"):
        derive_docker_policy(_daemon(), swap_mode="daemon", daemon_swap_bytes=0)
    with pytest.raises(SpecValidationError, match="does not support"):
        derive_docker_policy(
            {**_daemon(), "swap_limit_supported": False},
            swap_mode="daemon",
            daemon_swap_bytes=GIB,
        )


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


def _complete_managed_prefix() -> list[str]:
    return [
        "docker",
        "--config",
        "/trusted-config",
        "run",
        "--rm",
        "--cidfile",
        "/trusted.cid",
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
        str(8 * GIB),
        "--memory-swap",
        str(8 * GIB),
    ]


@pytest.mark.parametrize(
    "injected",
    [
        ["--volume", "/:/host-root"],
        ["--privileged=true"],
        ["--memory=0"],
        ["--network", "none", "--network", "host"],
        ["--user", "1000:1000", "--user", "0:0"],
        ["--cap-drop", "ALL"],
        ["--security-opt", "no-new-privileges=true"],
        ["--pids-limit", "512"],
        ["--memory", str(8 * GIB)],
        ["--memory-swap", str(8 * GIB)],
        ["--mount", "type=bind,source=/,target=/host-root"],
        ["--tmpfs", "/tmp:rw,noexec,nosuid,nodev"],
    ],
)
def test_managed_prefix_rejects_unknown_duplicate_or_weakened_options(
    injected: list[str],
) -> None:
    with pytest.raises(BenchmarkError, match="sandbox"):
        validate_managed_run_prefix([*_complete_managed_prefix(), *injected])


def test_bind_owner_wrapper_has_no_pinned_home_traversal_dependency() -> None:
    assert "/home/ftuser" not in BIND_OWNER_EXECUTABLE_FUNCTION
    assert "getent passwd" not in BIND_OWNER_EXECUTABLE_FUNCTION
    assert "nfi_image_home" not in BIND_OWNER_EXECUTABLE_FUNCTION


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
        assert "--read-only" in prefix
        assert prefix[prefix.index("--tmpfs") + 1].startswith(
            "/tmp:rw,noexec,nosuid,nodev"
        )
        assert prefix[prefix.index("--cap-drop") + 1] == "ALL"
        assert prefix[prefix.index("--security-opt") + 1] == (
            "no-new-privileges=true"
        )
        assert prefix[prefix.index("--pids-limit") + 1] == "512"
        assert prefix[prefix.index("--ulimit") + 1] == "nofile=4096:4096"
        assert "io.nfi-backtest-engine.role=reference" in prefix
        assert prefix[prefix.index("--memory") + 1] == str(
            lease["policy"]["container_memory_limit_bytes"]
        )
        assert prefix[prefix.index("--memory-swap") + 1] == str(
            lease["policy"]["container_memory_limit_bytes"]
        )
        assert lease["cleaned_stopped_containers"] == ["old-container"]

    assert len(removed) == 1


def test_managed_certification_prefix_uses_total_ram_and_swap_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(docker_runtime, "_LOCK_PATH", tmp_path / "runtime.lock")
    monkeypatch.setattr(docker_runtime, "docker_executable", lambda: "docker")
    monkeypatch.setattr(
        docker_runtime,
        "inspect_docker_daemon",
        lambda **_kwargs: _daemon(),
    )
    monkeypatch.setattr(
        docker_runtime,
        "inspect_docker_swap_capacity",
        lambda **_kwargs: 12 * GIB,
    )
    monkeypatch.setattr(
        docker_runtime,
        "cleanup_stopped_managed_containers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        docker_runtime,
        "list_managed_containers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(docker_runtime, "_force_remove_cid", lambda **_kwargs: None)

    with managed_docker_run(
        docker_config=tmp_path,
        role="reference",
        memory_cap_bytes=8 * GIB,
        swap_mode="daemon",
        swap_cap_bytes=6 * GIB,
        swap_probe_image="freqtrade@test",
    ) as lease:
        prefix = lease["command_prefix"]
        assert prefix[prefix.index("--memory") + 1] == str(8 * GIB)
        assert prefix[prefix.index("--memory-swap") + 1] == str(14 * GIB)


def test_swap_probe_reads_capacity_from_the_daemon_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, str(32 * GIB) + "\n", "")

    monkeypatch.setattr(docker_resources, "docker_executable", lambda: "docker")
    monkeypatch.setattr(docker_resources.subprocess, "run", fake_run)

    capacity = inspect_docker_swap_capacity(
        docker_config=tmp_path,
        image="freqtrade@test",
    )

    assert capacity == 32 * GIB
    assert "--network" in captured
    assert "none" in captured


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
