from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from nfi_backtest_engine.canonical import read_json, write_json
from nfi_backtest_engine.data_seal import inspect_candle_quality
from nfi_backtest_engine.errors import BenchmarkError, SpecValidationError
from nfi_backtest_engine.release_inputs import (
    select_release_universe,
    validate_release_input_lock,
)

ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "captured"
    / "normal-routing-spot-2025-01-01_04"
)


def test_release_selector_seals_strict_complete_pairs_in_source_order(
    tmp_path: Path,
) -> None:
    candidates = tmp_path / "candidates.json"
    write_json(candidates, {"pairs": ["BTC/USDT"]})

    lock = select_release_universe(
        candidates_path=candidates,
        strategy_path=FIXTURE / "inputs" / "strategy.py",
        class_name="ContractNormalRouting",
        config_path=FIXTURE / "inputs" / "config.json",
        data_directory=FIXTURE / "inputs" / "candles",
        timerange="1735690800-1735948800",
        output_directory=tmp_path / "release-inputs",
        pair_count=1,
        upstream_repository="https://github.com/iterativv/NostalgiaForInfinity",
        upstream_commit="a" * 40,
    )

    assert lock["pairlist"]["pairs"] == ["BTC/USDT"]
    assert lock["data"]["coverage_shortfall_count"] == 0
    assert lock["data"]["startup_shortfall_count"] == 0
    validate_release_input_lock(lock, required_pair_count=1)

    changed = read_json(tmp_path / "release-inputs" / "release-input-lock.json")
    changed["pairlist"]["pairs"] = ["ETH/USDT"]
    with pytest.raises(SpecValidationError, match="identity is corrupt"):
        validate_release_input_lock(changed, required_pair_count=1)


def test_release_selector_rejects_duplicate_candle_timestamps(tmp_path: Path) -> None:
    source = FIXTURE / "inputs" / "candles" / "BTC_USDT-5m.feather"
    frame = pd.read_feather(source)
    frame = pd.concat([frame.iloc[:1], frame], ignore_index=True)
    data = tmp_path / "data"
    data.mkdir()
    frame.to_feather(data / source.name)
    candidates = tmp_path / "candidates.json"
    write_json(candidates, ["BTC/USDT"])

    quality = inspect_candle_quality(data / source.name, timeframe="5m")
    assert quality["duplicate_timestamp_count"] == 1

    with pytest.raises(BenchmarkError, match="only 0 candidates"):
        select_release_universe(
            candidates_path=candidates,
            strategy_path=FIXTURE / "inputs" / "strategy.py",
            class_name="ContractNormalRouting",
            config_path=FIXTURE / "inputs" / "config.json",
            data_directory=data,
            timerange="1735690800-1735948800",
            output_directory=tmp_path / "release-inputs",
            pair_count=1,
            upstream_repository=(
                "https://github.com/iterativv/NostalgiaForInfinity"
            ),
            upstream_commit="a" * 40,
        )
    report = read_json(tmp_path / "release-inputs" / "selection-report.json")
    assert report["rejected_candidates"][0]["reasons"][-1]["code"] == (
        "DUPLICATE_TIMESTAMPS"
    )
