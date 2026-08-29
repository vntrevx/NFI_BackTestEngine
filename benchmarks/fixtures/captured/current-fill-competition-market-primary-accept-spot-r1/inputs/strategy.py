from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class FillCompetitionBase(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 0
    can_short = False
    use_exit_signal = True
    stoploss = -0.05
    minimal_roi = {"0": 0.01}
    process_only_new_candles = True
    mode = "base"
    order_types = {"entry": "limit", "exit": "limit", "stoploss": "limit", "stoploss_on_exchange": False}

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.sequence = 0

    def emit(self, callback: str, current_time: datetime, **fields: Any) -> None:
        self.sequence += 1
        print("TASK16|" + json.dumps({"sequence": self.sequence, "callback": callback,
            "timestamp_ms": int(current_time.timestamp() * 1000), "mode": self.mode, **fields},
            sort_keys=True, separators=(",", ":")), flush=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = (dataframe["date"].dt.minute == 0).astype(int)
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = "task16-entry"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        signal = self.mode == "signal"
        dataframe["exit_long"] = ((dataframe["date"].dt.minute == 5) & signal).astype(int)
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = "task16-explicit"
        return dataframe

    def custom_entry_price(self, pair: str, trade: Trade | None, current_time: datetime,
                           proposed_rate: float, entry_tag: str | None, side: str, **kwargs: Any) -> float:
        value = 99.237
        self.emit("custom_entry_price", current_time, proposed_rate=proposed_rate, result=value)
        return value

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs: Any) -> str | None:
        eligible = current_time.minute == 10 and self.mode != "signal"
        self.emit("custom_exit", current_time, eligible=eligible, current_rate=current_rate)
        return "task16-primary" if eligible else None

    def custom_exit_price(self, pair: str, trade: Trade, current_time: datetime,
                          proposed_rate: float, current_profit: float,
                          exit_tag: str | None, **kwargs: Any) -> float:
        value = 101.237
        self.emit("custom_exit_price", current_time, proposed_rate=proposed_rate,
                  result=value, exit_tag=exit_tag)
        return value

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs: Any) -> bool:
        self.emit("confirm_trade_entry", current_time, order_type=order_type,
                  amount=amount, rate=rate, result=True)
        return True

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        if exit_reason == "force_exit":
            accepted = True
        elif self.mode == "primary":
            accepted = exit_reason == "task16-primary"
        elif self.mode in {"fallthrough", "signal"}:
            accepted = exit_reason == "stop_loss"
        elif self.mode == "trailing":
            accepted = exit_reason == "trailing_stop_loss"
        else:
            accepted = False
        self.emit("confirm_trade_exit", current_time, order_type=order_type,
                  amount=amount, rate=rate, exit_reason=exit_reason, result=accepted)
        return accepted


class LimitFallthroughOracle(FillCompetitionBase):
    mode = "fallthrough"


class LimitAllRejectedOracle(FillCompetitionBase):
    mode = "all-rejected"


class LimitSignalOracle(FillCompetitionBase):
    mode = "signal"


class LimitPrimaryAcceptOracle(FillCompetitionBase):
    mode = "primary"


class LimitTrailingOracle(FillCompetitionBase):
    mode = "trailing"
    trailing_stop = True
    trailing_only_offset_is_reached = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.03


class MarketPrimaryAcceptOracle(LimitPrimaryAcceptOracle):
    order_types = {"entry": "market", "exit": "market", "stoploss": "market", "stoploss_on_exchange": False}
