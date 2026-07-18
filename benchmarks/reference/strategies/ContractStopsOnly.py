from __future__ import annotations

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class ContractStopsOnly(IStrategy):
    """Small deterministic reference strategy whose normal exit is stoploss."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 2
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.005
    trailing_stop = False
    use_exit_signal = False
    process_only_new_candles = True

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

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
