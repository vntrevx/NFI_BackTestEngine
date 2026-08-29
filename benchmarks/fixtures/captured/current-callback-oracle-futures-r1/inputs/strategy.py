from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pandas import DataFrame
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy


class Task14CallbackOracle(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    startup_candle_count = 3
    can_short = False
    position_adjustment_enable = True
    max_entry_position_adjustment = 2
    use_custom_stoploss = True
    use_exit_signal = True
    stoploss = -0.20
    minimal_roi = {"0": 10.0}
    process_only_new_candles = True

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.entry_confirms = 0
        self.adjustments = 0
        self.exit_confirms = 0
        self.sequence = 0

    def _emit(self, callback: str, current_time: datetime, **fields: Any) -> None:
        self.sequence += 1
        payload = {
            "sequence": self.sequence,
            "callback": callback,
            "timestamp_ms": int(current_time.timestamp() * 1000),
            **fields,
        }
        print("TASK14|" + json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)

    @staticmethod
    def _state(trade: Trade) -> dict[str, Any]:
        return {
            "system_version": trade.get_custom_data("system_version", None),
            "derisk_level_1": trade.get_custom_data("derisk_level_1", None),
            "entries": trade.nr_of_successful_entries,
            "exits": trade.nr_of_successful_exits,
            "orders": len(trade.orders),
            "stake_amount": trade.stake_amount,
        }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["oracle_row"] = range(len(dataframe))
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 1
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = "task14"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        dataframe, _ = self.dp.get_analyzed_dataframe(self.config["exchange"]["pair_whitelist"][0], self.timeframe)
        last_visible = None if dataframe.empty else int(dataframe.iloc[-1]["date"].timestamp() * 1000)
        self._emit(
            "bot_loop_start",
            current_time,
            predicate="main_candle_start",
            result={"kind": "none"},
            visible_rows=len(dataframe),
            last_visible_timestamp_ms=last_visible,
        )

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: str | None,
                 side: str, **kwargs) -> float:
        value = min(2.0, max_leverage)
        self._emit("leverage", current_time, predicate="futures_only", result={"kind": "value", "value": value})
        return value

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None, max_stake: float,
                            leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        value = min(80.0, max_stake)
        self._emit("custom_stake_amount", current_time, predicate="initial_entry_only",
                   result={"kind": "value", "value": value}, leverage=leverage)
        return value

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        self.entry_confirms += 1
        accepted = self.entry_confirms > 1
        self._emit("confirm_trade_entry", current_time, predicate="order_amount_validated",
                   result={"kind": "accept" if accepted else "reject", "value": accepted}, amount=amount)
        return accepted

    def order_filled(self, pair: str, trade: Trade, order, current_time: datetime, **kwargs) -> None:
        before = self._state(trade)
        if order.ft_order_side == trade.entry_side and before["system_version"] is None:
            trade.set_custom_data("system_version", "filled-visible")
        self._emit("order_filled", current_time, predicate=order.ft_order_side,
                   result={"kind": "none"}, before=before, after=self._state(trade), order_tag=order.ft_order_tag)

    def adjust_trade_position(self, trade: Trade, current_time: datetime, current_rate: float,
                              current_profit: float, min_stake: float | None, max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float, **kwargs):
        self.adjustments += 1
        before = self._state(trade)
        if self.adjustments == 1:
            result = None
            self._emit("adjust_trade_position", current_time, predicate="first_open_candle",
                       result={"kind": "none"}, before=before, after=self._state(trade))
            return result
        if self.adjustments == 2:
            result = (25.0, "scale-in")
            self._emit("adjust_trade_position", current_time, predicate="scale_in",
                       result={"kind": "value", "value": [25.0, "scale-in"]}, before=before, after=self._state(trade))
            return result
        if self.adjustments == 3:
            trade.set_custom_data("derisk_level_1", True)
            trade.stake_amount = 1.0
            self._emit("adjust_trade_position", current_time, predicate="rollback_probe",
                       result={"kind": "exception", "type": "ValueError"}, before=before, after=self._state(trade))
            raise ValueError("task14 rollback probe")
        self._emit("adjust_trade_position", current_time, predicate="competition",
                   result={"kind": "none"}, before=before, after=self._state(trade))
        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool, **kwargs):
        competition = self.adjustments >= 4 and not after_fill
        value = 0.01 if competition else None
        self._emit("custom_stoploss", current_time, predicate="competition" if competition else "not_eligible",
                   result={"kind": "value", "value": value} if value is not None else {"kind": "none"},
                   state=self._state(trade), after_fill=after_fill)
        return value

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        eligible = self.adjustments >= 4
        value = "same-candle-custom" if eligible else None
        self._emit("custom_exit", current_time, predicate="eligible" if eligible else "not_eligible",
                   result={"kind": "value", "value": value} if value else {"kind": "none"}, state=self._state(trade))
        return value

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        self.exit_confirms += 1
        spot_reject_custom = self.config["trading_mode"] == "spot" and exit_reason == "same-candle-custom"
        accepted = not spot_reject_custom
        self._emit("confirm_trade_exit", current_time, predicate=exit_reason,
                   result={"kind": "accept" if accepted else "reject", "value": accepted}, state=self._state(trade))
        return accepted
