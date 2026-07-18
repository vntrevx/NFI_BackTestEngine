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
