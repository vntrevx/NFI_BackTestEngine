"""Execution-shape and bounded-term validation for changed-signal evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .errors import SpecValidationError

COLUMNS: Final = ("RSI_3_15m", "RSI_3_1h", "RSI_3_4h", "AROONU_14_1h")
SURFACES: Final = ("signal_tag", "callback_columns", "trades", "full_state")
_THRESHOLDS: Final = (15.0, 20.0, 25.0, 0.0)
_TRADE_FIELDS: Final = frozenset(
    {
        "pair",
        "direction",
        "open_timestamp_ms",
        "close_timestamp_ms",
        "open_rate",
        "close_rate",
        "amount",
        "stake_amount",
        "max_stake_amount",
        "leverage",
        "entry_tag",
        "exit_reason",
        "fees",
        "profit",
        "liquidation_price",
        "orders",
        "is_open",
    }
)
_STATE_FIELDS: Final = frozenset(
    {"timestamp_ms", "pair", "wallet", "orders", "callbacks", "open_trades", "execution"}
)


def validate_trade_state(lane: Mapping[str, Any], mode: str) -> None:
    """Reject synthetic or mode-inappropriate normalized execution surfaces."""
    trades = lane["trades"]
    if mode == "spot" and trades:
        raise SpecValidationError("changed short signal cannot execute in Spot")
    if mode == "futures" and not trades:
        raise SpecValidationError("changed short signal was not executed in Futures")
    for trade in trades:
        if not trade.keys() >= _TRADE_FIELDS or len(trade["orders"]) < 2:
            raise SpecValidationError("changed signal trade surface is synthetic")
        if mode == "futures" and (
            trade["direction"] != "short"
            or trade["entry_tag"] != "562 "
            or "liquidation_price" not in trade
        ):
            raise SpecValidationError("changed signal futures trade state is incomplete")
    for state in lane["full_state"]:
        if not state.keys() >= _STATE_FIELDS:
            raise SpecValidationError("changed signal full-state surface is synthetic")


def term_matrix(callbacks: Mapping[str, Any]) -> list[list[bool]]:
    """Evaluate every bounded row against the four source-derived thresholds."""
    return [
        [
            value > threshold
            for value, threshold in zip(values, _THRESHOLDS, strict=True)
        ]
        for values in zip(*(callbacks[column] for column in COLUMNS), strict=True)
    ]
