from freqtrade.strategy import IStrategy


class SignalProgramContract(IStrategy):
    timeframe = "5m"

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[:, ["enter_long", "enter_short"]] = (0, 0)
        positive = dataframe["score"] > 0
        dataframe.loc[positive, "enter_long"] = 1
        dataframe.loc[dataframe["score"] >= 2, "enter_long"] = 0
        dataframe["enter_short"] = (dataframe["score"] < 0).astype(int)
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[:, ["exit_long", "exit_short"]] = 0
        dataframe.loc[
            (dataframe["enter_long"] == 0) & (dataframe["score"] > 1),
            "exit_long",
        ] = 1
        dataframe.loc[dataframe["exit_mask"], "exit_short"] = 1
        return dataframe
