from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from nfi_backtest_engine import cli, research_runner, setup_wizard, user_flow
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.commands import run as run_command
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.project_setup import (
    initialize_project,
    load_project,
    project_run_arguments,
)
from nfi_backtest_engine.verification_ledger import VerificationLedger


def _standard_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    user_data = tmp_path / "user_data"
    strategies = user_data / "strategies"
    strategies.mkdir(parents=True)
    source = strategies / "SimpleStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class SimpleStrategy(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    config = user_data / "config.json"
    config.write_text(
        '{"exchange":{"name":"binance","pair_whitelist":["BTC/USDT"]}}',
        encoding="utf-8",
    )
    data = user_data / "data" / "binance"
    data.mkdir(parents=True)
    return source, config, data


def _completed_run_evidence(root: Path) -> tuple[dict, Path]:
    root.mkdir(parents=True, exist_ok=True)
    surface = root / "trade-surface.json"
    surface.write_text("{}\n", encoding="utf-8")
    (root / "data-seal.json").write_text("{}\n", encoding="utf-8")
    write_json(
        root / "effective-config.redacted.json",
        {"config": {"trading_mode": "spot"}},
    )
    digest = "a" * 64
    inputs = {
        "strategy": {
            "file_sha256": digest,
            "capability_fingerprint": "c" * 64,
        },
        "config": {"run_effective_sha256": "d" * 64},
        "pairlist_sha256": "e" * 64,
        "market_metadata": {"sha256": "f" * 64},
        "timerange": "20210101-20260101",
    }
    run_id = hashlib.sha256(
        json.dumps(
            inputs,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    run = {
        "run_id": run_id,
        "status": "complete",
        "complete": True,
        "created_at": "2026-07-29T00:00:00Z",
        "inputs": inputs,
        "capability": {"hot_ir_fingerprint": "1" * 64},
        "result": {
            "execution": {
                "build": {"binary_sha256": "2" * 64},
            },
            "trade_surface": {
                "path": str(surface),
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            },
        },
    }
    write_json(root / "identity.json", {"run_id": run_id, "identity": inputs})
    write_json(root / "run.json", run)
    return run, surface


def test_standard_layout_initializes_without_prompts(tmp_path: Path) -> None:
    source, config, data = _standard_layout(tmp_path)
    messages: list[str] = []

    settings = initialize_project(
        workspace=tmp_path,
        source=None,
        timerange="20250101-20260101",
        interactive=False,
        prompt=lambda _: pytest.fail("standard layout should not prompt"),
        emit=messages.append,
    )

    assert settings.strategy_path == source
    assert settings.class_name == "SimpleStrategy"
    assert settings.config_path == config
    assert settings.data_directory == data
    assert settings.output_directory == (
        tmp_path / ".nfi/runs/simple-strategy-20250101-20260101"
    )
    document = read_json(tmp_path / ".nfi/project.json")
    assert document["workspace"] == ".."
    assert document["strategy"]["path"] == "user_data/strategies/SimpleStrategy.py"
    assert document["config_path"] == "user_data/config.json"
    assert document["pairs"] is None
    assert all("detected" in message or "project ready" in message for message in messages)


def test_first_run_generates_safe_futures_config_before_asking_for_pairs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "trading_mode-spot.json").write_text(
        '{"trading_mode":"spot"}',
        encoding="utf-8",
    )
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    (user_data / "config.json").write_text(
        '{"add_config_files":["../configs/trading_mode-spot.json"]}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    answers = iter(["futures", "binance", "custom", "BTC/USDT:USDT"])
    prompts: list[str] = []
    messages: list[str] = []

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        data_directory=data,
        timerange="20250101-20250108",
        interactive=True,
        prompt=lambda label: (prompts.append(label), next(answers))[1],
        emit=messages.append,
    )

    assert prompts[0].startswith("Trading mode")
    assert prompts[1].startswith("Exchange")
    assert prompts[2].startswith("Number of markets")
    assert prompts[3].startswith("Pairs")
    assert settings.config_path == tmp_path / ".nfi/first-run-config.json"
    assert settings.pairs == ("BTC/USDT:USDT",)
    generated = read_json(settings.config_path)
    assert "add_config_files" not in generated
    assert generated["trading_mode"] == "futures"
    assert generated["margin_mode"] == "isolated"
    assert generated["exchange"]["name"] == "binance"
    assert generated["exchange"]["pair_whitelist"] == []
    assert generated["exchange"]["ccxt_config"]["options"]["fetchMarkets"]["types"] == [
        "linear"
    ]
    assert any("modular config was found" in message for message in messages)
    assert any("generated safe futures config" in message for message in messages)


def test_beginner_can_select_eighty_live_ranked_pairs_without_pasting_symbols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "pairlist-volume-binance-usdt.json").write_text("{}", encoding="utf-8")
    (configs / "blacklist-binance.json").write_text("{}", encoding="utf-8")
    ranked_pairs = [f"COIN{index}/USDT" for index in range(100)]
    monkeypatch.setattr(
        setup_wizard,
        "resolve_nfi_volume_pairs",
        lambda *_args, **_kwargs: ranked_pairs,
    )
    answers = iter(["", "", "80"])
    prompts: list[str] = []
    messages: list[str] = []

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20260101-20260108",
        interactive=True,
        prompt=lambda label: (prompts.append(label), next(answers))[1],
        emit=messages.append,
    )

    assert settings.pairs == tuple(ranked_pairs[:80])
    assert settings.data_directory == tmp_path / ".nfi/data/binance"
    assert prompts[2].startswith("Number of markets")
    assert all(not prompt.startswith("Candle data") for prompt in prompts)
    assert any("selected top 80" in message for message in messages)
    assert any("Large run selected: 80 markets." in message for message in messages)
    assert any("39 GiB" in message for message in messages)


def test_beginner_default_is_btc_not_an_example_altcoin(tmp_path: Path) -> None:
    source = tmp_path / "NostalgiaForInfinityX7.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20260101-20260108",
        interactive=False,
    )

    assert settings.pairs == ("BTC/USDT",)


def test_explicit_config_remains_strict_and_must_match_requested_mode(
    tmp_path: Path,
) -> None:
    source, config, data = _standard_layout(tmp_path)

    with pytest.raises(SpecValidationError, match="differs from config mode spot"):
        initialize_project(
            workspace=tmp_path,
            source=source,
            config_path=config,
            trading_mode="futures",
            data_directory=data,
            timerange="20250101-20250108",
            interactive=False,
        )

    config.write_text(
        '{"add_config_files":["../configs/unsafe.json"]}',
        encoding="utf-8",
    )
    with pytest.raises(SpecValidationError, match="canonical portable child"):
        initialize_project(
            workspace=tmp_path,
            project_path=".nfi/strict.json",
            source=source,
            config_path=config,
            trading_mode="spot",
            data_directory=data,
            timerange="20250101-20250108",
            interactive=False,
        )


def test_noninteractive_cli_can_generate_spot_config_without_manual_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "SimpleStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class SimpleStrategy(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)

    result = cli.main(
        [
            "init",
            str(source),
            "--trading-mode",
            "spot",
            "--pair",
            "ADA/USDT",
            "--datadir",
            str(data),
            "--timerange",
            "20250101-20250108",
            "--yes",
        ]
    )

    assert result == 0
    project = load_project()
    assert project.pairs == ("ADA/USDT",)
    generated = read_json(project.config_path)
    assert generated["trading_mode"] == "spot"
    assert "margin_mode" not in generated
    assert generated["exchange"]["ccxt_config"]["options"]["fetchMarkets"]["types"] == [
        "spot"
    ]


def test_noninteractive_cli_accepts_numeric_ranked_pair_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "NostalgiaForInfinityX7.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class NostalgiaForInfinityX7(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "pairlist-volume-binance-usdt.json").write_text("{}", encoding="utf-8")
    (configs / "blacklist-binance.json").write_text("{}", encoding="utf-8")
    ranked_pairs = [f"COIN{index}/USDT" for index in range(100)]
    monkeypatch.setattr(
        setup_wizard,
        "resolve_nfi_volume_pairs",
        lambda *_args, **_kwargs: ranked_pairs,
    )
    monkeypatch.chdir(tmp_path)

    result = cli.main(
        [
            "init",
            str(source),
            "--pair-count",
            "80",
            "--timerange",
            "20250101-20250108",
            "--yes",
        ]
    )

    assert result == 0
    assert load_project().pairs == tuple(ranked_pairs[:80])


def test_noninteractive_setup_uses_recent_seven_day_quick_test(
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        interactive=False,
        now=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert settings.timerange == "20260712-20260719"


def test_multiple_strategy_classes_require_choice_or_explicit_class(
    tmp_path: Path,
) -> None:
    source, config, data = _standard_layout(tmp_path)
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class First(IStrategy):\n"
        "    timeframe = '5m'\n"
        "class Second(IStrategy):\n"
        "    timeframe = '15m'\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecValidationError, match="multiple strategy classes"):
        initialize_project(
            workspace=tmp_path,
            source=source,
            config_path=config,
            data_directory=data,
            timerange="20250101-20250102",
            interactive=False,
        )

    answers = iter(["2"])
    selected = initialize_project(
        workspace=tmp_path,
        project_path=".nfi/selected.json",
        source=source,
        config_path=config,
        data_directory=data,
        timerange="20250101-20250102",
        interactive=True,
        prompt=lambda _: next(answers),
        emit=lambda _: None,
    )

    assert selected.class_name == "Second"


def test_dynamic_pairlist_wizard_can_freeze_explicit_pairs(tmp_path: Path) -> None:
    source, config, data = _standard_layout(tmp_path)
    config.write_text('{"exchange":{"name":"binance"}}', encoding="utf-8")
    answers = iter(["BTC/USDT, ETH/USDT"])

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        config_path=config,
        data_directory=data,
        timerange="20250101-20250102",
        interactive=True,
        prompt=lambda _: next(answers),
        emit=lambda _: None,
    )

    assert settings.pairs == ("BTC/USDT", "ETH/USDT")


def test_output_directory_cannot_own_workspace_or_inputs(tmp_path: Path) -> None:
    source, config, data = _standard_layout(tmp_path)

    with pytest.raises(SpecValidationError, match="would own the workspace"):
        initialize_project(
            workspace=tmp_path,
            source=source,
            config_path=config,
            data_directory=data,
            timerange="20250101-20250102",
            output_directory=tmp_path,
            interactive=False,
        )


def test_project_load_rejects_unknown_fields(tmp_path: Path) -> None:
    source, _, _ = _standard_layout(tmp_path)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    document = read_json(settings.project_path)
    document["unexpected"] = True
    write_json(settings.project_path, document)

    with pytest.raises(SpecValidationError, match="fields differ"):
        load_project(settings.project_path)


def test_first_run_initializes_project_and_forwards_existing_runner_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        run_command.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _question: pytest.fail("--yes must bypass interactive confirmation"),
    )

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {
            "status": "prepared",
            "vectors": {"pair_count": 1, "cache_hits": 0},
            "resumed_stages": [],
            "complete": False,
            "prepared_only": True,
        }

    monkeypatch.setattr(research_runner, "run_research_backtest", fake_run)

    result = cli.main(
        [
            "run",
            str(source),
            "--timerange",
            "20250101-20250102",
            "--yes",
            "--prepare-only",
        ]
    )

    assert result == 0
    assert (tmp_path / ".nfi/project.json").is_file()
    assert calls[0]["strategy_path"] == source
    assert calls[0]["class_name"] == "SimpleStrategy"
    assert calls[0]["prepare_only"] is True
    assert calls[0]["resume"] is False
    assert calls[0]["profile_path"] == tmp_path / ".nfi/execution-profile.json"
    preflight = read_json(tmp_path / ".nfi/run-preflight.json")
    assert preflight["passed"] is True
    assert preflight["disk"]["download_growth_bounded"] is False


def test_saved_run_resumes_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    settings.output_directory.mkdir(parents=True)
    (settings.output_directory / "identity.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "prepared",
            "vectors": {"pair_count": 1, "cache_hits": 1},
            "resumed_stages": ["data", "vectors"],
            "complete": False,
            "prepared_only": True,
        }

    monkeypatch.setattr(research_runner, "run_research_backtest", fake_run)

    prompts: list[str] = []
    monkeypatch.setattr(
        run_command.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda question: prompts.append(question) or "y",
    )

    assert cli.main(["run"]) == 0
    assert captured["resume"] is True
    assert captured["prepare_only"] is False
    assert "Resume this run now?" in prompts[0]
    assert "CPU workers may run at full load" in prompts[0]


def test_saved_run_cancellation_starts_no_simulation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    settings.output_directory.mkdir(parents=True)
    (settings.output_directory / "identity.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_command.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        "builtins.input",
        lambda question: prompts.append(question) or "n",
    )
    monkeypatch.setattr(
        research_runner,
        "run_research_backtest",
        lambda **_kwargs: pytest.fail("declined run must not reach the simulator"),
    )

    assert cli.main(["run"]) == 0

    assert "Resume this run now?" in prompts[0]
    assert "CPU workers may run at full load" in prompts[0]
    assert "Backtest cancelled. No simulation was started." in capsys.readouterr().out


def test_noninteractive_saved_run_requires_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_command.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: False),
    )
    monkeypatch.setattr(
        research_runner,
        "run_research_backtest",
        lambda **_kwargs: pytest.fail("unconfirmed run must not reach the simulator"),
    )

    assert cli.main(["run"]) == 2
    assert (
        "non-interactive run requires --yes to confirm CPU-intensive work"
        in capsys.readouterr().err
    )


def test_saved_project_rejects_inline_reconfiguration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    monkeypatch.chdir(tmp_path)

    result = cli.main(["run", "--timerange", "20240101-20250101"])

    assert result == 2
    assert "init --force" in capsys.readouterr().err


def test_project_arguments_do_not_embed_config_secrets(tmp_path: Path) -> None:
    source, config, _ = _standard_layout(tmp_path)
    config.write_text(
        '{"exchange":{"name":"binance","key":"secret-key","pair_whitelist":["BTC/USDT"]}}',
        encoding="utf-8",
    )

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    document = settings.project_path.read_text(encoding="utf-8")
    arguments = project_run_arguments(settings)

    assert "secret-key" not in document
    assert arguments["config_path"] == config


def test_disk_preflight_derives_work_and_margin_from_local_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, data = _standard_layout(tmp_path)
    (data / "one.feather").write_bytes(b"a" * 100)
    (data / "two.feather").write_bytes(b"b" * 300)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    monkeypatch.setattr(
        user_flow,
        "inspect_hardware",
        lambda _workspace: {
            "system": "test",
            "machine": "portable",
            "physical_cpu_count": 4,
            "logical_cpu_count": 8,
            "affinity_cpu_count": 4,
            "memory": {"available_bytes": 10_000},
        },
    )
    monkeypatch.setattr(
        user_flow,
        "_inspect_optional_docker",
        lambda: {"status": "unavailable", "detail": "test"},
    )
    monkeypatch.setattr(
        user_flow.psutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1_000_000),
    )

    fresh = user_flow.inspect_run_preflight(
        settings,
        resume=False,
        download_missing=False,
    )
    known = fresh["disk"]["known_input_bytes"]

    assert fresh["passed"] is True
    assert fresh["host"]["cpu_worker_limit"] == 4
    assert fresh["host"]["physical_cpu_count"] == 4
    assert fresh["host"]["logical_cpu_count"] == 8
    assert "4 CPU workers (4 logical visible)" in user_flow.format_run_preflight(
        fresh,
        settings.workspace / ".nfi/run-preflight.json",
    )
    assert fresh["disk"]["known_data_logical_bytes"] == 400
    assert fresh["disk"]["estimated_remaining_work_bytes"] == known
    assert fresh["disk"]["safety_margin_bytes"] == known
    assert fresh["disk"]["required_free_bytes"] == known * 2
    assert fresh["disk"]["download_growth_bounded"] is True

    settings.output_directory.mkdir(parents=True)
    (settings.output_directory / "checkpoint.bin").write_bytes(b"x" * (known // 2))
    resumed = user_flow.inspect_run_preflight(
        settings,
        resume=True,
        download_missing=False,
    )

    assert resumed["disk"]["estimated_remaining_work_bytes"] < known
    assert resumed["disk"]["required_free_bytes"] < fresh["disk"]["required_free_bytes"]


def test_disk_preflight_fails_before_native_when_measured_space_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, data = _standard_layout(tmp_path)
    (data / "candles.feather").write_bytes(b"x" * 100)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    monkeypatch.setattr(
        user_flow,
        "inspect_hardware",
        lambda _workspace: {
            "system": "test",
            "machine": "portable",
            "physical_cpu_count": 2,
            "logical_cpu_count": 4,
            "affinity_cpu_count": 2,
            "memory": {"available_bytes": 1_000},
        },
    )
    monkeypatch.setattr(
        user_flow,
        "_inspect_optional_docker",
        lambda: {"status": "unavailable", "detail": "test"},
    )
    monkeypatch.setattr(
        user_flow.psutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1),
    )

    with pytest.raises(SpecValidationError, match="disk preflight failed"):
        user_flow.write_run_preflight(
            settings,
            resume=False,
            download_missing=False,
        )

    audit = read_json(tmp_path / ".nfi/run-preflight.json")
    assert audit["passed"] is False
    assert audit["disk"]["available_bytes"] == 1
    assert audit["disk"]["required_free_bytes"] > 1


def test_run_progress_prints_stage_percentage_and_elapsed_time() -> None:
    stream = io.StringIO()

    with user_flow.RunProgress(stream=stream, interactive=False) as progress:
        progress.update(20, "Preparing candles for 1 selected pair + 1 required BTC reference")
        progress.update(100, "Backtest complete")

    rendered = stream.getvalue()
    assert "[ 20%] Preparing candles for 1 selected pair + 1 required BTC reference" in rendered
    assert "[100%] Backtest complete" in rendered
    assert "(00:00 elapsed)" in rendered


def test_run_progress_rotates_one_interactive_status_line() -> None:
    stream = io.StringIO()

    with user_flow.RunProgress(
        stream=stream,
        interactive=True,
        interval_seconds=0.01,
    ) as progress:
        progress.update(20, "Preparing candles")
        time.sleep(0.03)
        progress.update(100, "Backtest complete")

    rendered = stream.getvalue()
    assert "\r\033[2K" in rendered
    assert any(frame in rendered for frame in user_flow.SPINNER_FRAMES)
    assert "✓  100%  Backtest complete" in rendered
    assert rendered.endswith("\n")


def test_run_banner_is_compact_and_marks_resumes() -> None:
    settings = SimpleNamespace(
        class_name="NostalgiaForInfinityX7",
        timerange="20260824-20260831",
        pairs=("BTC/USDT",),
    )

    banner = user_flow.format_run_banner(settings, resume=True)

    assert "BACKTEST ENGINE" in banner
    assert "NostalgiaForInfinityX7" in banner
    assert "2026-08-24 → 2026-08-31" in banner
    assert "1 pair" in banner
    assert "Resuming hash-valid checkpoints" in banner


def test_consent_defaults_to_no_and_never_prompts_noninteractively() -> None:
    def unexpected_prompt(_question: str) -> str:
        pytest.fail("noninteractive consent must not prompt")

    assert (
        user_flow.resolve_consent(
            None,
            interactive=False,
            question="verify",
            prompt=unexpected_prompt,
        )
        is False
    )
    assert (
        user_flow.resolve_consent(
            True,
            interactive=False,
            question="verify",
            prompt=unexpected_prompt,
        )
        is True
    )
    assert (
        user_flow.resolve_consent(
            None,
            interactive=True,
            question="verify",
            prompt=lambda _question: "yes",
        )
        is True
    )
    assert (
        user_flow.resolve_consent(
            None,
            interactive=True,
            question="verify",
            prompt=lambda _question: "",
        )
        is False
    )




def test_quick_verification_reuses_only_a_surface_bound_exact_attempt(
    tmp_path: Path,
) -> None:
    run, surface = _completed_run_evidence(tmp_path / "run")
    attempt = tmp_path / "run/official-verification/attempt-0001"
    attempt.mkdir(parents=True)
    (attempt / "official-trade-surface.json").write_bytes(surface.read_bytes())
    proof = {
        "run_id": run["run_id"],
        "complete": True,
        "exact_parity": True,
        "ended_at": "2026-07-29T00:01:00Z",
        "reference": {
            "version": "test",
            "image_index_digest": "sha256:index",
            "image_platform_digest": "sha256:platform",
            "platform": "linux/amd64",
        },
        "inputs": {
            "engine_trade_surface": {
                "path": str(surface),
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            },
        },
        "official_trade_surface": {
            "path": str(attempt / "official-trade-surface.json"),
            "bytes": surface.stat().st_size,
            "sha256": sha256_file(surface),
        },
    }
    write_json(attempt / "run.json", proof)

    reused, proof_path, was_reused = user_flow.run_quick_official_verification(
        tmp_path / "run"
    )

    assert was_reused is True
    assert reused == proof
    assert proof_path == attempt / "run.json"

    proof["inputs"]["engine_trade_surface"]["sha256"] = "0" * 64
    write_json(attempt / "run.json", proof)
    with pytest.raises(BenchmarkError, match="different trade surface"):
        user_flow.run_quick_official_verification(tmp_path / "run")


def test_native_and_quick_states_append_to_the_verification_ledger(
    tmp_path: Path,
) -> None:
    run, surface = _completed_run_evidence(tmp_path / "run")
    attempt = tmp_path / "run/official-verification/attempt-0001"
    attempt.mkdir(parents=True)
    official_surface = attempt / "official-trade-surface.json"
    official_surface.write_bytes(surface.read_bytes())
    proof = {
        "run_id": run["run_id"],
        "complete": True,
        "exact_parity": True,
        "ended_at": "2026-07-29T00:01:00Z",
        "reference": {
            "version": "2026.5.1",
            "image_index_digest": "sha256:index",
            "image_platform_digest": "sha256:platform",
            "platform": "linux/amd64",
        },
        "inputs": {
            "engine_trade_surface": {
                "path": str(surface),
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            },
        },
        "official_trade_surface": {
            "path": str(official_surface),
            "bytes": official_surface.stat().st_size,
            "sha256": sha256_file(official_surface),
        },
    }
    proof_path = attempt / "run.json"
    write_json(proof_path, proof)
    ledger_path = tmp_path / ".nfi/verification-ledger.sqlite"

    first = user_flow.record_native_completion(ledger_path, tmp_path / "run")
    repeated = user_flow.record_native_completion(ledger_path, tmp_path / "run")
    strategy_sequence, run_sequence = user_flow.record_quick_verification(
        ledger_path,
        tmp_path / "run",
        proof,
        proof_path,
    )

    assert first == repeated == 1
    assert strategy_sequence == 2
    assert run_sequence == 3
    with VerificationLedger(ledger_path, create=False) as ledger:
        projection = ledger.project()
    assert projection["strategy"]["quick_verified"]["subject"]["id"] == "a" * 64
    assert projection["runs"][0]["run_id"] == run["run_id"]
    assert projection["runs"][0]["highest_success"]["state"] == "quick_verified"


def test_failed_quick_attempt_preserves_the_native_success(
    tmp_path: Path,
) -> None:
    run, surface = _completed_run_evidence(tmp_path / "run")
    attempt = tmp_path / "run/official-verification/attempt-0001"
    attempt.mkdir(parents=True)
    proof = {
        "run_id": run["run_id"],
        "complete": False,
        "exact_parity": False,
        "timed_out": True,
        "ended_at": "2026-07-29T00:01:00Z",
        "reference": {
            "version": "2026.5.1",
            "image_index_digest": "sha256:index",
            "image_platform_digest": "sha256:platform",
            "platform": "linux/amd64",
        },
        "inputs": {
            "engine_trade_surface": {
                "path": str(surface),
                "bytes": surface.stat().st_size,
                "sha256": sha256_file(surface),
            },
        },
        "official_trade_surface": None,
    }
    proof_path = attempt / "run.json"
    write_json(proof_path, proof)
    ledger_path = tmp_path / ".nfi/verification-ledger.sqlite"

    native_sequence = user_flow.record_native_completion(
        ledger_path,
        tmp_path / "run",
    )
    failure_sequence = user_flow.record_quick_failure(
        ledger_path,
        tmp_path / "run",
        proof,
        proof_path,
    )

    assert native_sequence == 1
    assert failure_sequence == 2
    with VerificationLedger(ledger_path, create=False) as ledger:
        projection = ledger.project()
    run_status = projection["runs"][0]
    assert run_status["highest_success"]["state"] == "native_complete"
    assert run_status["latest"]["state"] == "failed"
    assert run_status["latest_failure"]["failure"]["code"] == (
        "OFFICIAL_VERIFICATION_TIMEOUT"
    )


def test_finished_one_line_flow_runs_only_explicit_post_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    run, _ = _completed_run_evidence(settings.output_directory)
    (settings.output_directory / "report.md").write_text("# Report\n", encoding="utf-8")
    proof_path = settings.output_directory / "proof.json"
    proof = {
        "run_id": run["run_id"],
        "complete": True,
        "exact_parity": True,
    }
    write_json(proof_path, proof)
    calls: list[str] = []
    monkeypatch.setattr(
        user_flow,
        "record_native_completion",
        lambda _ledger, _run: calls.append("native") or 1,
    )
    monkeypatch.setattr(
        user_flow,
        "run_quick_official_verification",
        lambda _run, timeout_seconds: (
            calls.append(f"verify:{timeout_seconds}") or proof,
            proof_path,
            False,
        ),
    )
    monkeypatch.setattr(
        user_flow,
        "record_quick_verification",
        lambda _ledger, _run, _proof, _path: (
            calls.append("ledger-quick") or 2,
            3,
        ),
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.result_report.write_result_presentation",
        lambda *_args, **_kwargs: {},
    )
    messages: list[str] = []

    status = user_flow.finish_one_line_run(
        settings,
        native_status=0,
        verification=True,
        verification_timeout_seconds=45,
        interactive=False,
        include_breakdowns=False,
        emit=messages.append,
    )

    assert status == 0
    assert calls == ["native", "verify:45", "ledger-quick"]
    assert any("exact parity" in message for message in messages)


def test_finished_noninteractive_flow_executes_neither_optional_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)
    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        timerange="20250101-20250102",
        interactive=False,
    )
    _completed_run_evidence(settings.output_directory)
    (settings.output_directory / "report.md").write_text("# Report\n", encoding="utf-8")
    monkeypatch.setattr(user_flow, "record_native_completion", lambda _ledger, _run: 1)
    monkeypatch.setattr(
        user_flow,
        "run_quick_official_verification",
        lambda *_args, **_kwargs: pytest.fail("verification requires consent"),
    )
    monkeypatch.setattr(
        "nfi_backtest_engine.result_report.write_result_presentation",
        lambda *_args, **_kwargs: {},
    )
    messages: list[str] = []

    status = user_flow.finish_one_line_run(
        settings,
        native_status=0,
        verification=None,
        verification_timeout_seconds=None,
        interactive=False,
        include_breakdowns=False,
        emit=messages.append,
    )

    assert status == 0
    assert messages == []
