from __future__ import annotations

import argparse
from pathlib import Path

from nfi_backtest_engine import cli, config_loader, market_snapshot


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
