from __future__ import annotations

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class IndicatorProgramContract(IStrategy):
    """Small source-only contract for the causal indicator-program-v1 DAG."""

    timeframe = "5m"

    @staticmethod
    def rsi(values):
        return ta.RSI(values, timeperiod=14)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        dataframe["rsi"] = self.rsi(close)
        dataframe["mean4"] = pd.Series(close).rolling(4).mean()
        dataframe["previous"] = close.shift(1)
        dataframe["ewm8"] = pd.Series(close).ewm(span=8, adjust=False).mean()
        dataframe["selected"] = np.where(
            dataframe["rsi"] > 50,
            dataframe["mean4"],
            dataframe["previous"],
        )
        return dataframe
