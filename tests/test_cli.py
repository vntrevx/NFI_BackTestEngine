from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine import cli


def test_benchmark_command_after_separator_is_not_swallowed(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(manifest, output, *, command_override):
        captured.update(
            manifest=manifest,
            output=output,
            command_override=command_override,
        )
        return {"complete": True}

    monkeypatch.setattr(cli, "run_benchmark", fake_run)
    result = cli.main(
        [
            "benchmark",
            "manifest.json",
            "--output",
            str(tmp_path / "report.json"),
            "--",
            "python",
            "-c",
            "print('ok')",
        ]
    )

    assert result == 0
    assert captured["manifest"] == Path("manifest.json")
    assert captured["command_override"] == ["python", "-c", "print('ok')"]


def test_system_tune_forwards_explicit_spool_directory(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_profile(destination, **kwargs):
        captured.update(destination=destination, **kwargs)
        return {
            "limits": {
                "cpu_process_limit": 4,
                "memory_cap_bytes": None,
            }
        }

    monkeypatch.setattr(cli, "create_execution_profile", fake_profile)
    result = cli.main(
        [
            "system",
            "tune",
            "--output",
            str(tmp_path / "profile.json"),
            "--spool-directory",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert captured["spool_directory"] == tmp_path
