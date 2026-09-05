from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from nfi_backtest_engine import cli
from nfi_backtest_engine.cleanroom_e2e import run_cleanroom_e2e
from nfi_backtest_engine.errors import BenchmarkError

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-tag121-spot-v17.4.435-2023-01-01_02"
    / "manifest.json"
)


def test_cleanroom_journey_uses_only_installed_cli_and_sealed_inputs(tmp_path: Path) -> None:
    output = tmp_path / "audit"
    observed: list[tuple[list[str], Path]] = []

    def fake_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        observed.append((command, cwd))
        if command[1] == "init":
            project = Path(command[command.index("--project") + 1])
            project.parent.mkdir(parents=True, exist_ok=True)
            project.write_text("{}\n", encoding="utf-8")
        elif command[1] == "run":
            run = cwd / ".nfi" / "run"
            run.mkdir(parents=True)
            (run / "run.json").write_text("{}\n", encoding="utf-8")
            (run / "report.md").write_text("# report\n", encoding="utf-8")
        elif command[1] == "clean":
            (cwd / ".nfi" / "clean-audit.json").write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    report = run_cleanroom_e2e(
        FIXTURE,
        output,
        executable="/installed/bin/nfi-bte",
        require_no_checkout=False,
        runner=fake_runner,
    )

    assert report["complete"] is True
    assert report["repository_checkout_used"] is False
    assert [item["name"] for item in report["commands"]] == [
        "doctor",
        "strategy-list",
        "init",
        "run",
        "report",
        "status",
        "clean-dry-run",
        "update-check",
    ]
    assert all(command[0] == "/installed/bin/nfi-bte" for command, _cwd in observed)
    assert len({cwd for _command, cwd in observed}) == 1
    assert "--no-download" in observed[3][0]
    assert "--no-market-download" in observed[3][0]
    assert (output / "cleanroom-report.json").is_file()


def test_cleanroom_failure_is_durable_and_actionable(tmp_path: Path) -> None:
    def failing_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="missing docker")

    output = tmp_path / "audit"
    with pytest.raises(BenchmarkError, match="CLEANROOM_DOCTOR_FAILED"):
        run_cleanroom_e2e(
            FIXTURE,
            output,
            executable="/installed/bin/nfi-bte",
            require_no_checkout=False,
            runner=failing_runner,
        )

    assert (output / "cleanroom-report.json").is_file()
    assert (output / "01-doctor.stderr.log").read_text(encoding="utf-8") == "missing docker"


def test_cleanroom_cli_contract() -> None:
    args = cli.build_parser().parse_args(
        [
            "release",
            "cleanroom",
            "--fixture",
            "manifest.json",
            "--output-dir",
            "audit",
        ]
    )

    assert args.release_command == "cleanroom"
    assert args.fixture == Path("manifest.json")
    assert args.output_dir == Path("audit")
    assert args.timeout == 900
