from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import sha256_file
from nfi_backtest_engine.reference_runtime import REFERENCE_CCXT_VERSION
from nfi_backtest_engine.research_reference import (
    RESEARCH_REFERENCE_VERSION,
    _official_backtest_config,
    _sealed_input_path,
    _validate_audit_timestamps,
    _validate_reference_market_snapshot,
    build_research_market_capture_command,
    build_research_reference_command,
)


def test_research_reference_command_keeps_inputs_as_argv(tmp_path: Path) -> None:
    command = build_research_reference_command(
        run_prefix=["docker", "run", "--rm"],
        input_directory=tmp_path / "inputs",
        output_directory=tmp_path / "output",
        data_directory=tmp_path / "data",
        strategy="NostalgiaForInfinityX7",
        timerange="20210101-20260101",
        pairs=["BTC/USDT:USDT", "ETH/USDT:USDT"],
        audit_timestamps_ms=[1_650_000_000_000],
    )

    pairs_index = command.index("--pairs")
    assert command[pairs_index + 1 : pairs_index + 3] == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    ]
    assert "NFI_CALLBACK_AUDIT_TIMESTAMPS_MS=1650000000000" in command
    assert "NFI_REFERENCE_DATASTORE=spooled" in command
    assert "NFI_REFERENCE_STORAGE_REPORT=/output/reference-storage.json" in command
    assert any(value.endswith(":/nfi-reference-tracer:ro") for value in command)
    assert any(
        value.endswith(":/nfi-python/nfi_backtest_engine:ro") for value in command
    )
    assert "PYTHONPATH=/nfi-reference-tracer:/nfi-python" in command
    assert command[-6:] == [
        "--cache",
        "none",
        "--export",
        "trades",
        "--backtest-directory",
        "/output",
    ]


def test_market_capture_uses_the_pinned_list_pairs_contract(tmp_path: Path) -> None:
    command = build_research_market_capture_command(
        input_directory=tmp_path / "inputs",
        output_directory=tmp_path / "output",
    )

    assert command[-5:] == [
        "list-pairs",
        "--config",
        "/input/market-config.json",
        "--userdir",
        "/output/user_data",
    ]
    assert "--json" not in command
    assert "NFI_MARKET_CAPTURE_PATH=/output/reference-markets.json" in command
    assert any(value.endswith(":/nfi-reference-tracer:ro") for value in command)
    assert any(
        value.endswith(":/nfi-python/nfi_backtest_engine:ro") for value in command
    )


def test_official_config_drops_only_the_unrelated_api_service() -> None:
    source = {
        "strategy": "NostalgiaForInfinityX7",
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "api_server": {
            "enabled": True,
            "jwt_secret_key": "",
        },
    }

    result = _official_backtest_config(source)

    assert result == {
        "strategy": "NostalgiaForInfinityX7",
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "pairlists": [{"method": "StaticPairList", "allow_inactive": True}],
    }
    assert source["api_server"]["jwt_secret_key"] == ""


def test_official_config_rejects_an_unexecuted_dynamic_pairlist() -> None:
    with pytest.raises(BenchmarkError, match="static sealed pairlist"):
        _official_backtest_config(
            {
                "exchange": {"name": "binance"},
                "pairlists": [{"method": "VolumePairList", "number_assets": 20}],
            }
        )


def test_reference_market_snapshot_requires_every_data_pair() -> None:
    snapshot = {
        "schema_version": "1.0.0",
        "freqtrade_version": "2026.5.1",
        "ccxt_version": REFERENCE_CCXT_VERSION,
        "exchange": "binance",
        "trading_mode": "futures",
        "markets": {"BTC/USDT:USDT": {}},
    }

    with pytest.raises(BenchmarkError, match="ETH/USDT:USDT"):
        _validate_reference_market_snapshot(
            snapshot,
            expected_exchange="binance",
            expected_trading_mode="futures",
            required_pairs=["BTC/USDT:USDT", "ETH/USDT:USDT"],
        )


def test_callback_audit_timestamps_are_sorted_unique_and_nonnegative() -> None:
    assert _validate_audit_timestamps([30, 10, 30]) == [10, 30]
    with pytest.raises(BenchmarkError, match="non-negative"):
        _validate_audit_timestamps([-1])
    assert RESEARCH_REFERENCE_VERSION == "1.3.0"


def test_research_reference_can_retain_the_in_memory_diagnostic_baseline(
    tmp_path: Path,
) -> None:
    command = build_research_reference_command(
        run_prefix=["docker", "run", "--rm"],
        input_directory=tmp_path / "inputs",
        output_directory=tmp_path / "output",
        data_directory=tmp_path / "data",
        strategy="NostalgiaForInfinityX7",
        timerange="20250101-20250102",
        pairs=["BTC/USDT"],
        audit_timestamps_ms=[],
        storage_mode="in-memory",
    )

    assert "NFI_REFERENCE_DATASTORE=spooled" not in command
    assert "NFI_REFERENCE_STORAGE_REPORT=/output/reference-storage.json" not in command


def test_research_reference_trace_is_full_state_and_hash_bound(
    tmp_path: Path,
) -> None:
    command = build_research_reference_command(
        run_prefix=["docker", "run"],
        input_directory=tmp_path / "input",
        output_directory=tmp_path / "output",
        data_directory=tmp_path / "data",
        strategy="NostalgiaForInfinityX7",
        timerange="20250101-20250102",
        pairs=["BTC/USDT"],
        audit_timestamps_ms=[],
        trace_identity={
            "run_id": "probe",
            "input_sha256": "a" * 64,
            "strategy_sha256": "b" * 64,
            "profile_sha256": "c" * 64,
            "trading_mode": "spot",
        },
        dependency_directory=tmp_path / "dependencies",
    )

    joined = " ".join(command)
    assert "NFI_TRACE_PATH=/output/state-trace.nfitrace" in command
    assert "NFI_TRACE_INCLUDE_STATE=1" in command
    assert "NFI_TRACE_INPUT_SHA256=" + "a" * 64 in command
    assert "/reference-deps:ro" in joined


def test_sealed_input_survives_the_original_source_path(tmp_path: Path) -> None:
    sealed = tmp_path / "sealed-inputs" / "strategy.py"
    sealed.parent.mkdir()
    sealed.write_text("class Strategy: pass\n", encoding="utf-8")

    resolved = _sealed_input_path(
        tmp_path,
        {
            "path": "sealed-inputs/strategy.py",
            "sha256": sha256_file(sealed),
        },
    )

    assert resolved == sealed
