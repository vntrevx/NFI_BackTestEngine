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


def test_prepared_bundle_preserves_crlf_source_identity(tmp_path: Path) -> None:
    """Windows checkout line endings are part of the sealed strategy bytes."""
    source = tmp_path / "WindowsStrategy.py"
    source.write_bytes(
        b"from freqtrade.strategy import IStrategy\r\n"
        b"class WindowsStrategy(IStrategy):\r\n"
        b"    timeframe = '5m'\r\n"
    )
    bundle = tmp_path / "bundle"

    manifest = prepare_strategy(source, bundle, class_name="WindowsStrategy")

    assert (bundle / "strategy.py").read_bytes() == source.read_bytes()
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


def test_class_constant_aliases_resolve_only_from_prior_static_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Aliases.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Aliases(IStrategy):\n"
        "    system_v3_name = 'system_v3'\n"
        "    system_name_use = system_v3_name\n"
        "    unresolved = later_value\n"
        "    later_value = 'later'\n",
        encoding="utf-8",
    )

    strategy = analyze_strategy(source)["strategies"][0]

    assert strategy["constants"]["system_name_use"] == "system_v3"
    assert "unresolved" in strategy["dynamic_constants"]


def test_annotated_class_constants_use_the_same_bounded_static_evaluator(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Annotated.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Annotated(IStrategy):\n"
        "    startup_candle_count: int = 800\n"
        "    timeframe: str = '5m'\n"
        "    declaration_only: int\n"
        "    unsafe: list[str] = make_tags()\n",
        encoding="utf-8",
    )

    strategy = analyze_strategy(source)["strategies"][0]

    assert strategy["constants"]["startup_candle_count"] == 800
    assert strategy["constants"]["timeframe"] == "5m"
    assert "declaration_only" not in strategy["constants"]
    assert "unsafe" in strategy["dynamic_constants"]


def test_bounded_class_constant_expressions_resolve_without_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Expressions.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Expressions(IStrategy):\n"
        "    base_tags = ['101', '102']\n"
        "    grind_tags = ['120']\n"
        "    combined_tags = base_tags + grind_tags\n"
        "    multiplier = 1 / 4\n"
        "    unsafe = make_tags()\n",
        encoding="utf-8",
    )

    strategy = analyze_strategy(source)["strategies"][0]

    assert strategy["constants"]["combined_tags"] == ["101", "102", "120"]
    assert strategy["constants"]["multiplier"] == 0.25
    assert "unsafe" in strategy["dynamic_constants"]


def test_literal_condition_index_inventory_distinguishes_tags_from_routes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "IndexedSignals.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class IndexedSignals(IStrategy):\n"
        "    long_btc_mode_tags = ['121']\n"
        "    def populate_entry_trend(self, dataframe, metadata):\n"
        "        long_entry_condition_index = 0\n"
        "        if long_entry_condition_index == 120:\n"
        "            dataframe['enter_long'] = 1\n"
        "        if 141 == long_entry_condition_index:\n"
        "            dataframe['enter_long'] = 1\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    strategy = analyze_strategy(source)["strategies"][0]

    assert strategy["constants"]["long_btc_mode_tags"] == ["121"]
    assert strategy["literal_condition_indices"] == {
        "populate_entry_trend": {
            "long_entry_condition_index": [120, 141],
        }
    }
    assert (
        121
        not in strategy["literal_condition_indices"]["populate_entry_trend"][
            "long_entry_condition_index"
        ]
    )
