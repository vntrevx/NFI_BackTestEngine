from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine import research_runner
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import sha256_file


def _profile() -> dict:
    return {
        "schema_version": "2.0.0",
        "created_at": "2026-01-01T00:00:00Z",
        "hardware_fingerprint": "hardware",
        "hardware": {
            "platform": "test",
            "machine": "x86_64",
            "cpu_name": "test",
            "physical_cpu_count": 2,
            "logical_cpu_count": 2,
            "affinity_cpu_count": 2,
            "affinity_cpu_ids": [0, 1],
            "memory": {
                "total_bytes": 16 * 1024**3,
                "available_bytes": 8 * 1024**3,
            },
        },
        "limits": {
            "memory_cap_bytes": 8 * 1024**3,
            "cpu_process_limit": 2,
        },
        "runtime": {
            "portfolio_simulator_threads": 1,
            "nested_numeric_threads": 1,
        },
        "environment": {"OMP_NUM_THREADS": "1"},
    }


def _fake_prepare_data(**kwargs) -> dict:
    seal = {
        "aggregate_sha256": "data",
        "files": [{"path": "BTC_USDT-5m.feather"}],
        "downloads": [],
        "coverage_shortfalls": [],
        "request": {
            "history_coverage_policy": kwargs.get(
                "history_coverage_policy",
                "strict",
            )
        },
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
    monkeypatch.setattr(
        research_runner,
        "current_resource_limits",
        lambda _profile: {
            "memory_cap_bytes": 8 * 1024**3,
            "working_memory_bytes": 8 * 1024**3,
            "cpu_process_limit": 2,
        },
    )
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
    assert first["pipeline_evidence"]["cold"] is True
    assert first["schema_version"] == "1.4.0"
    assert first["timings"]["pipeline_wall_time_seconds"] >= 0
    assert set(first["timings"]["stages"]) == {
        "input_preparation_seconds",
        "data_seconds",
        "vectors_seconds",
        "capability_seconds",
        "manifest_seconds",
        "engine_seconds",
        "surface_seconds",
    }
    assert all(value >= 0 for value in first["timings"]["stages"].values())
    assert second["resumed_stages"] == ["data", "vectors"]
    assert second["pipeline_evidence"]["cold"] is False
    assert calls == 1
    assert (output / "run.json").is_file()
    assert (output / first["inputs"]["strategy"]["sealed"]["path"]).read_bytes() == (
        source.read_bytes()
    )
    assert read_json(output / first["inputs"]["config"]["sealed"]["path"]) == read_json(
        output / "effective-config.redacted.json"
    )["config"]


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
    monkeypatch.setattr(
        research_runner,
        "current_resource_limits",
        lambda _profile: {
            "memory_cap_bytes": 8 * 1024**3,
            "working_memory_bytes": 8 * 1024**3,
            "cpu_process_limit": 2,
        },
    )
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
    monkeypatch.setattr(
        research_runner,
        "current_resource_limits",
        lambda _profile: {
            "memory_cap_bytes": 8 * 1024**3,
            "working_memory_bytes": 8 * 1024**3,
            "cpu_process_limit": 2,
        },
    )

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
