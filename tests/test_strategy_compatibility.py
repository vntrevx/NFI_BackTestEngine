from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.canonical import read_json
from nfi_backtest_engine.strategy_compatibility import check_strategy_compatibility


def test_callback_free_revision_is_native_compatible(tmp_path: Path) -> None:
    source = tmp_path / "SimpleStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class SimpleStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    stoploss = -0.1\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )
    output = tmp_path / "compatibility.json"

    report = check_strategy_compatibility(source, output_path=output)

    assert report["native_compatible"] is True
    assert report["blockers"] == []
    assert report["callback_ir"]["hot_loop_ready"] is True
    assert read_json(output)["source"]["sha256"] == report["source"]["sha256"]


def test_invalid_revision_produces_a_durable_blocker(tmp_path: Path) -> None:
    source = tmp_path / "BrokenStrategy.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class BrokenStrategy(IStrategy)\n",
        encoding="utf-8",
    )

    report = check_strategy_compatibility(source)

    assert report["native_compatible"] is False
    assert report["static_safe"] is False
    assert report["blockers"][0]["code"] == "PYTHON_SYNTAX"
