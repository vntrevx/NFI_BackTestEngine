"""Typed exit callback signatures."""

from typing import Any

EXIT_SIGNATURES: dict[str, dict[str, Any]] = {
    "confirm_trade_exit": {
        "inputs": ["pair", "trade", "order_type", "amount", "rate", "reason", "timestamp"],
        "returns": "bool",
    },
    "custom_exit": {
        "inputs": ["pair", "trade", "timestamp", "rate", "profit"],
        "returns": "exit_reason|null",
    },
    "custom_exit_price": {
        "inputs": ["pair", "trade", "timestamp", "proposed_rate", "profit", "exit_tag"],
        "returns": "price",
    },
    "custom_roi": {
        "inputs": [
            "pair",
            "trade",
            "timestamp",
            "duration_minutes",
            "entry_tag|null",
            "side",
        ],
        "returns": "roi_ratio",
    },
    "custom_stoploss": {
        "inputs": ["pair", "trade", "timestamp", "rate", "profit", "after_fill"],
        "returns": "stoploss_ratio",
    },
}
