from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from nfi_backtest_engine import research_runner
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.fixture import sha256_file

SURFACE_FIXTURE = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "artifacts"
    / "trade-surface.json"
)


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


def _resume_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fail_first_vector_attempt: bool = False,
    fail_first_surface_attempt: bool = False,
) -> tuple[dict, dict[str, int]]:
    source = tmp_path / "Strategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Strategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    stoploss = -0.1\n"
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
    markets = tmp_path / "markets.json"
    write_json(markets, {"exchange": "binance", "pairs": {}})
    calls = {"data": 0, "vectors": 0, "engine": 0, "surface": 0}

    def fake_data(**kwargs):
        calls["data"] += 1
        return _fake_prepare_data(**kwargs)

    def fake_vectors(**kwargs):
        calls["vectors"] += 1
        destination = Path(kwargs["output_directory"])
        partial = destination / "partial.feather"
        if fail_first_vector_attempt and calls["vectors"] == 1:
            partial.write_bytes(b"incomplete")
            raise RuntimeError("interrupted vector stage")
        assert not partial.exists()
        artifact = destination / "BTC_USDT.feather"
        artifact.write_bytes(b"vectors")
        return {
            "pipeline_version": research_runner.VECTOR_PIPELINE_VERSION,
            "pair_count": 1,
            "worker_count": 1,
            "cache_hits": 0,
            "outputs": [
                {
                    "pair": "BTC/USDT",
                    "path": str(artifact),
                    "sha256": sha256_file(artifact),
                }
            ],
        }

    def fake_manifest(**kwargs):
        write_json(kwargs["destination"], {"schema_version": "test"})

    def fake_engine(_input, output, **kwargs):
        calls["engine"] += 1
        write_json(output, {"schema_version": "test"})
        profile_path = kwargs.get("engine_profile_path")
        if profile_path is not None:
            write_json(profile_path, {"schema_version": "test"})
        return {
            "wall_time_seconds": 0.1,
            "peak_rss_bytes": 1024,
        }

    def fake_surface(**kwargs):
        calls["surface"] += 1
        if fail_first_surface_attempt and calls["surface"] == 1:
            raise RuntimeError("interrupted surface stage")
        destination = Path(kwargs["destination"])
        shutil.copyfile(SURFACE_FIXTURE, destination)
        return read_json(destination)

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
    monkeypatch.setattr(research_runner, "prepare_data", fake_data)
    monkeypatch.setattr(research_runner, "validate_data_seal", read_json)
    monkeypatch.setattr(research_runner, "prepare_vector_signals", fake_vectors)
    monkeypatch.setattr(research_runner, "generic_adapter_blockers", lambda *a, **k: [])
    monkeypatch.setattr(research_runner, "generic_data_blockers", lambda *a, **k: [])
    monkeypatch.setattr(research_runner, "build_generic_vector_manifest", fake_manifest)
    monkeypatch.setattr(research_runner, "run_engine", fake_engine)
    monkeypatch.setattr(research_runner, "generic_result_to_surface", fake_surface)
    arguments = {
        "strategy_path": source,
        "class_name": "Strategy",
        "config_path": config,
        "data_directory": data,
        "timerange": "20250101-20250102",
        "output_directory": tmp_path / "run",
        "profile_path": tmp_path / "profile.json",
        "market_metadata_path": markets,
    }
    return arguments, calls


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
    assert first["schema_version"] == "1.5.0"
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
    assert second == first
    assert second["resumed_stages"] == []
    assert second["pipeline_evidence"]["cold"] is True
    assert calls == 1
    assert (output / "run.json").is_file()
    assert (output / "summary.json").is_file()
    assert (output / "trades.csv").is_file()
    assert (output / "report.html").is_file()
    assert (output / first["inputs"]["strategy"]["sealed"]["path"]).read_bytes() == (
        source.read_bytes()
    )
    assert (
        read_json(output / first["inputs"]["config"]["sealed"]["path"])
        == read_json(output / "effective-config.redacted.json")["config"]
    )


def test_completed_resume_returns_verified_result_without_running_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(monkeypatch, tmp_path)
    first = research_runner.run_research_backtest(**arguments)
    output = Path(arguments["output_directory"])
    evidence_before = {
        name: (output / name).read_bytes()
        for name in (
            "run.json",
            "simulation-input.manifest.json",
            "simulation-result.json",
            "trade-surface.json",
        )
    }

    resumed = research_runner.run_research_backtest(
        **arguments,
        resume=True,
    )

    assert resumed == first
    assert calls["engine"] == 1
    assert evidence_before == {
        name: (output / name).read_bytes()
        for name in evidence_before
    }


def test_completed_resume_rejects_tampered_result_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(monkeypatch, tmp_path)
    research_runner.run_research_backtest(**arguments)
    result = Path(arguments["output_directory"]) / "simulation-result.json"
    result.write_bytes(result.read_bytes() + b"\n")
    tampered = result.read_bytes()

    with pytest.raises(BenchmarkError, match="simulation result.*(bytes|SHA-256)"):
        research_runner.run_research_backtest(
            **arguments,
            resume=True,
        )

    assert calls["engine"] == 1
    assert result.read_bytes() == tampered


def test_completed_resume_rejects_missing_simulation_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(monkeypatch, tmp_path)
    research_runner.run_research_backtest(**arguments)
    output = Path(arguments["output_directory"])
    result_before = (output / "simulation-result.json").read_bytes()
    (output / "checkpoints" / "simulation.json").unlink()

    with pytest.raises(BenchmarkError, match="missing its simulation checkpoint"):
        research_runner.run_research_backtest(
            **arguments,
            resume=True,
        )

    assert calls["engine"] == 1
    assert (output / "simulation-result.json").read_bytes() == result_before


def test_completed_legacy_resume_validates_evidence_without_migrating_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(monkeypatch, tmp_path)
    research_runner.run_research_backtest(**arguments)
    output = Path(arguments["output_directory"])
    identity_path = output / "identity.json"
    run_path = output / "run.json"
    identity_document = read_json(identity_path)
    legacy_identity = research_runner._legacy_resume_identity(
        identity_document["identity"]
    )
    assert legacy_identity is not None
    legacy_run_id = research_runner._identity_sha256(legacy_identity)
    legacy_report = read_json(run_path)
    legacy_report["schema_version"] = research_runner.LEGACY_RESEARCH_RUN_VERSION
    legacy_report["run_id"] = legacy_run_id
    legacy_report["inputs"] = legacy_identity
    write_json(
        identity_path,
        {"run_id": legacy_run_id, "identity": legacy_identity},
    )
    write_json(run_path, legacy_report)
    (output / "checkpoints" / "simulation.json").unlink()
    evidence_before = {
        name: (output / name).read_bytes()
        for name in (
            "identity.json",
            "run.json",
            "simulation-input.manifest.json",
            "simulation-result.json",
            "trade-surface.json",
        )
    }

    resumed = research_runner.run_research_backtest(
        **arguments,
        resume=True,
    )

    assert resumed == legacy_report
    assert calls["engine"] == 1
    assert evidence_before == {
        name: (output / name).read_bytes()
        for name in evidence_before
    }


def test_prepare_only_resume_continues_from_simulation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(monkeypatch, tmp_path)
    prepared = research_runner.run_research_backtest(
        **arguments,
        prepare_only=True,
    )
    completed = research_runner.run_research_backtest(
        **arguments,
        resume=True,
    )

    assert prepared["status"] == "prepared"
    assert completed["status"] == "complete"
    assert completed["resumed_stages"][:2] == ["data", "vectors"]
    assert calls == {"data": 1, "vectors": 1, "engine": 1, "surface": 1}


def test_surface_interruption_reuses_checkpointed_simulation_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(
        monkeypatch,
        tmp_path,
        fail_first_surface_attempt=True,
    )

    with pytest.raises(RuntimeError, match="interrupted surface stage"):
        research_runner.run_research_backtest(**arguments)

    output = Path(arguments["output_directory"])
    simulation_before = (output / "simulation-result.json").read_bytes()
    resumed = research_runner.run_research_backtest(
        **arguments,
        resume=True,
    )

    assert resumed["status"] == "complete"
    assert resumed["resumed_stages"] == [
        "data",
        "vectors",
        "simulation_input",
        "simulation_result",
    ]
    assert calls == {"data": 1, "vectors": 1, "engine": 1, "surface": 2}
    assert (output / "simulation-result.json").read_bytes() == simulation_before


def test_vector_interruption_reuses_only_completed_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments, calls = _resume_workspace(
        monkeypatch,
        tmp_path,
        fail_first_vector_attempt=True,
    )

    with pytest.raises(RuntimeError, match="interrupted vector stage"):
        research_runner.run_research_backtest(
            **arguments,
            prepare_only=True,
        )

    resumed = research_runner.run_research_backtest(
        **arguments,
        resume=True,
        prepare_only=True,
    )

    assert resumed["status"] == "prepared"
    assert resumed["resumed_stages"] == ["data"]
    assert calls == {"data": 1, "vectors": 2, "engine": 0, "surface": 0}


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
