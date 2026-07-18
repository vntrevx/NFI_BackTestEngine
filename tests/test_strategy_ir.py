from __future__ import annotations

from pathlib import Path

import pytest
from nfi_backtest_engine.errors import StrategyAnalysisError
from nfi_backtest_engine.strategy_ir import (
    analyze_strategy,
    prepare_strategy,
    validate_strategy_bundle,
)

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "benchmarks" / "reference" / "strategies" / "ContractNormalRouting.py"


def test_contract_strategy_inventory_finds_vector_and_hot_methods() -> None:
    analysis = analyze_strategy(CONTRACT, class_name="ContractNormalRouting")
    strategy = analysis["strategies"][0]

    assert analysis["static_safe"]
    assert strategy["required_timeframes"] == ["5m"]
    assert strategy["hot_callbacks"] == ["adjust_trade_position", "custom_exit"]
    assert strategy["vector_methods"] == [
        "populate_entry_trend",
        "populate_exit_trend",
        "populate_indicators",
    ]


def test_dynamic_and_lookahead_code_reports_exact_source_locations(tmp_path: Path) -> None:
    source = tmp_path / "Unsafe.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Unsafe(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        eval('1 + 1')\n"
        "        getattr(self, metadata['method'])()\n"
        "        dataframe['future'] = dataframe['close'].shift(-1)\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    analysis = analyze_strategy(source)

    assert not analysis["static_safe"]
    assert [(item["code"], item["location"]["line"]) for item in analysis["diagnostics"]] == [
        ("DYNAMIC_EXECUTION", 5),
        ("DYNAMIC_ATTRIBUTE", 6),
        ("LOOKAHEAD_NEGATIVE_SHIFT", 7),
    ]


def test_prepare_refuses_unsafe_source_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "Unsafe.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Unsafe(IStrategy):\n"
        "    def custom_exit(self, *args):\n"
        "        return exec('pass')\n",
        encoding="utf-8",
    )

    with pytest.raises(StrategyAnalysisError, match="DYNAMIC_EXECUTION"):
        prepare_strategy(source, tmp_path / "bundle")


def test_prepared_bundle_is_hash_bound(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    manifest = prepare_strategy(CONTRACT, bundle, class_name="ContractNormalRouting")

    assert manifest["selected_class"] == "ContractNormalRouting"
    assert validate_strategy_bundle(bundle)["strategy"]["sha256"] == manifest["strategy"]["sha256"]


def test_dynamic_attribute_in_one_time_initialization_requires_freeze_not_fallback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Configured.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Configured(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def __init__(self, config):\n"
        "        setattr(self, config['name'], config['value'])\n",
        encoding="utf-8",
    )

    analysis = analyze_strategy(source)

    assert analysis["static_safe"]
    assert analysis["diagnostics"][0]["severity"] == "warning"
    assert analysis["diagnostics"][0]["code"] == "DYNAMIC_ATTRIBUTE_INIT"


def test_literal_informative_timeframe_lists_are_discovered(tmp_path: Path) -> None:
    source = tmp_path / "Informative.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Informative(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    info_timeframes = ['15m', '1h', '4h']\n",
        encoding="utf-8",
    )

    analysis = analyze_strategy(source)

    assert analysis["strategies"][0]["required_timeframes"] == ["5m", "15m", "1h", "4h"]
