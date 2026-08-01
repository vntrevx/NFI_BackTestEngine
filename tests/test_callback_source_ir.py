from __future__ import annotations

from pathlib import Path

from nfi_backtest_engine.callback_source_ir import compile_callback_source_ir
from nfi_backtest_engine.specs import CALLBACK_SOURCE_IR_SCHEMA, validate_schema


def _write_strategy(path: Path, *, route: str = "42", threshold: str = "0.10") -> None:
    path.write_text(
        f'''from freqtrade.strategy import IStrategy

class SourceDriven(IStrategy):
    timeframe = "5m"
    long_route_tags = ["{route}", "grind"]

    def order_filled(self, pair, trade, order, current_time, **kwargs):
        if order.ft_order_tag == "grind_entry":
            trade.set_custom_data("visited", True)

    def exit_helper(self, trade, last_candle, current_profit):
        if current_profit > {threshold} and last_candle["RSI_14"] > 70:
            return True, "take_profit" if trade.is_short else "take_profit_long"
        return False, None

    def custom_exit(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs):
        route_tags = self.long_route_tags
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last_candle = dataframe.iloc[-1]
        enter_tags = trade.enter_tag.split()
        if any(tag in route_tags for tag in enter_tags):
            sell, reason = self.exit_helper(trade, last_candle, current_profit)
            if sell:
                return f"{{reason}} ( {{trade.enter_tag}})"
        return None
''',
        encoding="utf-8",
    )


def test_callback_source_ir_preserves_routes_tags_reads_and_source_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "SourceDriven.py"
    _write_strategy(source)

    program = compile_callback_source_ir(
        source,
        class_name="SourceDriven",
        trading_mode="spot",
    )
    validate_schema(program, CALLBACK_SOURCE_IR_SCHEMA)

    assert [item["name"] for item in program["entrypoints"]] == [
        "order_filled",
        "custom_exit",
    ]
    custom_exit = program["entrypoints"][1]
    assert custom_exit["source_order"] == 1
    assert custom_exit["reachable_methods"] == ["custom_exit", "exit_helper"]
    assert custom_exit["route_keys"] == ["long_route_tags"]
    assert program["route_keys"][0]["values"] == ["42", "grind"]
    assert {item["value"] for item in program["emitted_tags"]} >= {
        "take_profit",
        "take_profit_long",
        "{reason} ( {trade.enter_tag})",
    }
    assert program["required_columns"] == [
        {"name": "RSI_14", "entrypoints": ["custom_exit"]}
    ]
    assert {
        (item["source"], item["key"])
        for item in program["required_reads"]
    } >= {
        ("candle", "current_profit"),
        ("order", "ft_order_tag"),
        ("trade", "enter_tag"),
    }
    assert all("signal" not in key["key"] for key in program["route_keys"])


def test_callback_source_ir_mutates_from_source_data_without_number_opcodes(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before.py"
    after = tmp_path / "after.py"
    _write_strategy(before, route="65", threshold="0.10")
    _write_strategy(after, route="future_tag", threshold="0.13")

    old = compile_callback_source_ir(before, class_name="SourceDriven")
    new = compile_callback_source_ir(after, class_name="SourceDriven")

    assert old["route_keys"][0]["values"][0] == "65"
    assert new["route_keys"][0]["values"][0] == "future_tag"
    assert old["fingerprint"] != new["fingerprint"]
    assert "opcodes" not in old
    assert "opcodes" not in new


def test_callback_source_ir_fingerprint_ignores_checkout_path(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "nested" / "second.py"
    second.parent.mkdir()
    _write_strategy(first)
    second.write_bytes(first.read_bytes())

    one = compile_callback_source_ir(first, class_name="SourceDriven")
    two = compile_callback_source_ir(second, class_name="SourceDriven")

    assert one["source"]["path"] != two["source"]["path"]
    assert one["fingerprint"] == two["fingerprint"]

    second.write_text("# checkout-only comment\n" + second.read_text(encoding="utf-8"))
    commented = compile_callback_source_ir(second, class_name="SourceDriven")

    assert commented["source"]["sha256"] != one["source"]["sha256"]
    assert commented["fingerprint"] == one["fingerprint"]


def test_callback_source_ir_marks_leverage_inactive_for_spot(tmp_path: Path) -> None:
    source = tmp_path / "Leverage.py"
    source.write_text(
        '''from freqtrade.strategy import IStrategy
class Leverage(IStrategy):
    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs):
        return proposed_leverage
''',
        encoding="utf-8",
    )

    spot = compile_callback_source_ir(source, class_name="Leverage", trading_mode="spot")
    futures = compile_callback_source_ir(
        source,
        class_name="Leverage",
        trading_mode="futures",
    )

    assert spot["entrypoints"][0]["active_for_mode"] is False
    assert futures["entrypoints"][0]["active_for_mode"] is True
