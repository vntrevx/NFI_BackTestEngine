from __future__ import annotations

import ast
from pathlib import Path

from nfi_backtest_engine import cli
from nfi_backtest_engine.indicator_inventory import _expression_text, build_indicator_inventory
from nfi_backtest_engine.specs import INDICATOR_INVENTORY_SCHEMA, validate_schema


def _write_indicator_strategy(path: Path) -> None:
    path.write_text(
        "import pandas as pd\n"
        "import talib.abstract as ta\n"
        "from freqtrade.strategy import IStrategy, merge_informative_pair\n"
        "class IndicatorStrategy(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    info_timeframes = ['1h']\n"
        "    def informative_pairs(self):\n"
        "        pairs = self.dp.current_whitelist()\n"
        "        return [(pair, tf) for pair in pairs for tf in self.info_timeframes]\n"
        "    @staticmethod\n"
        "    def helper(values, ta_min):\n"
        "        return ta_min(values, timeperiod=3)\n"
        "    def informative_1h(self, metadata, timeframe):\n"
        "        frame = self.dp.get_pair_dataframe(\n"
        "            pair=metadata['pair'], timeframe=timeframe\n"
        "        )\n"
        "        frame['rsi'] = ta.RSI(frame['close'], timeperiod=14)\n"
        "        return frame\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        ta_min = ta.MIN\n"
        "        dataframe['low3'] = self.helper(dataframe['low'], ta_min)\n"
        "        dataframe['mean4'] = pd.Series(dataframe['close']).rolling(4).mean()\n"
        "        dataframe['ewm4'] = dataframe['close'].ewm(span=4).mean()\n"
        "        info = self.informative_1h(metadata, '1h')\n"
        "        dataframe = merge_informative_pair(\n"
        "            dataframe, info, self.timeframe, '1h', ffill=False\n"
        "        )\n"
        "        return dataframe.ffill()\n",
        encoding="utf-8",
    )


def test_indicator_inventory_is_deterministic_and_covers_helpers_and_informative_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "IndicatorStrategy.py"
    _write_indicator_strategy(source)

    report = build_indicator_inventory(
        source,
        class_name="IndicatorStrategy",
        upstream_repository="https://example.invalid/upstream.git",
        upstream_commit="a" * 40,
    )

    validate_schema(report, INDICATOR_INVENTORY_SCHEMA)
    node_names = {node["name"] for node in report["call_graph"]["nodes"]}
    assert node_names == {
        "helper",
        "informative_1h",
        "informative_pairs",
        "populate_indicators",
    }
    operations = {operation["callable"]: operation for operation in report["operations"]}
    assert {
        "talib.MIN",
        "talib.RSI",
        "pandas.Series",
        "pandas.rolling",
        "pandas.ewm",
        "pandas.ffill",
        "freqtrade.informative.current_whitelist",
        "freqtrade.informative.get_pair_dataframe",
        "freqtrade.informative.merge_informative_pair",
    } <= operations.keys()
    assert operations["talib.MIN"]["occurrences"][0]["lookback"]["parameters"] == [
        {
            "name": "timeperiod",
            "expression": "3",
            "literal": 3,
        }
    ]
    rolling = operations["pandas.rolling"]["occurrences"][0]["lookback"]
    assert rolling["causal"] is True
    assert rolling["parameters"] == [
        {
            "name": "window",
            "expression": "4",
            "literal": 4,
        }
    ]
    assert report["informative_dependencies"]["complete"] is True
    assert report["informative_dependencies"]["informative_timeframes"] == ["1h"]
    assert len(report["informative_dependencies"]["dataframe_requests"]) == 1
    assert len(report["informative_dependencies"]["merge_operations"]) == 1
    assert report["summary"]["unresolved_call_site_count"] == 0
    assert report["summary"]["inventory_complete"] is True

    repeated_path = tmp_path / "copy" / "IndicatorStrategy.py"
    repeated_path.parent.mkdir()
    repeated_path.write_bytes(source.read_bytes())
    repeated = build_indicator_inventory(
        repeated_path,
        class_name="IndicatorStrategy",
        upstream_repository="https://mirror.invalid/upstream.git",
        upstream_commit="a" * 40,
    )
    assert repeated["fingerprint"] == report["fingerprint"]

    repeated_path.write_text(
        repeated_path.read_text(encoding="utf-8").replace("timeperiod=3", "timeperiod=5"),
        encoding="utf-8",
    )
    mutated = build_indicator_inventory(
        repeated_path,
        class_name="IndicatorStrategy",
        upstream_commit="a" * 40,
    )
    assert mutated["fingerprint"] != report["fingerprint"]


def test_indicator_inventory_records_all_required_coverage_families_when_absent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Minimal.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class Minimal(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    report = build_indicator_inventory(source, class_name="Minimal")
    coverage = {row["family"]: row for row in report["coverage_matrix"]}

    assert {"talib", "qtpylib", "pandas", "rolling", "informative"} <= coverage.keys()
    assert all(not coverage[family]["present"] for family in coverage)
    assert report["informative_dependencies"]["complete"] is True
    assert report["summary"]["inventory_complete"] is True


def test_indicator_inventory_parser_exposes_source_identity_and_output() -> None:
    args = cli.build_parser().parse_args(
        [
            "strategy",
            "indicator-inventory",
            "latest.py",
            "--class",
            "NostalgiaForInfinityX7",
            "--upstream-repository",
            "https://github.com/iterativv/NostalgiaForInfinity.git",
            "--upstream-commit",
            "a" * 40,
            "--output",
            ".nfi/indicator-inventory.json",
        ]
    )

    assert args.strategy_command == "indicator-inventory"
    assert args.upstream_commit == "a" * 40
    assert args.output == Path(".nfi/indicator-inventory.json")


def test_qtpylib_is_a_first_class_coverage_family(tmp_path: Path) -> None:
    source = tmp_path / "Qtpylib.py"
    source.write_text(
        "import qtpylib\n"
        "from freqtrade.strategy import IStrategy\n"
        "class Qtpylib(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        dataframe['typical'] = qtpylib.typical_price(dataframe)\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    report = build_indicator_inventory(source, class_name="Qtpylib")
    coverage = {row["family"]: row for row in report["coverage_matrix"]}

    assert coverage["qtpylib"]["present"] is True
    assert coverage["qtpylib"]["operation_count"] == 1
    assert report["operations"][0]["callable"] == "qtpylib.typical_price"


def test_missing_informative_registration_and_merge_are_not_omitted(tmp_path: Path) -> None:
    source = tmp_path / "IncompleteInformative.py"
    source.write_text(
        "from freqtrade.strategy import IStrategy\n"
        "class IncompleteInformative(IStrategy):\n"
        "    timeframe = '5m'\n"
        "    info_timeframes = ['1h']\n"
        "    def populate_indicators(self, dataframe, metadata):\n"
        "        return dataframe\n",
        encoding="utf-8",
    )

    report = build_indicator_inventory(source, class_name="IncompleteInformative")

    assert report["informative_dependencies"]["informative_timeframes"] == ["1h"]
    assert report["informative_dependencies"]["dependency_registration_present"] is False
    assert report["informative_dependencies"]["complete"] is False
    assert report["summary"]["inventory_complete"] is False


def test_pathological_expression_uses_non_recursive_structural_identity() -> None:
    expression: ast.expr = ast.Name(id="value", ctx=ast.Load())
    for _ in range(2_000):
        expression = ast.BinOp(
            left=expression,
            op=ast.Add(),
            right=ast.Constant(value=1),
        )

    rendered = _expression_text(expression)

    assert rendered.startswith("<ast-sha256:")
    assert len(rendered) == len("<ast-sha256:") + 64 + 1
