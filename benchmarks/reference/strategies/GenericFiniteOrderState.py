from __future__ import annotations

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class GenericFiniteOrderState(IStrategy):
    """Deterministic official fixture for finite, source-ordered order iteration."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 2
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.005
    trailing_stop = False
    use_exit_signal = True
    process_only_new_candles = True
    position_adjustment_enable = True
    max_entry_position_adjustment = 12

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["previous_green"] = (
            dataframe["close"].shift(1) > dataframe["open"].shift(1)
        ).fillna(False)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entry_condition = (
            (dataframe["volume"] > 0)
            & dataframe["previous_green"]
            & (dataframe["close"] < dataframe["open"])
        )
        eligible_entry = entry_condition & (
            dataframe.index >= self.startup_candle_count
        )
        dataframe.loc[
            eligible_entry & (eligible_entry.cumsum() == 1),
            "enter_long",
        ] = 1
        dataframe.loc[dataframe["enter_long"] == 1, "enter_tag"] = "contract_stop"
        return dataframe

    def adjust_trade_position(
        self,
        trade,
        current_time,
        current_rate,
        current_profit,
        min_stake,
        max_stake,
        **kwargs,
    ):
        level = trade.get_custom_data("grind_level", 0)
        if current_profit < -0.001 and level < self.max_entry_position_adjustment:
            trade.set_custom_data("grind_level", level + 1)
            return (25.0, "generic_grind_entry")
        return None

    def custom_exit(
        self,
        pair,
        trade,
        current_time,
        current_rate,
        current_profit,
        **kwargs,
    ):
        filled_entries = trade.select_filled_orders(trade.entry_side)
        tagged_entry_count = 0
        for entry_order in filled_entries:
            if entry_order.ft_order_tag == "generic_grind_entry":
                tagged_entry_count += 1
        trade.set_custom_data("generic_order_count", tagged_entry_count)
        if tagged_entry_count == self.max_entry_position_adjustment:
            return "finite_order_exit"
        return None

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
