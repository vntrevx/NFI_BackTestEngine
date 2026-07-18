from __future__ import annotations

from datetime import datetime

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class ContractNormalRouting(IStrategy):
    """Small callback-heavy strategy used only to capture semantic fixtures."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 4
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.03
    use_exit_signal = True
    position_adjustment_enable = True
    max_entry_position_adjustment = 1
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["route_slot"] = dataframe.index % 72
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0) & (dataframe["route_slot"] == 0),
            "enter_long",
        ] = 1
        dataframe.loc[dataframe["enter_long"] == 1, "enter_tag"] = "contract_route"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | tuple[float, str] | None:
        filled_entries = trade.select_filled_orders(trade.entry_side)
        if current_profit < -0.004 and len(filled_entries) == 1:
            stake = min(filled_entries[0].cost * 0.5, max_stake)
            if min_stake is None or stake >= min_stake:
                return stake, "contract_rebuy"
        return None

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        if (current_time - trade.open_date_utc).total_seconds() >= 6 * 60 * 60:
            return "contract_timed_exit"
        return None
