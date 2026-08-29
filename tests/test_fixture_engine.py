from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl
import pytest
from nfi_backtest_engine import fixture_engine
from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.errors import BenchmarkError
from nfi_backtest_engine.fixture import validate_fixture
from nfi_backtest_engine.fixture_engine import (
    _fixture_data_directory,
    build_fixture_simulation_input,
    validate_native_manager_binding,
)

ROOT = Path(__file__).parents[1]
STOPS = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "stops-only-spot-2025-01-01_04"
    / "manifest.json"
)
NORMAL = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
    / "manifest.json"
)
COMPOUND_FUTURES = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "x7-compound-tag-futures-v17.4.435-2022-04-29_05-02"
    / "manifest.json"
)
TASK9_WALLET = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "current-long-grind-liquidation-rescue-wallet-boundary-futures-r1"
    / "manifest.json"
)
RELEASE_SPOT = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "release-candidate"
    / "x7-tag121-spot-v17.4.473-2023-01-01_02"
    / "manifest.json"
)


def test_official_consumed_interval_is_slot_complete_and_not_candle_minmax(
    tmp_path: Path,
) -> None:
    source = next(
        (ROOT / "benchmarks/fixtures/captured").glob(
            "current-portfolio-pressure-3p-*/manifest.json"
        )
    )
    fixture_root = tmp_path / "fixture"
    shutil.copytree(source.parent, fixture_root)
    manifest_path = fixture_root / "manifest.json"
    manifest = validate_fixture(manifest_path, validate_trace_semantics=False)

    identity = fixture_engine.official_consumed_interval(manifest_path, manifest)

    assert identity["native_timerange"] == "1735689600000-1735693200000"
    assert identity["official_event_start_timestamp_ms"] == 1735689900000
    assert identity["official_event_end_timestamp_ms"] == 1735692900000
    candle = next(item for item in manifest["inputs"] if item["role"] == "candles")
    candle_path = fixture_root / candle["path"]
    frame = pl.read_ipc(candle_path).filter(pl.col("date").dt.minute() != 30)
    frame.write_ipc(candle_path, compression="uncompressed")
    candle["bytes"] = candle_path.stat().st_size
    candle["sha256"] = fixture_engine.sha256_file(candle_path)

    with pytest.raises(BenchmarkError, match="official-consumed interval.*missing slot"):
        fixture_engine.official_consumed_interval(manifest_path, manifest)


def test_legacy_release_fixture_authenticates_from_its_sealed_full_trace() -> None:
    manifest = validate_fixture(RELEASE_SPOT, validate_trace_semantics=False)

    identity = fixture_engine.official_consumed_interval(RELEASE_SPOT, manifest)

    assert identity["native_timerange"] == "1672531200000-1672617900000"
    assert identity["configured_pairs"] == ["BTC/USDT"]
    assert identity["official_trace_sha256"] == manifest["artifacts"]["state_trace"]["sha256"]


@pytest.mark.parametrize("mutation", ["final", "parent", "in-place", "hardlink"])
def test_fixture_engine_uses_retained_strategy_after_validation_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "fixture"
    shutil.copytree(STOPS.parent, root)
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    strategy = root / next(
        item["path"] for item in manifest["inputs"] if item["role"] == "strategy"
    )
    original = strategy.read_bytes()
    outside = tmp_path / "outside.py"
    outside.write_bytes(original)
    outside_parent = tmp_path / "outside-inputs"
    shutil.copytree(strategy.parent, outside_parent)
    real_validate = validate_fixture

    def validate_then_swap(*args, **kwargs):
        validated = real_validate(*args, **kwargs)
        if mutation == "final":
            strategy.unlink()
            strategy.symlink_to(outside)
        elif mutation == "parent":
            strategy.parent.rename(root / "inputs-retained")
            (root / "inputs").symlink_to(outside_parent, target_is_directory=True)
        else:
            target = strategy
            if mutation == "hardlink":
                target = tmp_path / "strategy-alias.py"
                target.hardlink_to(strategy)
            changed = bytearray(original)
            changed[-1] = changed[-1] ^ 1
            target.write_bytes(changed)
        return validated

    class ConsumerReached(RuntimeError):
        pass

    def stop_at_analysis(path: str | Path, **_kwargs):
        consumed = Path(path)
        assert consumed.resolve() != outside
        assert consumed.read_bytes() == original
        raise ConsumerReached

    monkeypatch.setattr(fixture_engine, "validate_fixture", validate_then_swap)
    monkeypatch.setattr(fixture_engine, "analyze_strategy", stop_at_analysis)

    with pytest.raises(ConsumerReached):
        fixture_engine.run_fixture_engine(manifest_path, tmp_path / "output")


def test_stops_fixture_compiles_shifted_signal_arrays(tmp_path: Path) -> None:
    document = build_fixture_simulation_input(STOPS, tmp_path / "input.json")
    candles = document["pairs"][0]["candles"]

    assert len(candles) == 862
    assert sum(candle["enter_long"] is not None for candle in candles) > 0
    assert document["config"]["stoploss_ratio"] == -0.005
    assert document["config"]["adjustment_rule"] is None


def test_normal_fixture_compiles_stateful_callback_rules(tmp_path: Path) -> None:
    document = build_fixture_simulation_input(NORMAL, tmp_path / "input.json")

    assert document["config"]["custom_exit_after_ms"] == 21_600_000
    assert document["config"]["adjustment_rule"]["profit_below"] == -0.004
    assert document["config"]["adjustment_rule"]["max_adjustments"] == 1


def test_fixture_data_directory_supports_spot_and_futures_layouts() -> None:
    assert _fixture_data_directory(STOPS.parent, read_json(STOPS)) == (
        STOPS.parent / "inputs" / "candles"
    )
    assert _fixture_data_directory(
        COMPOUND_FUTURES.parent,
        read_json(COMPOUND_FUTURES),
    ) == (COMPOUND_FUTURES.parent / "inputs" / "data")


def test_wallet_branch_probe_replays_sealed_native_manifest(tmp_path: Path) -> None:
    # Given: official wallet-boundary evidence plus its source-derived Native input.
    output = tmp_path / "wallet-replay"

    # When: the public fixture command executes full trade and state verification.
    report = fixture_engine.run_fixture_engine(
        TASK9_WALLET,
        output,
        verification_level="full",
    )

    # Then: the sealed Native lane is exact through the same user surface.
    assert report["complete"] is True
    assert report["parity"]["trade_surface"]["equal"] is True
    assert report["parity"]["state_trace"]["equal"] is True
    assert report["artifacts"]["simulation_input"]["path"].endswith(
        "simulation-input.manifest.json"
    )
    manifest = read_json(TASK9_WALLET)
    native_record = manifest["artifacts"]["native_vector_manifest"]
    native = read_json(TASK9_WALLET.parent / native_record["path"])
    manager = native["config"]["nfi_x7_trade_manager"]
    assert manager["source_sha256"] == manifest["strategy_provenance"]["base_source_sha256"]
    gd5 = next(
        transition
        for transition in manager["long_grind"]["program"]["source_order"]
        if transition.get("entry_tag") == "gd5"
    )
    assert gd5["liquidation_rescue"] == {
        "cluster_level": 5,
        "loss_threshold": -0.12,
        "liquidation_multiplier": 1.2,
        "used_state_key": "gd5_liquidation_rescue_used",
    }


@pytest.mark.parametrize("mutation", ["missing", "mismatched"])
def test_native_manager_binding_fails_closed(mutation: str) -> None:
    # Given: a sealed Native lane that claims one authenticated base source.
    manifest = {"strategy_provenance": {"base_source_sha256": "a" * 64}}
    manager = None if mutation == "missing" else {"source_sha256": "b" * 64}

    # When/Then: absent or cross-source manager semantics fail before execution.
    with pytest.raises(
        BenchmarkError,
        match="compiled NFI trade manager|differs from fixture provenance",
    ):
        validate_native_manager_binding(
            manifest,
            {"config": {"nfi_x7_trade_manager": manager}},
        )
