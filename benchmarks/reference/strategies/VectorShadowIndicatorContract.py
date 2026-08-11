from __future__ import annotations

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy


class VectorShadowIndicatorContract(IStrategy):
    timeframe = "5m"

    def populate_indicators(self, dataframe, metadata):
        delta = dataframe["close"] - dataframe["open"]
        dataframe["delta"] = delta
        dataframe["previous_close"] = dataframe["close"].shift(1)
        dataframe["mean3"] = pd.Series(dataframe["close"]).rolling(3).mean()
        dataframe["selected"] = np.where(
            delta > 0,
            dataframe["mean3"],
            dataframe["previous_close"],
        )
        return dataframe
