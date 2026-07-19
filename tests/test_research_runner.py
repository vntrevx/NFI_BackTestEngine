from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine import research_runner
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file


def _profile() -> dict:
    return {
        "hardware_fingerprint": "hardware",
        "tuning": {
            "indicator_processes": 2,
            "working_memory_bytes": 8 * 1024**3,
            "assumed_indicator_worker_peak_bytes": 3 * 1024**3,
            "portfolio_simulator_threads": 1,
        },
        "environment": {"OMP_NUM_THREADS": "1"},
    }


def _fake_prepare_data(**kwargs) -> dict:
    seal = {
        "aggregate_sha256": "data",
        "files": [{"path": "BTC_USDT-5m.feather"}],
        "downloads": [],
    }
    write_json(kwargs["destination"], seal)
    return seal


def test_research_prepare_is_checkpointed_and_resumable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Strategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Strategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata): return dataframe\n"
        "    def populate_entry_trend(self, dataframe, metadata): return dataframe\n"
        "    def populate_exit_trend(self, dataframe, metadata): return dataframe\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        '{"exchange":{"name":"binance","pair_whitelist":["BTC/USDT"]}}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    calls = 0

    def fake_vectors(**kwargs):
        nonlocal calls
        calls += 1
        destination = Path(kwargs["output_directory"]) / "BTC_USDT.feather"
        destination.write_bytes(b"vectors")
        return {
            "pipeline_version": research_runner.VECTOR_PIPELINE_VERSION,
            "pair_count": 1,
            "worker_count": 1,
            "cache_hits": 0,
            "outputs": [
                {
                    "pair": "BTC/USDT",
                    "path": str(destination),
                    "sha256": sha256_file(destination),
                }
            ],
        }

    monkeypatch.setattr(research_runner, "ensure_execution_profile", lambda *a, **k: _profile())
    monkeypatch.setattr(research_runner, "prepare_vector_signals", fake_vectors)
    monkeypatch.setattr(research_runner, "prepare_data", _fake_prepare_data)
    monkeypatch.setattr(research_runner, "validate_data_seal", read_json)
    output = tmp_path / "run"
    arguments = {
        "strategy_path": source,
        "class_name": "Strategy",
        "config_path": config,
        "data_directory": data,
        "timerange": "20250101-20250102",
        "output_directory": output,
        "profile_path": tmp_path / "profile.json",
        "prepare_only": True,
    }

    first = research_runner.run_research_backtest(**arguments)
    second = research_runner.run_research_backtest(**arguments, resume=True)

    assert first["status"] == "prepared"
    assert second["resumed_stages"] == ["data", "vectors"]
    assert calls == 1
    assert (output / "run.json").is_file()


def test_research_backtest_reports_uncompiled_callback_blocker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Strategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Strategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def custom_exit(self, pair, trade, current_time, current_rate, "
        "current_profit, **kwargs):\n"
        "        for order in trade.orders:\n"
        "            return order.ft_order_tag\n"
        "        return None\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        '{"exchange":{"name":"binance","pair_whitelist":["BTC/USDT"]}}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()

    def fake_vectors(**kwargs):
        destination = Path(kwargs["output_directory"]) / "BTC_USDT.feather"
        destination.write_bytes(b"vectors")
        return {
            "pipeline_version": research_runner.VECTOR_PIPELINE_VERSION,
            "pair_count": 1,
            "worker_count": 1,
            "cache_hits": 0,
            "outputs": [
                {
                    "pair": "BTC/USDT",
                    "path": str(destination),
                    "sha256": sha256_file(destination),
                }
            ],
        }

    monkeypatch.setattr(research_runner, "ensure_execution_profile", lambda *a, **k: _profile())
    monkeypatch.setattr(research_runner, "prepare_vector_signals", fake_vectors)
    monkeypatch.setattr(research_runner, "prepare_data", _fake_prepare_data)

    report = research_runner.run_research_backtest(
        strategy_path=source,
        class_name="Strategy",
        config_path=config,
        data_directory=data,
        timerange="20250101-20250102",
        output_directory=tmp_path / "run",
        profile_path=tmp_path / "profile.json",
    )

    assert report["status"] == "blocked_unsupported_semantics"
    assert report["capability"]["blockers"][0]["callback"] == "custom_exit"


def test_research_workers_cannot_exceed_hardware_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Strategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Strategy(IStrategy):\n"
        "    timeframe = '5m'\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        '{"exchange":{"name":"binance","pair_whitelist":["BTC/USDT"]}}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(research_runner, "ensure_execution_profile", lambda *a, **k: _profile())

    with pytest.raises(SpecValidationError, match="exceeds the hardware profile limit"):
        research_runner.run_research_backtest(
            strategy_path=source,
            class_name="Strategy",
            config_path=config,
            data_directory=data,
            timerange="20250101-20250102",
            output_directory=tmp_path / "run",
            workers=3,
            prepare_only=True,
        )
