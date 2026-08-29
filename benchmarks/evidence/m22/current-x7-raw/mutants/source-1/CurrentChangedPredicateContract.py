import numpy as np
import pandas as pd  # noqa: PANDAS_OK
from freqtrade.strategy import IStrategy


def append_tag(target, mask, tag):
    target[mask] = target[mask] + tag


class CurrentChangedPredicateContract(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 0
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.5
    use_exit_signal = True

    def populate_indicators(self, dataframe, metadata):
        slots = np.arange(len(dataframe)) % 96
        boundary = slots < 5
        dataframe["RSI_3_15m"] = np.where(
            slots == 1, 15.000000000000002, np.where(boundary, 15.0, 14.0)
        )
        dataframe["RSI_3_1h"] = np.where(
            slots == 2, 20.000000000000004, np.where(boundary, 20.0, 19.0)
        )
        dataframe["RSI_3_4h"] = np.where(
            slots == 3, 25.000000000000004, np.where(boundary, 25.0, 24.0)
        )
        dataframe["AROONU_14_1h"] = np.where(
            slots == 4, 5e-324, np.where(boundary, 0.0, -1.0)
        )
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        entry_tags = np.full(len(dataframe), "", dtype=object)
        dataframe.loc[:, ["enter_long", "enter_short"]] = (0, 0)
        changed_predicate = (
            (dataframe["RSI_3_15m"] > 15.0)
            | (dataframe["RSI_3_4h"] > 20.0)
            | (dataframe["RSI_3_4h"] > 25.0)
            | (dataframe["AROONU_14_1h"] > 0.0)
        )
        append_tag(entry_tags, changed_predicate, "562 ")
        dataframe.loc[changed_predicate, "enter_short"] = 1
        dataframe.loc[:, "enter_tag"] = pd.array(entry_tags, dtype="string")
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        changed_exit = (
            (dataframe["RSI_3_15m"] < 15.0)
            & (dataframe["RSI_3_1h"] < 20.0)
            & (dataframe["RSI_3_4h"] < 25.0)
            & (dataframe["AROONU_14_1h"] < 0.0)
        )
        dataframe.loc[:, ["exit_long", "exit_short"]] = (0, 0)
        dataframe.loc[changed_exit, "exit_short"] = 1
        dataframe.loc[:, "exit_tag"] = ""
        return dataframe
