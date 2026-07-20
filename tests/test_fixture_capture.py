from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import SpecValidationError
from nfi_backtest_engine.fixture import fixture_input_sha256
from nfi_backtest_engine.fixture_capture import stage_fixture_v2


def test_stage_fixture_copies_inputs_and_returns_trace_identity(tmp_path: Path) -> None:
    strategy = tmp_path / "source.py"
    config = tmp_path / "source.json"
    candles = tmp_path / "candles.feather"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    config.write_text("{}\n", encoding="utf-8")
    candles.write_bytes(b"feather")

    stage = stage_fixture_v2(
        tmp_path / "fixture",
        fixture_id="captured-test",
        description="test capture",
        fixture_kind="stops-only",
        strategy=strategy,
        config=config,
        inputs=[("candles", candles)],
    )

    assert stage["input_sha256"] == fixture_input_sha256(stage["inputs"])
    assert (tmp_path / "fixture" / "inputs" / "strategy.py").read_text(
        encoding="utf-8"
    ) == "class Strategy: pass\n"


def test_stage_fixture_refuses_unknown_input_role(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("x", encoding="utf-8")

    with pytest.raises(SpecValidationError, match="unsupported capture input role"):
        stage_fixture_v2(
            tmp_path / "fixture",
            fixture_id="captured-test",
            description="test capture",
            fixture_kind="stops-only",
            strategy=source,
            config=source,
            inputs=[("secret", source)],
        )


def test_stage_fixture_preserves_explicit_freqtrade_data_layout(
    tmp_path: Path,
) -> None:
    strategy = tmp_path / "source.py"
    config = tmp_path / "source.json"
    candles = tmp_path / "candles.feather"
    strategy.write_text("class Strategy: pass\n", encoding="utf-8")
    config.write_text("{}\n", encoding="utf-8")
    candles.write_bytes(b"feather")

    stage = stage_fixture_v2(
        tmp_path / "fixture",
        fixture_id="explicit-layout",
        description="explicit data layout",
        fixture_kind="normal-routing",
        strategy=strategy,
        config=config,
        inputs=[
            (
                "candles",
                candles,
                "inputs/data/spot/BTC_USDT-5m.feather",
            )
        ],
    )

    candle = next(item for item in stage["inputs"] if item["role"] == "candles")
    assert candle["path"] == "inputs/data/spot/BTC_USDT-5m.feather"
