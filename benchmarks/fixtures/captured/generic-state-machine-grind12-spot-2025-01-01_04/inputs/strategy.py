from __future__ import annotations

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class GenericStateMachineGrind(IStrategy):
    """Deterministic stateful fixture for the generic callback compiler and VM."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 2
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.005
    trailing_stop = False
    use_exit_signal = False
    process_only_new_candles = True
    position_adjustment_enable = True
    max_entry_position_adjustment = 12

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["previous_green"] = (
            dataframe["close"].shift(1) > dataframe["open"].shift(1)
        ).fillna(False)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0)
            & dataframe["previous_green"]
            & (dataframe["close"] < dataframe["open"]),
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

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
