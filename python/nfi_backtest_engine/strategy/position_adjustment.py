"""Typed price and position-adjustment callback signatures."""

from typing import Any

POSITION_ADJUSTMENT_SIGNATURES: dict[str, dict[str, Any]] = {
    "adjust_entry_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
        ],
        "returns": "price|null",
    },
    "adjust_exit_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
        ],
        "returns": "price|null",
    },
    "adjust_order_price": {
        "inputs": [
            "trade",
            "order|null",
            "pair",
            "timestamp",
            "proposed_rate",
            "current_order_rate",
            "entry_tag|null",
            "side",
            "is_entry",
        ],
        "returns": "price|null",
    },
    "adjust_trade_position": {
        "inputs": ["trade", "timestamp", "rate", "profit", "wallet", "orders"],
        "returns": "position_adjustment|null",
    },
}
