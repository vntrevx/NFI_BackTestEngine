from freqtrade.strategy import IStrategy


class TagProgramContract(IStrategy):
    timeframe = "5m"

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[:, ["enter_long", "enter_short"]] = (0, 0)
        long_route = 101
        short_route = 562
        long_mask = dataframe["score"] >= 0
        short_mask = dataframe["score"] <= 0
        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] += f"{long_route} "
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] += f"{short_route} "
        override_mask = dataframe["score"] >= 2
        dataframe.loc[override_mask, "enter_tag"] = "override "
        dataframe.loc[override_mask, "enter_tag"] += "final  "
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[:, ["exit_long", "exit_short"]] = (0, 0)
        long_mask = (dataframe["enter_long"] == 1) & (dataframe["score"] > 1)
        dataframe.loc[long_mask, ["exit_long", "exit_tag"]] = (1, "profit ")
        dataframe.loc[dataframe["exit_mask"], "exit_short"] = 1
        dataframe.loc[dataframe["exit_mask"], "exit_tag"] += "signal "
        return dataframe
