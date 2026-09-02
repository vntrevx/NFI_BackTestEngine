from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from nfi_backtest_engine import cli, config_loader, execution_platform, market_snapshot
from nfi_backtest_engine.commands import (
    certify as certify_commands,
)
from nfi_backtest_engine.commands import (
    clean as clean_commands,
)
from nfi_backtest_engine.commands import (
    fixture as fixture_commands,
)
from nfi_backtest_engine.commands import (
    reference as reference_commands,
)
from nfi_backtest_engine.commands import (
    release as release_commands,
)
from nfi_backtest_engine.commands import (
    report as report_commands,
)
from nfi_backtest_engine.commands import (
    run as run_commands,
)
from nfi_backtest_engine.commands import (
    system as system_commands,
)
from nfi_backtest_engine.commands import (
    update as update_commands,
)
from nfi_backtest_engine.execution_platform import NATIVE_WINDOWS_UNSUPPORTED_MESSAGE
from nfi_backtest_engine.parity import ParityDifference, ParityMismatch


def test_cli_rejects_native_windows_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(execution_platform.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli, "_dispatch_command", lambda *_args, **_kwargs: pytest.fail("dispatch"))

    assert cli.main(["update"]) == 2
    assert capsys.readouterr().err == f"error: {NATIVE_WINDOWS_UNSUPPORTED_MESSAGE}\n"


def test_cli_help_and_version_bypass_execution_platform_guard(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "require_supported_execution_platform",
        lambda: pytest.fail("execution guard"),
    )

    with pytest.raises(SystemExit) as help_stopped:
        cli.main(["--help"])
    assert help_stopped.value.code == 0
    assert "usage: nfi-bte" in capsys.readouterr().out

    with pytest.raises(SystemExit) as version_stopped:
        cli.main(["--version"])
    assert version_stopped.value.code == 0
    assert capsys.readouterr().out.strip().startswith("nfi-bte ")


def test_no_argument_cli_is_nonblocking_and_default_help_is_layered(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.main([]) == 0
    beginner_help = capsys.readouterr().out
    assert "run" in beginner_help
    assert "doctor" in beginner_help
    assert "semantic-inventory" not in beginner_help

    assert cli.main(["help", "--all"]) == 0
    expert_help = capsys.readouterr().out
    assert "strategy" in expert_help
    assert "engine" in expert_help


def test_certification_help_renders_literal_spread_percentage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["certify", "--help"])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "extends to 5 above 5% spread" in help_text
    assert "option_strings" not in help_text


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


def test_probe_capture_parser_keeps_fixture_and_work_outputs_separate() -> None:
    args = cli.build_parser().parse_args(
        [
            "probe",
            "capture",
            "probe.json",
            "--output-dir",
            "fixture",
            "--work-dir",
            ".nfi/probe-work",
            "--workers",
            "4",
        ]
    )

    assert args.command_name == "probe"
    assert args.probe_command == "capture"
    assert args.output_dir == Path("fixture")
    assert args.work_dir == Path(".nfi/probe-work")
    assert args.workers == 4


def test_targeted_strategy_verification_parser_binds_exact_inputs() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "verify-targeted",
            "latest.py",
            "strategy-diff.json",
            "compatibility.json",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "futures",
            "--upstream-repository",
            "iterativv/NostalgiaForInfinity",
            "--upstream-commit",
            "a" * 40,
            "--output-dir",
            ".nfi/targeted",
        ]
    )

    assert args.strategy_command == "verify-targeted"
    assert args.source == Path("latest.py")
    assert args.trading_mode == "futures"
    assert args.fixtures_root == Path("benchmarks/fixtures/captured")
    assert args.output_dir == Path(".nfi/targeted")


def test_shared_strategy_discovery_parser_binds_transition_inputs() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "discover",
            "latest.py",
            "strategy-diff.json",
            "compatibility.json",
            "--class",
            "NostalgiaForInfinityX7",
            "--trading-mode",
            "spot",
            "--upstream-repository",
            "iterativv/NostalgiaForInfinity",
            "--upstream-commit",
            "a" * 40,
            "--baseline-source",
            "previous.py",
            "--baseline-upstream-commit",
            "b" * 40,
            "--engine-commit",
            "c" * 40,
            "--profile",
            "profile.json",
            "--output-dir",
            ".nfi/discovery",
        ]
    )

    assert args.strategy_command == "discover"
    assert args.trading_mode == "spot"
    assert args.policy is None
    assert args.baseline_source == Path("previous.py")
    assert args.baseline_upstream_commit == "b" * 40


def test_result_report_and_run_registry_machine_modes_parse() -> None:
    parser = cli.build_parser()

    report = parser.parse_args(
        [
            "report",
            "artifacts/x7",
            "--confirmation",
            "artifacts/x7-official/run.json",
            "--full-report",
        ]
    )
    runs = parser.parse_args(["runs", "list", "--limit", "5", "--json"])
    saved_run = parser.parse_args(
        [
            "run",
            "--full-report",
            "--verify",
            "--verification-timeout",
            "45",
        ]
    )
    unattended_run = parser.parse_args(["run", "--yes"])
    compact_run = parser.parse_args(["run", "--no-full-report"])
    shown = parser.parse_args(["runs", "show", "1234567890ab", "--full-report"])

    assert report.command_name == "report"
    assert report.run_directory == Path("artifacts/x7")
    assert report.confirmation == Path("artifacts/x7-official/run.json")
    assert report.full_report is True
    assert runs.runs_command == "list"
    assert runs.limit == 5
    assert runs.json is True
    assert saved_run.full_report is True
    assert saved_run.verify is True
    assert saved_run.verification_timeout == 45
    assert unattended_run.verify is None
    assert unattended_run.full_report is True
    assert compact_run.full_report is False
    assert shown.full_report is True


def test_run_rejects_contradictory_or_nonpositive_verification_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "--no-verify", "--verification-timeout", "45"]) == 2
    assert "--verification-timeout cannot be combined" in capsys.readouterr().err

    assert cli.main(["run", "--verification-timeout", "0"]) == 2
    assert "--verification-timeout must be positive" in capsys.readouterr().err

    assert cli.main(["run", "--prepare-only", "--verify"]) == 2
    assert "--verify requires a completed Native run" in capsys.readouterr().err


def test_every_top_level_command_has_one_handler() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    handler_sets = [
        fixture_commands.COMMAND_NAMES,
        reference_commands.COMMAND_NAMES,
        report_commands.COMMAND_NAMES,
        run_commands.COMMAND_NAMES,
        system_commands.COMMAND_NAMES,
        clean_commands.COMMAND_NAMES,
        certify_commands.COMMAND_NAMES,
        release_commands.COMMAND_NAMES,
        update_commands.COMMAND_NAMES,
    ]

    assert set().union(*handler_sets) == set(subparsers.choices)
    assert sum(len(command_names) for command_names in handler_sets) == len(subparsers.choices)


def test_parity_mismatch_keeps_exit_one_after_dispatch_split(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mismatch = ParityMismatch(
        ParityDifference(
            path="$.trades",
            expected=1,
            actual=2,
            reason="value differs",
        )
    )

    def fail_dispatch(*_args, **_kwargs):
        raise mismatch

    monkeypatch.setattr(cli, "_dispatch_command", fail_dispatch)

    assert cli.main(["doctor"]) == 1
    assert "parity mismatch at $.trades" in capsys.readouterr().err


def test_successful_command_checks_for_update_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_versions: list[str] = []

    monkeypatch.setattr(cli, "_dispatch_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        cli,
        "maybe_print_update_notice",
        lambda version: checked_versions.append(version),
    )

    assert cli.main(["doctor"]) == 0
    assert checked_versions == [cli.__version__]


def test_machine_json_command_does_not_append_an_update_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_dispatch_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        cli,
        "maybe_print_update_notice",
        lambda _version: pytest.fail("JSON output must remain one machine document"),
    )

    assert cli.main(["doctor", "--json"]) == 0


def test_status_without_project_or_registry_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli.main(["status", "--json"]) == 2
    assert "no research runs are registered" in capsys.readouterr().err
    assert not (tmp_path / ".nfi").exists()


def test_update_command_does_not_repeat_update_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_dispatch_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        cli,
        "maybe_print_update_notice",
        lambda _version: pytest.fail("update must not trigger an update check"),
    )

    assert cli.main(["update"]) == 0


def test_futures_market_capture_loads_pinned_binance_tiers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    config = {
        "trading_mode": "futures",
        "exchange": {"name": "binance"},
    }
    pairs = ["APE/USDT:USDT"]

    monkeypatch.setattr(cli, "load_effective_config", lambda _path: {"config": config})
    monkeypatch.setattr(
        config_loader,
        "freeze_pairlist",
        lambda _config, *, resolved_pairs: {"pairs": resolved_pairs},
    )
    monkeypatch.setattr(config_loader, "sanitize_config", lambda value: value)
    monkeypatch.setattr(
        cli,
        "load_reference_leverage_tiers",
        lambda requested: {
            "tiers": {requested[0]: [{"minNotional": 0.0}]},
            "source": {"kind": "pinned-oracle"},
        },
    )

    def fake_capture(
        captured_config,
        captured_pairs,
        destination,
        *,
        leverage_tiers,
        leverage_tier_source,
    ):
        captured.update(
            config=captured_config,
            pairs=captured_pairs,
            destination=destination,
            leverage_tiers=leverage_tiers,
            leverage_tier_source=leverage_tier_source,
        )
        return {
            "exchange": "binance",
            "pairs": captured_pairs,
            "sha256": "a" * 64,
        }

    monkeypatch.setattr(market_snapshot, "capture_market_snapshot", fake_capture)
    output = tmp_path / "markets.json"
    result = cli._execute_market_capture(
        argparse.Namespace(
            config=tmp_path / "config.json",
            pair=pairs,
            leverage_tiers=None,
            output=output,
        )
    )

    assert result == 0
    assert captured["pairs"] == pairs
    assert captured["leverage_tier_source"] == {"kind": "pinned-oracle"}
    assert captured["leverage_tiers"] == {pairs[0]: [{"minNotional": 0.0}]}


def test_spot_market_capture_does_not_request_leverage_tiers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = {
        "trading_mode": "spot",
        "exchange": {"name": "binance"},
    }
    pairs = ["BTC/USDT"]
    observed: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_effective_config", lambda _path: {"config": config})
    monkeypatch.setattr(
        config_loader,
        "freeze_pairlist",
        lambda _config, *, resolved_pairs: {"pairs": resolved_pairs},
    )
    monkeypatch.setattr(config_loader, "sanitize_config", lambda value: value)

    def fail_if_loaded(_pairs):
        raise AssertionError("spot capture must not load leverage tiers")

    monkeypatch.setattr(cli, "load_reference_leverage_tiers", fail_if_loaded)

    def fake_capture(
        _config,
        _pairs,
        _destination,
        *,
        leverage_tiers,
        leverage_tier_source,
    ):
        observed.update(
            leverage_tiers=leverage_tiers,
            leverage_tier_source=leverage_tier_source,
        )
        return {
            "exchange": "binance",
            "pairs": pairs,
            "sha256": "b" * 64,
        }

    monkeypatch.setattr(market_snapshot, "capture_market_snapshot", fake_capture)
    result = cli._execute_market_capture(
        argparse.Namespace(
            config=tmp_path / "config.json",
            pair=pairs,
            leverage_tiers=None,
            output=tmp_path / "markets.json",
        )
    )

    assert result == 0
    assert observed == {
        "leverage_tiers": None,
        "leverage_tier_source": None,
    }
