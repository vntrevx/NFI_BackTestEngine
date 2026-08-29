import pandas as pd  # noqa: PANDAS_OK
from freqtrade.strategy import IStrategy


class CurrentShortGrindRescueContract(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 0
    can_short = True
    minimal_roi = {"0": 100.0}
    stoploss = -0.99
    use_exit_signal = True
    position_adjustment_enable = True
    max_entry_position_adjustment = 1

    def leverage(
        self,
        pair,
        current_time,
        current_rate,
        proposed_leverage,
        max_leverage,
        entry_tag,
        side,
        **kwargs,
    ):
        return min(5.0, max_leverage)

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[:, ["enter_long", "enter_short"]] = (0, 0)
        dataframe.loc[
            dataframe["date"] == pd.Timestamp("2021-02-04 00:40:00+00:00"),
            ["enter_short", "enter_tag"],
        ] = (1, "620 ")
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[:, ["exit_long", "exit_short"]] = (0, 0)
        dataframe.loc[
            dataframe["date"] == pd.Timestamp("2021-02-08 15:00:00+00:00"),
            ["exit_short", "exit_tag"],
        ] = (1, "exit_signal")
        return dataframe

    def adjust_trade_position(
        self,
        trade,
        current_time,
        current_rate,
        current_profit,
        min_stake,
        max_stake,
        current_entry_rate,
        current_exit_rate,
        current_entry_profit,
        current_exit_profit,
        **kwargs,
    ):
        filled_entries = trade.select_filled_orders(trade.entry_side)
        last_filled_entry = filled_entries[-1]
        slice_profit_entry = (
            current_rate - last_filled_entry.safe_price
        ) / last_filled_entry.safe_price
        rescue_eligible = (
            slice_profit_entry > 0.12
            and trade.liquidation_price is not None
            and current_rate > trade.liquidation_price * 0.8
            and trade.get_custom_data(key="gd5_liquidation_rescue_used") is None
        )
        if not rescue_eligible:
            return None
        trade.set_custom_data(key="gd5_liquidation_rescue_used", value=True)
        first_filled_entry = filled_entries[0]
        slice_amount = (
            first_filled_entry.safe_amount * first_filled_entry.safe_price / 0.2
        )
        buy_amount = slice_amount * 0.2 / trade.leverage
        buy_amount = max(buy_amount, float(min_stake or 0.0) * 1.5)
        if buy_amount > max_stake:
            return None
        return buy_amount, "gd5"
