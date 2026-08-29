from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class PortfolioPressureOracle(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 0
    can_short = False
    position_adjustment_enable = True
    max_entry_position_adjustment = 0
    use_exit_signal = True
    stoploss = -0.99
    minimal_roi = {"0": 100.0}
    process_only_new_candles = True

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._stake_calls: dict[int, int] = {}
        self._event_sequence = 0

    def _emit(self, callback: str, current_time: datetime, **fields: Any) -> None:
        self._event_sequence += 1
        print("TASK15|" + json.dumps({
            "sequence": self._event_sequence,
            "callback": callback,
            "timestamp_ms": int(current_time.timestamp() * 1000),
            **fields,
        }, sort_keys=True, separators=(",", ":")), flush=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        minute = dataframe["date"].dt.minute
        dataframe["enter_long"] = minute.isin((10, 20, 40)).astype(int)
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = "portfolio-pressure"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs: Any,
    ) -> float:
        timestamp = int(current_time.timestamp() * 1000)
        call = self._stake_calls.get(timestamp, 0) + 1
        self._stake_calls[timestamp] = call
        rejected = current_time.minute == 25 and call == 1
        result = 0.0 if rejected else proposed_stake
        self._emit(
            "custom_stake_amount", current_time, pair=pair, call_at_timestamp=call,
            proposed_stake=proposed_stake, max_stake=max_stake, result=result,
            decision="reject" if rejected else "accept",
        )
        return result

    def adjust_trade_position(
        self, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, min_stake: float | None, max_stake: float,
        current_entry_rate: float, current_exit_rate: float,
        current_entry_profit: float, current_exit_profit: float, **kwargs: Any,
    ):
        if trade.id == 2 and current_time.minute == 35 and trade.nr_of_successful_exits == 0:
            result = -trade.stake_amount / 2
            self._emit(
                "adjust_trade_position", current_time, pair=trade.pair,
                trade_id=trade.id, stake_before=trade.stake_amount,
                result=result, decision="partial_exit",
            )
            return result, "pressure-partial"
        return None

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, **kwargs: Any,
    ) -> str | None:
        if (trade.id == 1 and current_time.minute == 25) or (
            trade.id == 2 and current_time.minute == 45
        ):
            self._emit(
                "custom_exit", current_time, pair=pair, trade_id=trade.id,
                current_profit=current_profit, decision="exit",
            )
            return "pressure-rotate"
        return None
