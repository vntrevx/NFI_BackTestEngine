from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from nfi_backtest_engine import cli, research_runner
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.project_setup import (
    initialize_project,
    load_project,
    project_run_arguments,
)


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
    assert settings.output_directory == (tmp_path / "artifacts/simple-strategy-20250101-20260101")
    document = read_json(tmp_path / ".nfi/project.json")
    assert document["workspace"] == ".."
    assert document["strategy"]["path"] == "user_data/strategies/SimpleStrategy.py"
    assert document["config_path"] == "user_data/config.json"
    assert document["pairs"] is None
    assert all("detected" in message or "project ready" in message for message in messages)


def test_noninteractive_setup_uses_previous_complete_calendar_year(
    tmp_path: Path,
) -> None:
    source, _, _ = _standard_layout(tmp_path)

    settings = initialize_project(
        workspace=tmp_path,
        source=source,
        interactive=False,
        now=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert settings.timerange == "20250101-20260101"


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


def test_saved_run_automatically_resumes_nonempty_output(
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

    assert cli.main(["run", "--prepare-only"]) == 0
    assert captured["resume"] is True


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
