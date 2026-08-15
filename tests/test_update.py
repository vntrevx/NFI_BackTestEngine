from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from nfi_backtest_engine import update_check
from nfi_backtest_engine.commands import update as update_commands
from nfi_backtest_engine.errors import NfiBacktestError


def test_update_executes_detected_upgrade_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[list[str]] = []
    release = update_check.LatestRelease(version="1.6.1", assets=())

    monkeypatch.setattr(update_commands, "_is_source_checkout", lambda: False)
    monkeypatch.setattr(update_commands, "__version__", "1.6.0")
    monkeypatch.setattr(update_commands, "fetch_latest_release", lambda: release)
    monkeypatch.setattr(
        update_commands,
        "_download_release_wheel",
        lambda fetched, destination: destination / f"engine-{fetched.version}.whl",
    )
    monkeypatch.setattr(
        update_commands,
        "_select_upgrade_command",
        lambda wheel: ["/usr/bin/uv", "tool", "install", "--force", str(wheel)],
    )

    def fake_run(
        arguments: list[str],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        observed.append(arguments)
        assert check is False
        return subprocess.CompletedProcess(arguments, returncode=0)

    monkeypatch.setattr(update_commands.subprocess, "run", fake_run)

    result = update_commands.execute(argparse.Namespace(command_name="update"))

    assert result == 0
    assert observed[0][:4] == ["/usr/bin/uv", "tool", "install", "--force"]
    assert observed[0][4].endswith("engine-1.6.1.whl")
    assert "Updated to NFI Backtest Engine 1.6.1." in capsys.readouterr().out


def test_update_failure_preserves_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = update_check.LatestRelease(version="1.6.1", assets=())

    monkeypatch.setattr(update_commands, "_is_source_checkout", lambda: False)
    monkeypatch.setattr(update_commands, "__version__", "1.6.0")
    monkeypatch.setattr(update_commands, "fetch_latest_release", lambda: release)
    monkeypatch.setattr(
        update_commands,
        "_download_release_wheel",
        lambda fetched, destination: destination / f"engine-{fetched.version}.whl",
    )
    monkeypatch.setattr(
        update_commands,
        "_select_upgrade_command",
        lambda wheel: ["/usr/bin/uv", "tool", "install", "--force", str(wheel)],
    )
    monkeypatch.setattr(
        update_commands.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], returncode=1),
    )

    with pytest.raises(NfiBacktestError, match="installed version was left unchanged"):
        update_commands.execute(argparse.Namespace(command_name="update"))


def test_update_does_not_downgrade_newer_installed_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = update_check.LatestRelease(version="1.6.0", assets=())

    monkeypatch.setattr(update_commands, "_is_source_checkout", lambda: False)
    monkeypatch.setattr(update_commands, "__version__", "1.6.1")
    monkeypatch.setattr(update_commands, "fetch_latest_release", lambda: release)
    monkeypatch.setattr(
        update_commands,
        "_download_release_wheel",
        lambda *_args: pytest.fail("a newer installation must not be replaced"),
    )

    result = update_commands.execute(argparse.Namespace(command_name="update"))

    assert result == 0
    assert "Already up to date: 1.6.1." in capsys.readouterr().out


def test_uv_tool_install_selects_verified_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = Path("/tmp/nfi-backtest-engine-1.6.1.whl")
    monkeypatch.setattr(sys, "executable", "/home/user/.local/share/uv/tools/pkg/bin/python")
    monkeypatch.setattr(
        update_commands.shutil,
        "which",
        lambda executable: f"/usr/bin/{executable}" if executable == "uv" else None,
    )

    assert update_commands._select_upgrade_command(wheel) == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.12",
        str(wheel),
    ]


def test_github_release_payload_parses_version_and_assets() -> None:
    release = update_check.parse_latest_release(
        {
            "tag_name": "v1.6.1",
            "assets": [
                {
                    "name": "nfi_backtest_engine-1.6.1-manylinux2014_x86_64.whl",
                    "browser_download_url": "https://example.test/engine.whl",
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        }
    )

    assert release.version == "1.6.1"
    assert release.assets == (
        update_check.ReleaseAsset(
            name="nfi_backtest_engine-1.6.1-manylinux2014_x86_64.whl",
            download_url="https://example.test/engine.whl",
            sha256="a" * 64,
        ),
    )


def test_update_notice_fetches_and_caches_latest_version(tmp_path: Path) -> None:
    cache_path = tmp_path / "update-check.json"

    notice = update_check.available_update_notice(
        "1.6.0",
        cache_path=cache_path,
        now_epoch=10_000.0,
        fetch_latest=lambda: "1.7.0",
    )

    assert notice == "Update available: 1.6.0 -> 1.7.0. Run `nfi-bte update`."
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {
        "checked_at": 10_000.0,
        "latest_version": "1.7.0",
    }


def test_update_notice_uses_fresh_cache_without_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "update-check.json"
    cache_path.write_text(
        json.dumps({"checked_at": 9_900.0, "latest_version": "1.7.0"}),
        encoding="utf-8",
    )

    def fail_fetch() -> str:
        raise AssertionError("fresh cache must avoid a network request")

    notice = update_check.available_update_notice(
        "1.6.0",
        cache_path=cache_path,
        now_epoch=10_000.0,
        fetch_latest=fail_fetch,
    )

    assert notice == "Update available: 1.6.0 -> 1.7.0. Run `nfi-bte update`."


def test_update_notice_does_not_downgrade_newer_local_version(tmp_path: Path) -> None:
    notice = update_check.available_update_notice(
        "2.0.0",
        cache_path=tmp_path / "update-check.json",
        now_epoch=10_000.0,
        fetch_latest=lambda: "1.7.0",
    )

    assert notice is None
